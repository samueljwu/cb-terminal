import csv
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path

from cb_terminal.domain import (
    Assumptions,
    CallSchedule,
    Contract,
    ConversionTerms,
    CouponSchedule,
    MarketRow,
    PutSchedule,
)
from cb_terminal.pricing.batch import (
    RESULT_FIELDNAMES,
    ResultRow,
    price_history,
    write_results_csv,
)
from cb_terminal.pricing.yields import calculate_market_yields, issuance_yield_checks
from cb_terminal.web.server import _batch_yield_summary, _result_to_api_row


CLOSING_DATE = date(2026, 1, 1)


def zero_coupon_contract(
    *,
    maturity_date: date = date(2027, 1, 1),
    issue_price: float = 100.0,
    quoted_ytm: float | None = None,
    puts: list[PutSchedule] | None = None,
) -> Contract:
    return Contract(
        id="yield_test_cb",
        issuer="Yield Test Issuer",
        description="Synthetic zero-coupon convertible",
        currency="USD",
        settlement_currency="USD",
        stock_currency="USD",
        face=100.0,
        issue_price=issue_price,
        maturity_price=100.0,
        pricing_date=date(2025, 12, 20),
        issue_date=CLOSING_DATE,
        maturity_date=maturity_date,
        coupon=CouponSchedule(annual_rate=0.0, frequency=0),
        conversion=ConversionTerms(
            underlying_ticker="YIELD",
            conversion_price=50.0,
            fixed_fx_rate=1.0,
        ),
        puts=list(puts or []),
        yield_to_maturity=quoted_ytm,
        yield_to_maturity_frequency=2,
        day_count="ACT/365",
        quote_convention="dirty",
    )


def sample_result_row() -> ResultRow:
    return ResultRow(
        as_of_date=date(2026, 8, 15),
        stock_price=42.0,
        bond_price=97.5,
        market_fx_rate=1.0,
        fair_value=101.0,
        parity=84.0,
        bond_floor=93.0,
        cheapness=3.5,
        implied_volatility=0.31,
        yield_to_maturity=0.0525,
        yield_to_put=0.04125,
        yield_to_put_date=date(2028, 1, 1),
        yield_accrued_interest=0.5,
        yield_dirty_price=98.0,
        yield_price_basis="clean",
        yield_warning="put payoff assumed to include accrued coupon interest",
        output_currency="USD",
        warning_count=1,
        warnings="yield assumption",
        source="market.csv",
        source_row=2,
        assumption_source="defaults",
        valuation_date=date(2026, 8, 15),
        volatility=0.30,
        risk_free_rate=0.03,
        credit_spread=0.02,
        borrow_rate=0.01,
        dividend_yield=0.005,
        steps=100,
        model_version="tf_split_tree:v2",
    )


class IssuanceYieldTests(unittest.TestCase):
    def test_exact_negative_issuance_ytm_uses_closing_date(self):
        contract = replace(
            zero_coupon_contract(
                maturity_date=date(2027, 6, 21),
                issue_price=102.0,
                quoted_ytm=-0.0198,
            ),
            pricing_date=date(2026, 6, 15),
            issue_date=date(2026, 6, 23),
            day_count="ACT/365.25",
        )

        checks = issuance_yield_checks(contract)
        ytm = checks["yield_to_maturity"]

        self.assertEqual(checks["settlement_date"], "2026-06-23")
        self.assertEqual(
            ytm["calculation"]["settlement_date"],
            "2026-06-23",
        )
        self.assertEqual(ytm["quoted_yield"], -0.0198)
        self.assertAlmostEqual(ytm["calculated_yield"], -0.019826444549, places=11)
        self.assertAlmostEqual(ytm["difference_bps"], -0.26444549, places=6)
        self.assertEqual(ytm["status"], "match")

    def test_positive_issuance_ytm_control(self):
        contract = replace(
            zero_coupon_contract(
                maturity_date=date(2027, 7, 14),
                issue_price=100.0,
                quoted_ytm=0.0275,
            ),
            issue_date=date(2026, 7, 16),
            maturity_price=102.75,
            day_count="ACT/365.25",
        )

        ytm = issuance_yield_checks(contract)["yield_to_maturity"]

        self.assertGreater(ytm["calculated_yield"], 0.0)
        self.assertAlmostEqual(ytm["calculated_yield"], 0.027483949755, places=11)
        self.assertAlmostEqual(ytm["difference_bps"], -0.16050245, places=6)
        self.assertEqual(ytm["status"], "match")


