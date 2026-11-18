# Gumroad sales in Airtable

Each Gumroad sale gets one Airtable row. A second purchase from the same email gets a separate row. Run the sync from a Mac or Linux host you control using Python 3.10 or later.

The [public demo run](https://github.com/mohiwalla/gumroad-airtable-demo/actions/workflows/demo.yml) shows the rows created for a $24.99 workbook and a free download. Open a successful run, select **demo**, and expand **Run the fictional store twice and show the resulting rows**. The second pass writes zero rows. A separate test simulates Airtable receiving a write just before the connection drops.

| Fictional order | Amount | Customer email | May contact |
| --- | ---: | --- | --- |
| Workbook | $24.99 | mira@example.invalid | Yes |
| Sampler | $0.00 | sam@example.invalid | No |

For a store owner who wants the integration set up, a one-store, one-table pilot is $295 USD. It includes column mapping, scheduled imports on your host, verification against a sale in your own accounts, and handover. Keep store data and credentials private. [Ask about setup](https://github.com/mohiwalla/gumroad-airtable-demo/issues/new?template=store-setup.yml) · [Email Kamaljot Singh privately](mailto:kamal@mohiwalla.com)

## Try it offline

From this folder, run the commands below. The demo processes two pages of fictional sales and immediately replays them. The first pass should write two example rows. The replay should write none. It uses a temporary in-memory table.

```sh
python3 sync_sales.py --demo
python3 -m unittest -v
python3 acceptance_check.py
```

## Prepare the destination

Create a table with these field names and types. If you already have a sales table, remove duplicate sale IDs before pointing the sync at it. The sync updates the listed fields and preserves other columns, such as your follow-up notes.

| Airtable field | Type | Value |
| --- | --- | --- |
| Sale ID | Single line text, primary field | Gumroad's unique sale ID |
| Email | Email | Customer email from the sale |
| Transaction date | Date with time | Sale creation time in UTC. Set the column to display one timezone for all collaborators if you need consistent timestamps. |
| Sale amount USD | Currency, USD with two decimal places | Gumroad's sale price in USD cents divided by 100. Fees, refunds and payouts are separate figures. |
| Product ID | Single line text | Gumroad product ID |
| Displayed price | Single line text | Gumroad's formatted display price, which can use another currency |
| Can contact | Checkbox | Checked only when Gumroad explicitly returns true |
| Refund status | Single line text | Refunded, Partially refunded, No refund, or Unreported |

Use a Gumroad token with the `view_sales` scope and an Airtable personal access token with `data.records:write` scoped to the destination base. The token owner needs permission to create and edit rows in that table. The code sends only the listed fields to Airtable. It uses the API's USD price as reported and never multiplies it by quantity. [Gumroad sales endpoint](https://github.com/antiwork/gumroad/blob/main/app/controllers/api/v2/sales_controller.rb) · [Gumroad sale field definitions](https://github.com/antiwork/gumroad/blob/main/app/models/purchase.rb) · [Airtable upsert endpoint](https://airtable.com/developers/web/api/update-multiple-records)

Set these variables in the private environment of the account or scheduler that runs the script. Keep token values out of the project folder, source control and screenshots.

```text
GUMROAD_ACCESS_TOKEN
AIRTABLE_TOKEN
AIRTABLE_BASE_ID
AIRTABLE_TABLE_ID
```

The Airtable IDs start with `app` and `tbl`. Copy them from the table URL. Use a separate state file for each store and destination.

## Make the first transfer

1. Run `python3 sync_sales.py --dry-run` with your Gumroad token set. It reads the entire sale history, validates each row, prints counts and writes nothing. You can use the exact same mapping offline with `python3 sync_sales.py --dry-run --fixture acceptance_sales.json`.
2. Run `python3 sync_sales.py --apply` from the same account that will run the schedule. The first pass imports the full history. In Airtable, compare a sale ID, email, UTC sale time, USD price and product ID with a real historical sale. For a new store, check the first real sale once it appears in Gumroad's Sales API. An empty history only confirms the source responded.
3. Run `--apply` again. With unchanged source data, `upserted` should be zero and the count of Airtable rows should stay the same. For a later refund, compare the same row's refund status after a full pass.

Every subsequent run reads purchases created since the day before the last successful run. It skips rows whose mapped fields match the last successful write. An interrupted batch can be replayed because Airtable merges on Sale ID. The SQLite file saves sale IDs, hashes and the run date. Customer emails stay out of the state file and terminal output.

## Schedule it on your host

Save the four variable assignments in an owner-readable shell environment file, for example `/srv/sales-sync-private/settings.env`, with mode `0600`. Point a cron entry at your actual script, environment file and Python executable. These example entries run every 15 minutes and reconcile older sales every Sunday. Put the entries on one host and use the same state path for both.

```cron
*/15 * * * * (set -a; . /srv/sales-sync-private/settings.env && /usr/bin/python3 /srv/sales-sync/sync_sales.py --apply --state /srv/sales-sync-private/state.sqlite3)
7 3 * * 0 (set -a; . /srv/sales-sync-private/settings.env && /usr/bin/python3 /srv/sales-sync/sync_sales.py --apply --full --state /srv/sales-sync-private/state.sqlite3)
```

Use a full pass after a manual Airtable edit or deletion, and to pick up a later refund or contact preference change on an older sale. It rechecks and rewrites the entire history. On a large store, choose a reconciliation interval that fits your Airtable quota. Airtable accepts up to ten rows per request. [Airtable rate and plan limits](https://support.airtable.com/articles/7735693959-Managing-API-call-limits-in-Airtable)

A failed request exits with status 1. The next run can retry from the previous checkpoint. Check the scheduler's exit status or configure failure notifications on your host. A running process holds an SQLite write lock during transfer. Concurrent invocations using the same state file wait up to five seconds and then exit for the next scheduled run.
