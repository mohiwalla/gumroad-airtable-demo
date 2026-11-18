import io
import json
import sqlite3
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

import sync_sales as sync


def example(index):
    return {
        "id": f"sale-{index}", "product_id": "art-book",
        "email": "reader@example.invalid", "created_at": "2026-11-15T21:03:00+05:30",
        "price": 1005, "formatted_display_price": "€9.00",
        "refunded": False, "partially_refunded": False,
    }


class SalesTests(unittest.TestCase):
    def test_cursor_preserves_filters_and_stays_on_gumroad(self):
        pages = [{"success": True, "sales": [], "next_page_key": "part:+&/=",
                  "next_page_url": "https://other.example.invalid/"},
                 {"success": True, "sales": []}]
        with patch.object(sync, "request_json", side_effect=pages) as request:
            self.assertEqual(list(sync.gumroad_pages("test", "2026-11-14")), [[], []])
        self.assertEqual(request.call_args.args, ("GET", sync.GUMROAD_SALES + "?after=2026-11-14&page_key=part%3A%2B%26%2F%3D", "test"))

    def test_currency_date_and_contact(self):
        fields = sync.fields_for(example(1))
        self.assertEqual(fields["Sale amount USD"], 10.05)
        self.assertEqual(fields["Displayed price"], "€9.00")
        self.assertEqual(fields["Transaction date"], "2026-11-15T15:33:00Z")
        self.assertFalse(fields["Can contact"])
        for invalid in (True, "1005", -1):
            with self.assertRaises(sync.SyncError):
                sync.fields_for(dict(example(1), price=invalid))

    def test_duplicate_cursor_aborts(self):
        page = {"success": True, "sales": [], "next_page_key": "same"}
        with self.assertRaises(sync.SyncError):
            list(sync.read_pages(lambda _: page))

    def test_changed_rows_and_batch_boundaries(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        sync.init_state(db, "a/t")
        inputs = [example(i) for i in range(23)]
        writer = MagicMock()
        sync.run_sync([inputs], writer, db)
        self.assertEqual([len(c.args[0]) for c in writer.call_args_list], [10, 10, 3])
        inputs[0]["partially_refunded"] = True
        result = sync.run_sync([inputs], writer, db)
        self.assertEqual(result["upserted"], 1)
        self.assertEqual(writer.call_args.args[0][0]["Refund status"], "Partially refunded")
        self.assertEqual(sync.run_sync([inputs], writer, db, full=True)["upserted"], 23)
        with self.assertRaises(sync.SyncError):
            sync.init_state(db, "different/table")

    def test_patch_receipt_requires_the_same_keys(self):
        rows = [sync.fields_for(example(1))]
        writer = sync.airtable_writer("appTEST", "tblTEST", "test")
        with patch.object(sync, "request_json", return_value={"records": [{"fields": {"Sale ID": "different"}}]}) as request:
            with self.assertRaises(sync.SyncError):
                writer(rows)
        method, url, token, payload = request.call_args.args
        self.assertEqual((method, url, token), ("PATCH", "https://api.airtable.com/v0/appTEST/tblTEST", "test"))
        self.assertEqual(payload["performUpsert"], {"fieldsToMergeOn": ["Sale ID"]})
        self.assertEqual(payload["records"], [{"fields": rows[0]}])

    def test_rate_limit_wait_and_success(self):
        error = HTTPError(sync.GUMROAD_SALES, 429, "rate limit", {"Retry-After": "45"}, io.BytesIO())
        success = io.BytesIO(json.dumps({"success": True}).encode())
        with patch.object(sync, "build_opener") as opener, patch.object(sync.time, "sleep") as sleep:
            opener.return_value.open.side_effect = [error, success]
            self.assertEqual(sync.request_json("GET", sync.GUMROAD_SALES, "test"), {"success": True})
            sleep.assert_called_once_with(45)


if __name__ == "__main__":
    unittest.main()