class MarketYieldTests(unittest.TestCase):
    def test_day_count_aliases_and_unsupported_variants_are_not_silent(self):
        fixed = calculate_market_yields(
            replace(zero_coupon_contract(), day_count="ACT/365 (Fixed)"),
            price=100.0,
            settlement_date=CLOSING_DATE,
        )
        unknown = calculate_market_yields(
            replace(zero_coupon_contract(), day_count="foo"),
            price=100.0,
            settlement_date=CLOSING_DATE,
        )
        icma = calculate_market_yields(
            replace(
                zero_coupon_contract(),
                coupon=CouponSchedule(annual_rate=0.05, frequency=2),
                day_count="Actual/Actual (ICMA)",
            ),
            price=100.0,
            settlement_date=CLOSING_DATE,
        )

        self.assertEqual(
            fixed["yield_to_maturity_detail"]["day_count"],
            "ACT/365",
        )
        self.assertEqual(fixed["warning"], "")
        self.assertEqual(
            unknown["yield_to_maturity_detail"]["day_count"],
            "ACT/365.25",
        )
        self.assertIn("unrecognized day-count", unknown["warning"])
        self.assertIn("ACT/ACT ICMA approximated", icma["warning"])

    def test_inferred_coupon_schedule_preserves_maturity_month_end_without_drift(self):
        contract = replace(
            zero_coupon_contract(maturity_date=date(2030, 8, 31)),
            pricing_date=date(2028, 8, 20),
            issue_date=date(2028, 8, 31),
            coupon=CouponSchedule(annual_rate=0.05, frequency=2),
            day_count="30/360",
        )

        calculated = calculate_market_yields(
            contract,
            price=100.0,
            settlement_date=date(2028, 8, 31),
        )
        cash_flow_dates = [
            item["date"]
            for item in calculated["yield_to_maturity_detail"]["cash_flows"]
        ]

        self.assertEqual(
            cash_flow_dates,
            ["2029-02-28", "2029-08-31", "2030-02-28", "2030-08-31"],
        )
        self.assertIn("coupon dates", calculated["warning"])
        self.assertEqual(
            calculated["yield_to_maturity_detail"]["status"],
            "calculated_with_assumptions",
        )

    def test_long_dated_coupon_bond_does_not_underflow_at_negative_bracket(self):
        contract = replace(
            zero_coupon_contract(maturity_date=date(2056, 1, 1)),
            coupon=CouponSchedule(annual_rate=0.05, frequency=2),
            day_count="30/360",
        )

        calculated = calculate_market_yields(
            contract,
            price=100.0,
            settlement_date=CLOSING_DATE,
        )

        self.assertAlmostEqual(calculated["yield_to_maturity"], 0.05, places=12)

    def test_market_ytm_is_calculated_only_when_bond_price_exists(self):
        contract = zero_coupon_contract()

        missing = calculate_market_yields(
            contract,
            price=None,
            settlement_date=CLOSING_DATE,
        )
        quoted = calculate_market_yields(
            contract,
            price=100.0 / (1.0 + 0.04 / 2.0) ** 2,
            settlement_date=CLOSING_DATE,
            same_day_settlement_assumed=True,
        )

        self.assertIsNone(missing["yield_to_maturity"])
        self.assertIsNone(missing["yield_to_maturity_detail"])
        self.assertIn("bond price is required", missing["warning"])
        self.assertAlmostEqual(quoted["yield_to_maturity"], 0.04, places=12)
        self.assertIsNotNone(quoted["yield_to_maturity_detail"])
        self.assertTrue(quoted["same_day_settlement_assumed"])
        self.assertIn("same-day settlement", quoted["warning"])

    def test_negligible_market_ytm_is_zero_but_larger_yields_are_preserved(self):
        contract = zero_coupon_contract()

        for target_yield in (0.00005465, -0.00005465):
            with self.subTest(target_yield=target_yield):
                price = 100.0 / (1.0 + target_yield / 2.0) ** 2
                calculated = calculate_market_yields(
                    contract,
                    price=price,
                    settlement_date=CLOSING_DATE,
                )

                self.assertEqual(calculated["yield_to_maturity"], 0.0)
                self.assertEqual(
                    calculated["yield_to_maturity_detail"]["annual_yield"],
                    0.0,
                )
                self.assertEqual(
                    calculated["yield_to_maturity_detail"]["annual_yield_percent"],
                    0.0,
                )

        material_yield = 0.00015
        material_price = 100.0 / (1.0 + material_yield / 2.0) ** 2
        material = calculate_market_yields(
            contract,
            price=material_price,
            settlement_date=CLOSING_DATE,
        )

        self.assertAlmostEqual(
            material["yield_to_maturity"],
            material_yield,
            places=12,
        )
        self.assertAlmostEqual(
            material["yield_to_maturity_detail"]["annual_yield_percent"],
            material_yield * 100.0,
            places=10,
        )

    def test_earliest_future_scheduled_put_is_selected_and_rolls_forward(self):
        first_put = PutSchedule(
            put_type="holder_put",
            model_type="scheduled_put",
            price=101.0,
            date=date(2027, 1, 1),
        )
        second_put = PutSchedule(
            put_type="holder_put",
            model_type="scheduled_put",
            price=103.0,
            date=date(2028, 1, 1),
        )
        expired_put = PutSchedule(
            put_type="holder_put",
            model_type="scheduled_put",
            price=100.0,
            date=date(2025, 1, 1),
        )
        event_put = PutSchedule(
            put_type="change_of_control",
            model_type="event_put",
            price=110.0,
            date=date(2026, 6, 1),
        )
        contract = zero_coupon_contract(
            maturity_date=date(2030, 1, 1),
            puts=[event_put, second_put, expired_put, first_put],
        )

        before_first = calculate_market_yields(
            contract,
            price=99.0,
            settlement_date=CLOSING_DATE,
        )
        on_first = calculate_market_yields(
            contract,
            price=99.0,
            settlement_date=first_put.date,
        )

        self.assertEqual(before_first["yield_to_put_date"], "2027-01-01")
        self.assertEqual(
            [item["target_date"] for item in before_first["yield_to_puts"]],
            ["2027-01-01", "2028-01-01"],
        )
        self.assertEqual(on_first["yield_to_put_date"], "2028-01-01")
        self.assertEqual(
            [item["target_date"] for item in on_first["yield_to_puts"]],
            ["2028-01-01"],
        )

    def test_calls_and_conversion_terms_do_not_change_cashflow_yields(self):
        put = PutSchedule(
            put_type="holder_put",
            model_type="scheduled_put",
            price=102.0,
            date=date(2028, 1, 1),
        )
        plain = replace(
            zero_coupon_contract(
                maturity_date=date(2030, 1, 1),
                puts=[put],
            ),
            coupon=CouponSchedule(annual_rate=0.04, frequency=2),
            quote_convention="clean",
        )
        option_heavy = replace(
            plain,
            conversion=ConversionTerms(
                underlying_ticker="OTHER",
                conversion_price=1.0,
                start_date=CLOSING_DATE,
                end_date=date(2029, 12, 31),
                fixed_fx_rate=7.8,
                windows=((date(2027, 1, 1), date(2027, 6, 30)),),
            ),
            calls=[
                CallSchedule(
                    call_type="issuer_soft_call",
                    price=90.0,
                    start_date=date(2026, 2, 1),
                    trigger_ratio=1.01,
                )
            ],
        )

        plain_yields = calculate_market_yields(
            plain,
            price=97.5,
            settlement_date=date(2026, 8, 15),
        )
        option_heavy_yields = calculate_market_yields(
            option_heavy,
            price=97.5,
            settlement_date=date(2026, 8, 15),
        )

        self.assertEqual(option_heavy_yields, plain_yields)

    def test_batch_rows_add_market_yield_without_requiring_it_for_pricing(self):
        contract = zero_coupon_contract()
        rows = [
            MarketRow(
                as_of_date=CLOSING_DATE,
                stock_price=40.0,
                bond_price=None,
            ),
            MarketRow(
                as_of_date=date(2026, 1, 2),
                stock_price=40.0,
                bond_price=100.0,
            ),
        ]
        assumptions = Assumptions(
            volatility=0.30,
            risk_free_rate=0.03,
            credit_spread=0.02,
            steps=20,
        )

        results = price_history(contract, rows, assumptions)

        self.assertEqual(len(results), 2)
        self.assertIsNotNone(results[0].fair_value)
        self.assertIsNone(results[0].yield_to_maturity)
        self.assertIsNotNone(results[1].fair_value)
        self.assertIsNotNone(results[1].yield_to_maturity)
        self.assertIn("same-day settlement", results[1].yield_warning)


