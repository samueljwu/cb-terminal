import csv
import os
import tempfile
import unittest
from datetime import date

from cb_terminal.domain import Assumptions, Contract, ConversionTerms, CouponSchedule, FXConvention, MarketRow
from cb_terminal.io.market_history import load_market_history_csv, parse_market_history_csv_text
from cb_terminal.pricing.batch import ResultRow, price_history, write_results_csv


def sample_contract() -> Contract:
    return Contract(
        id="phase2_cb",
        issuer="Phase 2 Issuer",
        description="Synthetic phase 2 convertible",
        currency="USD",
        settlement_currency="USD",
        stock_currency="USD",
        face=100.0,
        issue_price=100.0,
        maturity_price=100.0,
        pricing_date=date(2026, 1, 1),
        maturity_date=date(2027, 1, 1),
        coupon=CouponSchedule(annual_rate=0.0, frequency=0),
        conversion=ConversionTerms(underlying_ticker="TEST", conversion_price=50.0, fixed_fx_rate=1.0),
    )


class MarketHistoryCsvTests(unittest.TestCase):
    def test_parser_accepts_bloombergish_aliases_and_normalizes_rows(self):
        text = """Date,PX_LAST,CB Price,FX,Stock CCY,Bond CCY,Volatility,OAS (bp),Borrow Cost,Dividend Yield,Risk Free Rate\n2026-01-02,45.25,104.5,1.0,usd,USD,35%,275,1.5%,0.50%,4%\n"""
        rows = parse_market_history_csv_text(text)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.as_of_date, date(2026, 1, 2))
        self.assertEqual(row.stock_price, 45.25)
        self.assertEqual(row.bond_price, 104.5)
        self.assertEqual(row.market_fx_rate, 1.0)
        self.assertEqual(row.stock_currency, "USD")
        self.assertEqual(row.bond_price_currency, "USD")
        self.assertEqual(row.assumption_overrides["volatility"], 0.35)
        self.assertEqual(row.assumption_overrides["credit_spread"], 0.0275)
        self.assertEqual(row.assumption_overrides["borrow_rate"], 0.015)
        self.assertEqual(row.assumption_overrides["dividend_yield"], 0.005)
        self.assertEqual(row.assumption_overrides["risk_free_rate"], 0.04)

    def test_parser_ignores_blank_rows_and_supports_core_schema(self):
        text = """date,stock_price,bond_price,market_fx_rate,fx_convention\n2026-01-02,40,101,,\n,,,,\n2026-01-03,41,102,1.2,CB_PER_STOCK\n"""
        rows = parse_market_history_csv_text(text)
        self.assertEqual([row.as_of_date for row in rows], [date(2026, 1, 2), date(2026, 1, 3)])
        self.assertEqual(rows[0].market_fx_rate, 1.0)
        self.assertIsNone(rows[0].fx_convention)
        self.assertEqual(rows[1].fx_convention, FXConvention.CB_PER_STOCK)

    def test_parser_preserves_explicit_zero_fx_rate(self):
        rows = parse_market_history_csv_text("date,stock_price,market_fx_rate\n2026-01-02,40,0.0\n")
        self.assertEqual(rows[0].market_fx_rate, 0.0)

    def test_parser_rejects_ambiguous_slash_dates(self):
        with self.assertRaisesRegex(ValueError, "ambiguous slash date"):
            parse_market_history_csv_text("date,stock_price\n01/02/2026,40\n")

    def test_parser_accepts_unambiguous_slash_dates(self):
        us, eu = parse_market_history_csv_text("date,stock_price\n12/31/2026,40\n31/12/2026,41\n")
        self.assertEqual(us.as_of_date, date(2026, 12, 31))
        self.assertEqual(eu.as_of_date, date(2026, 12, 31))

    def test_parser_rejects_conflicting_duplicate_canonical_columns(self):
        with self.assertRaisesRegex(ValueError, "conflicting values"):
            parse_market_history_csv_text("date,stock_price,PX_LAST\n2026-01-02,40,41\n")

    def test_parser_rejects_conflicting_credit_spread_forms(self):
        with self.assertRaisesRegex(ValueError, "credit_spread"):
            parse_market_history_csv_text("date,stock_price,credit_spread,credit_spread_bps\n2026-01-02,40,0.02,300\n")

    def test_load_market_history_csv_from_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "history.csv")
            with open(path, "w", newline="", encoding="utf-8") as handle:
                handle.write("Date,Stock Price,CB Price\n2026-01-02,45,101\n")
            rows = load_market_history_csv(path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].stock_price, 45.0)

    def test_missing_required_columns_raise_clear_error(self):
        with self.assertRaisesRegex(ValueError, "required column"):
            parse_market_history_csv_text("Date,CB Price\n2026-01-02,101\n")


