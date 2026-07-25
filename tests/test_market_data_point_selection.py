import tempfile
import unittest
from pathlib import Path

from cb_terminal.storage.price_history_store import PriceHistoryStore


QUOTE_CSV = (
    "contract_id,isin,reference_security,date,time,dealer,security,market_price\n"
    "TEST_contract,XS0000000001,Test CB,2026-05-22,15:00,BVAL,TEST 0 01/01/31,101.5\n"
)
EQUITY_CSV = "date,instrument_id,value\n2026-05-22,1234 HK Equity,50\n"


class MarketDataPointSelectionTests(unittest.TestCase):
    def test_currency_scaled_equity_outlier_cannot_win_on_series_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            quote_path = root / "quotes.csv"
            wrong_unit_path = root / "broad-wrong-unit-equity.csv"
            local_a_path = root / "local-equity-a.csv"
            local_b_path = root / "local-equity-b.csv"
            quote_path.write_text(
                QUOTE_CSV
                + "TEST_contract,XS0000000001,Test CB,2026-05-23,15:00,BVAL,TEST 0 01/01/31,101.6\n",
                encoding="utf-8",
            )
            wrong_unit_path.write_text(
                "date,instrument_id,value\n"
                "2026-05-22,1234 HK Equity,6.40\n"
                "2026-05-23,1234 HK Equity,6.45\n",
                encoding="utf-8",
            )
            local_a_path.write_text(
                "date,instrument_id,value\n2026-05-22,1234 HK Equity,50.0\n",
                encoding="utf-8",
            )
            local_b_path.write_text(
                "date,instrument_id,value\n2026-05-22,1234 HK Equity,50.2\n",
                encoding="utf-8",
            )
            store = PriceHistoryStore(root / "prices.sqlite")
            store.import_file(quote_path)
            for path in (wrong_unit_path, local_a_path, local_b_path):
                store.import_market_data_file(path)

            rows = store.build_valuation_market_rows(
                cb_instrument_id="XS0000000001",
                equity_instrument_id="1234 HK Equity",
            )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["date"], "2026-05-22")
        self.assertIn(rows[0]["stock_price"], {50.0, 50.2})
        self.assertIn(
            "equity_price_outliers_excluded:1",
            rows[0]["cb_selection_reason"],
        )

    def test_target_contract_overrides_stale_upload_time_contract_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            quote_path = root / "quotes.csv"
            equity_path = root / "equity.csv"
            quote_path.write_text(
                QUOTE_CSV.replace("TEST_contract", "WRONG_SELECTED_contract"),
                encoding="utf-8",
            )
            equity_path.write_text(EQUITY_CSV, encoding="utf-8")
            store = PriceHistoryStore(root / "prices.sqlite")
            store.import_file(quote_path)
            store.import_market_data_file(equity_path)

            rows = store.build_valuation_market_rows(
                cb_instrument_id="XS0000000001",
                equity_instrument_id="1234 HK Equity",
                cb_contract_id="RIGHT_contract",
            )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cb_contract_id"], "RIGHT_contract")

    def test_broadest_fx_series_wins_overlap_independent_of_import_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            quote_path = root / "quotes.csv"
            equity_path = root / "equity.csv"
            dedicated_fx_path = root / "dedicated-fx.csv"
            combined_path = root / "combined-equity-and-fx.csv"
            quote_path.write_text(QUOTE_CSV, encoding="utf-8")
            equity_path.write_text(EQUITY_CSV, encoding="utf-8")
            dedicated_fx_path.write_text(
                "date,instrument_id,value\n"
                "2026-05-22,USDJPY Curncy,159.18\n"
                "2026-05-23,USDJPY Curncy,159.20\n",
                encoding="utf-8",
            )
            combined_path.write_text(
                "date,instrument_id,value\n2026-05-22,USDJPY Curncy,159.11\n",
                encoding="utf-8",
            )

            forward = self._build_rows(
                root / "forward.sqlite",
                quote_path,
                equity_path,
                [dedicated_fx_path, combined_path],
            )
            reverse = self._build_rows(
                root / "reverse.sqlite",
                quote_path,
                equity_path,
                [combined_path, dedicated_fx_path],
            )

        self.assertEqual(len(forward), 1)
        self.assertEqual(forward, reverse)
        self.assertAlmostEqual(forward[0]["market_fx_rate"], 159.18)

    def test_equal_coverage_conflict_uses_stable_provenance_tie_break(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            quote_path = root / "quotes.csv"
            equity_path = root / "equity.csv"
            first_fx_path = root / "first-fx.csv"
            second_fx_path = root / "second-fx.csv"
            quote_path.write_text(QUOTE_CSV, encoding="utf-8")
            equity_path.write_text(EQUITY_CSV, encoding="utf-8")
            first_fx_path.write_text(
                "date,instrument_id,value\n2026-05-22,USDJPY Curncy,159.18\n",
                encoding="utf-8",
            )
            second_fx_path.write_text(
                "date,instrument_id,value\n2026-05-22,USDJPY Curncy,159.11\n",
                encoding="utf-8",
            )

            forward = self._build_rows(
                root / "forward.sqlite",
                quote_path,
                equity_path,
                [first_fx_path, second_fx_path],
            )
            reverse = self._build_rows(
                root / "reverse.sqlite",
                quote_path,
                equity_path,
                [second_fx_path, first_fx_path],
            )

        self.assertEqual(forward, reverse)
        self.assertIn(forward[0]["market_fx_rate"], {159.11, 159.18})

    def test_cross_currency_rows_require_fx_on_the_exact_valuation_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            quote_path = root / "quotes.csv"
            equity_path = root / "equity.csv"
            fx_path = root / "fx.csv"
            quote_path.write_text(QUOTE_CSV, encoding="utf-8")
            equity_path.write_text(EQUITY_CSV, encoding="utf-8")
            fx_path.write_text(
                "date,instrument_id,value\n"
                "2026-05-23,USDHKD Curncy,7.84\n",
                encoding="utf-8",
            )
            store = PriceHistoryStore(root / "prices.sqlite")
            store.import_file(quote_path)
            store.import_market_data_file(equity_path)
            store.import_market_data_file(fx_path)

            rows = store.build_valuation_market_rows(
                cb_instrument_id="XS0000000001",
                equity_instrument_id="1234 HK Equity",
                fx_instrument_id="USDHKD Curncy",
                stock_currency="HKD",
                bond_price_currency="USD",
                fx_convention="STOCK_PER_CB",
            )

        self.assertEqual(rows, [])

    def test_deleted_market_source_cannot_continue_to_drive_valuation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            quote_path = root / "quotes.csv"
            equity_path = root / "equity.csv"
            quote_path.write_text(QUOTE_CSV, encoding="utf-8")
            equity_path.write_text(EQUITY_CSV, encoding="utf-8")
            store = PriceHistoryStore(root / "prices.sqlite")
            store.import_file(quote_path)
            store.import_market_data_file(equity_path)
            equity_path.unlink()

            rows = store.build_valuation_market_rows(
                cb_instrument_id="XS0000000001",
                equity_instrument_id="1234 HK Equity",
            )

        self.assertEqual(rows, [])

    @staticmethod
    def _build_rows(
        db_path: Path,
        quote_path: Path,
        equity_path: Path,
        fx_paths: list[Path],
    ) -> list[dict[str, object]]:
        store = PriceHistoryStore(db_path)
        store.import_file(quote_path)
        store.import_market_data_file(equity_path)
        for fx_path in fx_paths:
            store.import_market_data_file(fx_path)
        return store.build_valuation_market_rows(
            cb_instrument_id="XS0000000001",
            equity_instrument_id="1234 HK Equity",
            fx_instrument_id="USDJPY Curncy",
        )


if __name__ == "__main__":
    unittest.main()
