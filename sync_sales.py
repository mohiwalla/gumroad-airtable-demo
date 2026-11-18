"""Copy Gumroad sales into a seller-owned Airtable table. Python 3.10+."""
import argparse
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import build_opener, HTTPRedirectHandler, Request

GUMROAD_SALES = "https://api.gumroad.com/v2/sales"
BATCH_SIZE = 10


class SyncError(Exception):
    pass


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(method, url, token, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + token, "Content-Type": "application/json",
        "Accept": "application/json", "User-Agent": "GumroadSalesSync/1.0",
    })
    client = build_opener(NoRedirects())
    service = "Gumroad" if url.startswith(GUMROAD_SALES) else "Airtable"
    for attempt in range(4):
        try:
            with client.open(request, timeout=35) as response:
                parsed = json.load(response)
            if not isinstance(parsed, dict):
                raise SyncError(f"{service} returned an unexpected response.")
            return parsed
        except HTTPError as exc:
            code = exc.code
            retry = exc.headers.get("Retry-After", "")
            exc.close()
            if code not in {429, 500, 502, 503, 504} or attempt == 3:
                raise SyncError(f"{service} returned HTTP {code}. Check credentials, permissions, fields and API limits.") from None
            delay = max(30 if code == 429 else 1, 2**attempt)
            if retry.isascii() and retry.isdecimal():
                delay = max(delay, int(retry))
            if delay > 120:
                raise SyncError(f"{service} requested a longer pause. Retry on the next scheduled run.") from None
        except (URLError, TimeoutError, ConnectionError):
            if attempt == 3:
                raise SyncError(f"{service} connection failed after four attempts.") from None
            delay = 2**attempt
        except (ValueError, UnicodeError):
            raise SyncError(f"{service} returned invalid JSON.") from None
        time.sleep(delay)


def read_pages(fetch):
    cursor = None
    seen = set()
    while True:
        page = fetch(cursor)
        if not isinstance(page, dict) or page.get("success") is not True or not isinstance(page.get("sales"), list):
            raise SyncError("Invalid Gumroad page. Expected success=true and a sales array.")
        yield page["sales"]
        cursor = page.get("next_page_key")
        if cursor is None:
            if page.get("next_page_url"):
                raise SyncError("Gumroad returned a next-page URL without its cursor.")
            return
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            raise SyncError("Gumroad returned an invalid or repeated page key.")
        seen.add(cursor)


def gumroad_pages(token, after):
    def fetch(cursor):
        params = {}
        if after:
            params["after"] = after
        if cursor:
            params["page_key"] = cursor
        query = "?" + urlencode(params) if params else ""
        return request_json("GET", GUMROAD_SALES + query, token)
    return read_pages(fetch)


def fixture_pages(path):
    with path.open(encoding="utf-8") as source:
        pages = json.load(source)
    if not isinstance(pages, list) or not pages:
        raise SyncError("Expected a non-empty JSON array of Gumroad pages.")
    iterator = iter(pages)
    def fetch(_cursor):
        try:
            return next(iterator)
        except StopIteration:
            raise SyncError("Fixture ends before the last page.") from None
    yield from read_pages(fetch)
    if next(iterator, False) is not False:
        raise SyncError("Fixture contains a page after the final cursor.")