class BatchPricingTests(unittest.TestCase):
    def setUp(self):
        self.contract = sample_contract()
        self.defaults = Assumptions(
            volatility=0.30,
            risk_free_rate=0.03,
            credit_spread=0.02,
            borrow_rate=0.0,
            dividend_yield=0.0,
            steps=40,
            valuation_date=date(2026, 1, 1),
        )

    def test_price_history_applies_row_overrides_and_returns_result_rows(self):
        rows = parse_market_history_csv_text(
            """date,stock_price,bond_price,volatility,risk_free_rate\n2026-01-02,45,104,45%,3%\n2026-01-03,46,105,,\n"""
        )
        results = price_history(self.contract, rows, self.defaults)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(isinstance(row, ResultRow) for row in results))
        self.assertEqual(results[0].as_of_date, date(2026, 1, 2))
        self.assertEqual(results[0].source_row, 2)
        self.assertEqual(results[0].assumption_source, "row_override")
        self.assertEqual(results[1].assumption_source, "defaults")
        self.assertEqual(results[0].valuation_date, date(2026, 1, 2))
        self.assertEqual(results[0].volatility, 0.45)
        self.assertEqual(results[0].risk_free_rate, 0.03)
        self.assertEqual(results[0].credit_spread, self.defaults.credit_spread)
        self.assertEqual(results[0].steps, self.defaults.steps)
        self.assertGreater(results[0].fair_value, 0.0)
        self.assertGreater(results[0].parity, 0.0)
        self.assertIsNotNone(results[0].cheapness)
        self.assertIsNotNone(results[0].implied_volatility)
        self.assertGreaterEqual(results[0].warning_count, 0)

    def test_batch_captures_implied_vol_errors_as_warnings(self):
        rows = parse_market_history_csv_text("date,stock_price,bond_price\n2026-01-02,45,10000\n")
        results = price_history(self.contract, rows, self.defaults)
        self.assertEqual(len(results), 1)
        self.assertIsNone(results[0].implied_volatility)
        self.assertGreaterEqual(results[0].warning_count, 1)
        self.assertIn("implied_volatility", results[0].warnings)

    def test_write_results_csv_uses_stable_schema(self):
        rows = parse_market_history_csv_text("date,stock_price,bond_price\n2026-01-02,45,104\n")
        results = price_history(self.contract, rows, self.defaults)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "results.csv")
            write_results_csv(results, path)
            with open(path, newline="", encoding="utf-8") as handle:
                records = list(csv.DictReader(handle))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["date"], "2026-01-02")
        self.assertIn("fair_value", records[0])
        self.assertIn("implied_volatility", records[0])
        self.assertEqual(records[0]["source_row"], "2")
        self.assertEqual(records[0]["valuation_date"], "2026-01-02")
        self.assertIn("volatility", records[0])
        self.assertIn("risk_free_rate", records[0])
        self.assertIn("credit_spread", records[0])
        self.assertIn("borrow_rate", records[0])
        self.assertIn("dividend_yield", records[0])
        self.assertIn("steps", records[0])

    def test_batch_continues_after_pricing_error_and_reports_row_warning(self):
        rows = [
            MarketRow(as_of_date=date(2026, 1, 2), stock_price=45.0, market_fx_rate=0.0, source_row=2),
            MarketRow(as_of_date=date(2026, 1, 3), stock_price=46.0, source_row=3),
        ]
        results = price_history(self.contract, rows, self.defaults)
        self.assertEqual(len(results), 2)
        self.assertIsNone(results[0].fair_value)
        self.assertIsNone(results[0].parity)
        self.assertIsNone(results[0].bond_floor)
        self.assertGreaterEqual(results[0].warning_count, 1)
        self.assertIn("pricing_error:", results[0].warnings)
        self.assertIn("fx_rate", results[0].error)
        self.assertGreater(results[1].fair_value, 0.0)


if __name__ == "__main__":
    unittest.main()
