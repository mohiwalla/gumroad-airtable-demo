"""Exercise the sales sync with a fictional store and an in-memory Airtable stand-in.

Run python3 acceptance_check.py. No account or network connection is used.
"""

import json
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from sync_sales import init_state, run_sync


FIXTURE = Path(__file__).with_name("acceptance_sales.json")


class SalesSyncAcceptance(unittest.TestCase):
    def setUp(self):
        self.pages = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        init_state(self.db, "fictional-store/fictional-sales-table")
        self.rows = {}
        self.sent = []

        # A misplaced live API call must fail even on a machine with credentials.
        network = patch("socket.socket.connect", side_effect=AssertionError("Unexpected network access"))
        network.start()
        self.addCleanup(network.stop)

    def upsert(self, rows):
        for row in rows:
            key = row["Sale ID"]
            self.rows[key] = row.copy()
            self.sent.append(key)

    def sync(self, writer=None, full=False):
        pages = (page["sales"] for page in self.pages)
        return run_sync(pages, writer or self.upsert, self.db, full=full)

    def test_sales_then_repeat_import(self):
        self.sync()
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(set(self.rows), {"demo-sale-001", "demo-sale-002", "demo-sale-003"})
        expected = {
            "demo-sale-001": ("river@example.invalid", "demo-product-checklist", 14.99, True),
            "demo-sale-002": ("sage@example.invalid", "demo-product-checklist", 0, True),
            "demo-sale-003": ("river@example.invalid", "demo-product-guide", 37, False),
        }
        for key, (email, product, amount, consent) in expected.items():
            row = self.rows[key]
            self.assertEqual(row["Email"], email)
            self.assertEqual(row["Product ID"], product)
            self.assertEqual(row["Sale amount USD"], amount)
            self.assertIs(row["Can contact"], consent)
        self.assertEqual(
            self.rows["demo-sale-002"]["Transaction date"],
            self.rows["demo-sale-003"]["Transaction date"],
        )
        self.assertTrue(str(self.rows["demo-sale-001"]["Transaction date"]).startswith("2026-11-16"))
        self.sent.clear()
        self.sync()
        self.assertEqual(self.sent, [])

    def test_refund_updates_existing_sale(self):
        self.sync()
        sale = self.pages[1]["sales"][0]
        original_status = self.rows[sale["id"]]["Refund status"]
        self.sent.clear()
        sale["refunded"] = True
        self.sync(full=True)
        self.assertEqual(len(self.rows), 3)
        self.assertIn(sale["id"], self.sent)
        self.assertNotEqual(self.rows[sale["id"]]["Refund status"], original_status)

    def test_retry_after_remote_write_with_missing_acknowledgment(self):
        def lost_response(rows):
            self.upsert(rows)
            raise ConnectionError("Simulated disconnect after remote write")

        with self.assertRaises(ConnectionError):
            self.sync(lost_response)
        self.assertTrue(self.rows, "The destination received the write before the simulated outage")
        self.sent.clear()
        self.sync()
        self.assertEqual(len(self.rows), 3)
        self.assertTrue(self.sent)
        self.sent.clear()
        self.sync()
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
