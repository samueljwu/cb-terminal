import os
import tempfile
import unittest
from datetime import date, time
from pathlib import Path

from cb_terminal.io.price_history import load_price_history_file, parse_price_history_csv_text
from cb_terminal.storage.price_history_store import PriceHistoryStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ISSUER_ANCHOR_CSV = PROJECT_ROOT / "tests" / "fixtures" / "issuer_cb_price_history_anchor.csv"
RAW_EXPORT_GLOBS = [
    PROJECT_ROOT / "data" / "price_history" / "raw",
]


class PriceHistoryIngestionTests(unittest.TestCase):
    def test_parser_accepts_csv_aliases_and_computes_mid(self):
        rows = parse_price_history_csv_text(
            "Reference Security,Date,Time,Dealer,Source,Bid Price,Ask Price,Subject\n"
            "ABC Corp,05/22/26,13:35,BAML,IB,104.125,104.625,quoted level\n",
            instrument_id="abc_cb",
            contract_id="abc_contract",
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.reference_security, "ABC Corp")
        self.assertEqual(row.instrument_id, "abc_cb")
        self.assertEqual(row.contract_id, "abc_contract")
        self.assertEqual(row.as_of_date, date(2026, 5, 22))
        self.assertEqual(row.as_of_time, time(13, 35))
        self.assertEqual(row.dealer, "BAML")
        self.assertEqual(row.mid_price, 104.375)

    def test_clean_issuer_anchor_csv_is_parseable(self):
        rows = load_price_history_file(ISSUER_ANCHOR_CSV)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].contract_id, "XS3236970433_contract")
        self.assertEqual(rows[0].instrument_id, "XS3236970433")
        self.assertEqual(rows[0].reference_security, "DH3633418 Corp")
        self.assertEqual(rows[0].security, "WIVYNN 0 04/01/31")
        self.assertEqual(rows[0].as_of_date, date(2026, 5, 22))
        self.assertAlmostEqual(rows[0].mid_price, 135.758)

    def test_canonical_csv_can_supply_explicit_market_price_without_bid_ask(self):
        rows = parse_price_history_csv_text(
            "contract_id,isin,reference_security,date,time,dealer,security,market_price\n"
            "XS3236970433_contract,XS3236970433,DH3633418 Corp,2026-05-22,15:00,BVAL,WIVYNN 0 04/01/31,135.758\n",
        )
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].bid_price)
        self.assertIsNone(rows[0].ask_price)
        self.assertAlmostEqual(rows[0].mid_price, 135.758)
        self.assertEqual(rows[0].instrument_id, "XS3236970433")
        self.assertEqual(rows[0].reference_security, "DH3633418 Corp")
        self.assertEqual(rows[0].contract_id, "XS3236970433_contract")

    def test_raw_private_price_history_exports_are_not_shipped(self):
        for root in RAW_EXPORT_GLOBS:
            if root.exists():
                shipped_exports = [path for path in root.iterdir() if path.is_file()]
                self.assertEqual(shipped_exports, [], f"raw user exports should not be committed: {shipped_exports}")


class PriceHistoryStoreTests(unittest.TestCase):
    def test_import_file_creates_dedicated_quote_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "price_history.sqlite")
            store = PriceHistoryStore(db_path)
            batch = store.import_file(ISSUER_ANCHOR_CSV, notes="unit test import")
            self.assertEqual(batch.row_count, 1)
            self.assertEqual(len(batch.source_sha256), 64)
            self.assertEqual(store.quote_count(instrument_id="XS3236970433"), batch.row_count)
            latest = store.latest_quotes(instrument_id="XS3236970433", limit=3)
            date_range = store.quote_date_range(instrument_id="XS3236970433")
        self.assertEqual(date_range, {"count": 1, "first_date": "2026-05-22", "latest_date": "2026-05-22"})
        self.assertEqual(len(latest), 1)
        self.assertEqual(latest[0]["instrument_id"], "XS3236970433")
        self.assertEqual(latest[0]["reference_security"], "DH3633418 Corp")
        self.assertIn("raw", latest[0])
        self.assertAlmostEqual(latest[0]["raw"]["mid_price"], 135.758)

    def test_reimport_is_idempotent_for_same_source_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "price_history.sqlite")
            store = PriceHistoryStore(db_path)
            first = store.import_file(ISSUER_ANCHOR_CSV)
            second = store.import_file(ISSUER_ANCHOR_CSV)
            self.assertEqual(first.row_count, second.row_count)
            self.assertEqual(store.quote_count(instrument_id="XS3236970433"), first.row_count)

    def test_import_registers_stable_instrument_identity_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "price_history.sqlite")
            store = PriceHistoryStore(db_path)
            store.import_file(ISSUER_ANCHOR_CSV)

            identities = store.instrument_identities(instrument_type="convertible_bond")
            latest = store.latest_quotes(instrument_key="cb:isin:XS3236970433", limit=3)

        self.assertEqual(len(identities), 1)
        self.assertEqual(identities[0]["instrument_key"], "cb:isin:XS3236970433")
        self.assertEqual(identities[0]["primary_id_scheme"], "ISIN")
        self.assertEqual(identities[0]["primary_id"], "XS3236970433")
        self.assertEqual(latest[0]["instrument_key"], "cb:isin:XS3236970433")
        self.assertEqual(latest[0]["primary_id"], "XS3236970433")


if __name__ == "__main__":
    unittest.main()