def fields_for(sale):
    if not isinstance(sale, dict):
        raise SyncError("Expected a sale object.")
    for name in ("id", "product_id", "email", "created_at"):
        if not isinstance(sale.get(name), str) or not sale[name].strip():
            raise SyncError(f"Gumroad sale lacks a valid {name} field.")
    try:
        timestamp = datetime.fromisoformat(sale["created_at"].replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError
    except ValueError:
        raise SyncError("Gumroad created_at must be an ISO timestamp with a timezone.") from None
    cents = sale.get("price")
    if type(cents) is not int or cents < 0:
        raise SyncError("Gumroad price must be a nonnegative integer in USD cents.")
    for flag in ("can_contact", "refunded", "partially_refunded"):
        if sale.get(flag) is not None and type(sale[flag]) is not bool:
            raise SyncError(f"Expected a true/false value in {flag}.")
    display = sale.get("formatted_display_price")
    if display is not None and not isinstance(display, str):
        raise SyncError("Gumroad formatted_display_price must be text.")
    if sale.get("refunded") is True:
        refund_status = "Refunded"
    elif sale.get("partially_refunded") is True:
        refund_status = "Partially refunded"
    elif sale.get("refunded") is False and sale.get("partially_refunded") is False:
        refund_status = "No refund"
    else:
        refund_status = "Unreported"
    return {
        "Sale ID": sale["id"], "Email": sale["email"], "Product ID": sale["product_id"],
        "Transaction date": timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "Sale amount USD": cents / 100, "Displayed price": display,
        "Can contact": sale.get("can_contact") is True, "Refund status": refund_status,
    }


def init_state(db, destination):
    with db:
        db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS synced (id TEXT PRIMARY KEY, digest TEXT NOT NULL)")
        db.execute("INSERT OR IGNORE INTO meta VALUES ('destination', ?)", (destination,))
        if db.execute("SELECT value FROM meta WHERE key='destination'").fetchone()[0] != destination:
            raise SyncError("This state belongs to a different Airtable destination. Choose a separate --state path.")


def checkpoint(db):
    row = db.execute("SELECT value FROM meta WHERE key='checkpoint'").fetchone()
    return (date.fromisoformat(row[0]) - timedelta(days=1)).isoformat() if row else None


def run_sync(pages, write_batch, db, full=False):
    """Send changed rows. Record fingerprints and the watermark together on success."""
    stats = {"pages": 0, "seen": 0, "upserted": 0, "unchanged": 0, "batches": 0}
    started = datetime.now(timezone.utc).date().isoformat()
    db.execute("BEGIN IMMEDIATE")
    try:
        for sales in pages:
            stats["pages"] += 1
            stats["seen"] += len(sales)
            pending = {}
            for sale in sales:
                fields = fields_for(sale)
                pending[fields["Sale ID"]] = fields
            changed = []
            for sale_id, fields in pending.items():
                encoded = json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()
                digest = hashlib.sha256(encoded).hexdigest()
                existing = db.execute("SELECT digest FROM synced WHERE id=?", (sale_id,)).fetchone()
                if not full and existing == (digest,):
                    stats["unchanged"] += 1
                else:
                    changed.append((fields, digest))
            for i in range(0, len(changed), BATCH_SIZE):
                batch = changed[i:i + BATCH_SIZE]
                write_batch([fields for fields, _ in batch])
                db.executemany("INSERT OR REPLACE INTO synced VALUES (?, ?)",
                               [(fields["Sale ID"], digest) for fields, digest in batch])
                stats["batches"] += 1
                stats["upserted"] += len(batch)
        db.execute("INSERT OR REPLACE INTO meta VALUES ('checkpoint', ?)", (started,))
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return stats


def airtable_writer(base, table, token):
    for value, prefix in ((base, "app"), (table, "tbl")):
        if not re.fullmatch(prefix + r"[A-Za-z0-9]+", value):
            raise SyncError(f"Expected an Airtable {prefix}… ID.")
    url = f"https://api.airtable.com/v0/{base}/{table}"
    def write(rows):
        response = request_json("PATCH", url, token, {
            "performUpsert": {"fieldsToMergeOn": ["Sale ID"]},
            "records": [{"fields": fields} for fields in rows],
        })
        records = response.get("records")
        if not isinstance(records, list) or len(records) != len(rows):
            raise SyncError("Airtable did not confirm every row. Retry this batch.")
        confirmed = {r.get("fields", {}).get("Sale ID") for r in records if isinstance(r, dict) and isinstance(r.get("fields"), dict)}
        if confirmed != {row["Sale ID"] for row in rows}:
            raise SyncError("Airtable confirmed unexpected Sale IDs. Check the table mapping.")
        time.sleep(0.22)
    return write


def env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise SyncError(f"Set {name} in the scheduler's private environment.")
    return value


def demo():
    sample = Path(__file__).with_name("demo-sales.json")
    table = {}
    def write(rows):
        for row in rows:
            table[row["Sale ID"]] = row
    with closing(sqlite3.connect(":memory:")) as db:
        init_state(db, "demo")
        first = run_sync(fixture_pages(sample), write, db)
        replay = run_sync(fixture_pages(sample), write, db)
    return {"mode": "offline demo", "first": first, "replay": replay, "example_rows": list(table.values())}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--demo", action="store_true", help="run a synthetic sale and a free download twice, offline")
    group.add_argument("--dry-run", action="store_true", help="validate mapped sales and print counts only")
    group.add_argument("--apply", action="store_true", help="write changes to the configured Airtable table")
    parser.add_argument("--fixture", type=Path, help="read JSON pages from a file during dry-run")
    parser.add_argument("--state", type=Path, default=Path.home()/".local"/"state"/"gumroad-sales"/"sync.sqlite3")
    parser.add_argument("--full", action="store_true", help="read all sale history and rewrite every mapped row")
    args = parser.parse_args(argv)
    if args.fixture and not args.dry_run:
        parser.error("--fixture requires --dry-run")
    if args.demo:
        result = demo()
    elif args.dry_run:
        pages = fixture_pages(args.fixture) if args.fixture else gumroad_pages(env("GUMROAD_ACCESS_TOKEN"), None)
        counts = {"mode": "dry run", "pages": 0, "seen": 0, "writes": 0}
        for page in pages:
            counts["pages"] += 1
            for sale in page:
                fields_for(sale)
                counts["seen"] += 1
        result = counts
    else:
        base, table = env("AIRTABLE_BASE_ID"), env("AIRTABLE_TABLE_ID")
        writer = airtable_writer(base, table, env("AIRTABLE_TOKEN"))
        source = env("GUMROAD_ACCESS_TOKEN")
        args.state.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(args.state, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with closing(sqlite3.connect(args.state, timeout=5)) as db:
            init_state(db, base + "/" + table)
            after = None if args.full else checkpoint(db)
            result = {"mode": "apply", **run_sync(gumroad_pages(source, after), writer, db, full=args.full)}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (SyncError, OSError, sqlite3.Error, ValueError) as error:
        print(f"Sync stopped: {error}", file=sys.stderr)
        sys.exit(1)