class ResultRowCsvTests(unittest.TestCase):
    def test_api_row_and_dashboard_summary_keep_yield_fields(self):
        result = sample_result_row()
        zero_ytm_result = replace(result, yield_to_maturity=0.0)

        api_row = _result_to_api_row(result)
        summary = _batch_yield_summary(
            zero_coupon_contract(quoted_ytm=0.0275),
            [result],
            {
                "yield_to_maturity": {
                    "calculated_yield": 0.02749,
                    "difference_bps": -0.1,
                    "status": "match",
                }
            },
        )
        zero_ytm_summary = _batch_yield_summary(
            zero_coupon_contract(),
            [zero_ytm_result],
            {},
        )

        self.assertEqual(api_row["yield_to_maturity"], 0.0525)
        self.assertEqual(
            _result_to_api_row(zero_ytm_result)["yield_to_maturity"],
            0.0,
        )
        self.assertEqual(api_row["yield_to_put"], 0.04125)
        self.assertEqual(api_row["yield_to_put_date"], "2028-01-01")
        self.assertEqual(api_row["yield_accrued_interest"], 0.5)
        self.assertEqual(api_row["yield_dirty_price"], 98.0)
        self.assertEqual(api_row["yield_price_basis"], "clean")
        self.assertEqual(
            api_row["yield_warning"],
            "put payoff assumed to include accrued coupon interest",
        )
        self.assertEqual(summary["latest_yield_to_maturity"], 0.0525)
        self.assertEqual(summary["latest_yield_to_put"], 0.04125)
        self.assertEqual(summary["latest_yield_to_put_date"], "2028-01-01")
        self.assertEqual(summary["quoted_issue_yield_to_maturity"], 0.0275)
        self.assertEqual(summary["calculated_issue_yield_to_maturity"], 0.02749)
        self.assertEqual(summary["issue_yield_difference_bps"], -0.1)
        self.assertEqual(summary["issue_yield_status"], "match")
        self.assertEqual(zero_ytm_summary["latest_yield_to_maturity"], 0.0)

    def test_csv_contains_result_row_yield_fields(self):
        result = sample_result_row()

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "results.csv"
            write_results_csv([result], path)
            with path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(
            [
                field
                for field in RESULT_FIELDNAMES
                if field.startswith("yield_")
            ],
            [
                "yield_to_maturity",
                "yield_to_put",
                "yield_to_put_date",
                "yield_accrued_interest",
                "yield_dirty_price",
                "yield_price_basis",
                "yield_warning",
            ],
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["yield_to_maturity"], "0.0525")
        self.assertEqual(rows[0]["yield_to_put"], "0.04125")
        self.assertEqual(rows[0]["yield_to_put_date"], "2028-01-01")
        self.assertEqual(rows[0]["yield_accrued_interest"], "0.5")
        self.assertEqual(rows[0]["yield_dirty_price"], "98")
        self.assertEqual(rows[0]["yield_price_basis"], "clean")
        self.assertEqual(
            rows[0]["yield_warning"],
            "put payoff assumed to include accrued coupon interest",
        )


if __name__ == "__main__":
    unittest.main()
