import math
import unittest
from dataclasses import replace
from datetime import date

from cb_terminal.domain import (
    Assumptions,
    CallSchedule,
    Contract,
    ConversionTerms,
    CouponSchedule,
    FXConvention,
    MarketSnapshot,
    PutSchedule,
)
from cb_terminal.pricing import PricingEngine, norm_cdf


VALUATION_DATE = date(2026, 1, 1)


def contract_with(
    *,
    maturity_date: date = date(2031, 1, 1),
    coupon_rate: float = 0.0,
    coupon_frequency: int = 0,
    conversion_price: float = 50.0,
    conversion_start: date | None = None,
    conversion_end: date | None = None,
    puts: list[PutSchedule] | None = None,
    calls: list[CallSchedule] | None = None,
) -> Contract:
    return Contract(
        id="robustness_cb",
        issuer="Robustness Issuer",
        description="synthetic robustness convertible",
        currency="USD",
        settlement_currency="USD",
        stock_currency="USD",
        face=100.0,
        issue_price=100.0,
        maturity_price=100.0,
        pricing_date=VALUATION_DATE,
        maturity_date=maturity_date,
        coupon=CouponSchedule(annual_rate=coupon_rate, frequency=coupon_frequency),
        conversion=ConversionTerms(
            underlying_ticker="ROBUST",
            conversion_price=conversion_price,
            start_date=conversion_start,
            end_date=conversion_end,
            fixed_fx_rate=1.0,
        ),
        puts=list(puts or []),
        calls=list(calls or []),
    )


def assumptions_with(**updates: float | int | date) -> Assumptions:
    values = {
        "volatility": 0.30,
        "risk_free_rate": 0.03,
        "credit_spread": 0.02,
        "borrow_rate": 0.005,
        "dividend_yield": 0.01,
        "steps": 250,
        "valuation_date": VALUATION_DATE,
    }
    values.update(updates)
    return Assumptions(**values)


class PricingBenchmarkTests(unittest.TestCase):
    def test_production_default_is_tf_split_tree(self):
        self.assertEqual(PricingEngine().model_mode, "tf_split_tree")

    def test_maturity_only_tf_tree_converges_to_closed_form_split_value(self):
        maturity = date(2031, 1, 1)
        contract = contract_with(
            maturity_date=maturity,
            conversion_start=maturity,
            conversion_end=maturity,
        )
        assumptions = assumptions_with(credit_spread=0.04, steps=400)
        spot = 45.0
        maturity_years = (maturity - VALUATION_DATE).days / 365.25
        carry = assumptions.equity_carry
        sigma_t = assumptions.volatility * math.sqrt(maturity_years)
        d1 = (
            math.log(spot / contract.conversion.conversion_price)
            + (carry + 0.5 * assumptions.volatility**2) * maturity_years
        ) / sigma_t
        d2 = d1 - sigma_t
        conversion_ratio = contract.face / contract.conversion.conversion_price
        expected = (
            contract.maturity_price
            * math.exp(-(assumptions.risk_free_rate + assumptions.credit_spread) * maturity_years)
            * norm_cdf(-d2)
            + conversion_ratio
            * spot
            * math.exp(-(assumptions.dividend_yield + assumptions.borrow_rate) * maturity_years)
            * norm_cdf(d1)
        )

        result = PricingEngine().price(contract, MarketSnapshot(stock_price=spot), assumptions)

        self.assertAlmostEqual(result.fair_value, expected, delta=0.03)
        self.assertEqual(result.diagnostics.details["model_version"], "tf_split_tree:v2")
        self.assertAlmostEqual(
            result.diagnostics.details["cash_component"] + result.diagnostics.details["equity_component"],
            result.fair_value,
            places=10,
        )

    def test_zero_volatility_uses_the_deterministic_carry_limit(self):
        maturity = date(2031, 1, 1)
        contract = contract_with(
            maturity_date=maturity,
            conversion_start=maturity,
            conversion_end=maturity,
        )
        assumptions = assumptions_with(
            volatility=0.0,
            risk_free_rate=0.08,
            credit_spread=0.0,
            borrow_rate=0.0,
            dividend_yield=0.0,
            steps=10,
        )
        maturity_years = (maturity - VALUATION_DATE).days / 365.25
        terminal_stock = 45.0 * math.exp(assumptions.equity_carry * maturity_years)
        expected = max(100.0, 2.0 * terminal_stock) * math.exp(-assumptions.risk_free_rate * maturity_years)

        result = PricingEngine().price(contract, MarketSnapshot(stock_price=45.0), assumptions)

        self.assertAlmostEqual(result.fair_value, expected, places=8)
        self.assertEqual(result.diagnostics.details["lattice_method"], "deterministic")
        self.assertFalse(any("clipped" in warning for warning in result.diagnostics.warnings))

    def test_invalid_crr_probability_uses_a_martingale_preserving_fallback(self):
        maturity = date(2031, 1, 1)
        contract = contract_with(
            maturity_date=maturity,
            conversion_start=maturity,
            conversion_end=maturity,
        )
        assumptions = assumptions_with(
            volatility=0.005,
            risk_free_rate=0.08,
            credit_spread=0.0,
            borrow_rate=0.0,
            dividend_yield=0.0,
            steps=10,
        )

        result = PricingEngine().price(contract, MarketSnapshot(stock_price=45.0), assumptions)

        self.assertTrue(math.isfinite(result.fair_value))
        self.assertEqual(result.diagnostics.details["lattice_method"], "drift_adjusted_equal_probability")
        self.assertAlmostEqual(result.diagnostics.details["risk_neutral_probability"], 0.5)
        self.assertFalse(any("clipped" in warning for warning in result.diagnostics.warnings))


class CouponAndExerciseTests(unittest.TestCase):
    def test_maturity_date_includes_final_coupon_and_same_day_put(self):
        coupon_contract = contract_with(
            maturity_date=VALUATION_DATE,
            coupon_rate=0.12,
            coupon_frequency=1,
            conversion_price=1_000.0,
        )
        assumptions = assumptions_with(
            volatility=0.0,
            risk_free_rate=0.0,
            credit_spread=0.0,
            borrow_rate=0.0,
            dividend_yield=0.0,
            steps=1,
        )

        coupon_result = PricingEngine().price(
            coupon_contract,
            MarketSnapshot(stock_price=1.0),
            assumptions,
        )
        put_result = PricingEngine().price(
            replace(
                coupon_contract,
                puts=[PutSchedule(put_type="holder_put", price=120.0, date=VALUATION_DATE)],
            ),
            MarketSnapshot(stock_price=1.0),
            assumptions,
        )

        self.assertAlmostEqual(coupon_result.fair_value, 112.0, places=8)
        self.assertAlmostEqual(coupon_result.bond_floor, 112.0, places=8)
        self.assertAlmostEqual(put_result.fair_value, 120.0, places=8)
        self.assertAlmostEqual(put_result.bond_floor, 120.0, places=8)

    def test_maturity_coupon_remains_after_valuation_moves_off_issue_anniversary(self):
        contract = contract_with(
            maturity_date=date(2027, 1, 1),
            coupon_rate=0.12,
            coupon_frequency=1,
            conversion_price=1_000.0,
        )
        assumptions = assumptions_with(
            volatility=0.0,
            risk_free_rate=0.0,
            credit_spread=0.0,
            borrow_rate=0.0,
            dividend_yield=0.0,
            valuation_date=date(2026, 1, 5),
            steps=12,
        )

        result = PricingEngine().price(contract, MarketSnapshot(stock_price=1.0), assumptions)

        self.assertAlmostEqual(result.bond_floor, 112.0, places=8)
        self.assertAlmostEqual(result.fair_value, 112.0, places=8)

    def test_coarse_tree_does_not_collapse_multiple_coupons(self):
        contract = contract_with(
            maturity_date=date(2031, 1, 1),
            coupon_rate=0.02,
            coupon_frequency=2,
            conversion_price=1_000.0,
        )
        assumptions = assumptions_with(
            volatility=0.0,
            risk_free_rate=0.0,
            credit_spread=0.0,
            borrow_rate=0.0,
            dividend_yield=0.0,
            steps=1,
        )

        result = PricingEngine().price(contract, MarketSnapshot(stock_price=1.0), assumptions)

        self.assertAlmostEqual(result.bond_floor, 110.0, places=8)
        self.assertAlmostEqual(result.fair_value, 110.0, places=8)

    def test_maturity_coupon_is_not_added_after_holder_converts(self):
        maturity = date(2027, 1, 1)
        contract = contract_with(
            maturity_date=maturity,
            coupon_rate=0.12,
            coupon_frequency=1,
            conversion_start=maturity,
            conversion_end=maturity,
        )
        assumptions = assumptions_with(
            volatility=0.0,
            risk_free_rate=0.0,
            credit_spread=0.0,
            borrow_rate=0.0,
            dividend_yield=0.0,
            steps=1,
        )

        result = PricingEngine().price(contract, MarketSnapshot(stock_price=100.0), assumptions)

        self.assertAlmostEqual(result.fair_value, 200.0, places=8)

    def test_soft_call_that_started_before_valuation_is_active(self):
        contract = contract_with(
            maturity_date=date(2027, 1, 1),
            calls=[
                CallSchedule(
                    call_type="issuer_soft_call",
                    price=100.0,
                    start_date=date(2025, 1, 1),
                    trigger_ratio=1.10,
                )
            ],
        )

        result = PricingEngine().price(
            contract,
            MarketSnapshot(stock_price=60.0),
            assumptions_with(steps=120),
        )

        self.assertAlmostEqual(result.fair_value, 120.0, places=8)

    def test_multiple_soft_calls_are_order_independent(self):
        call_a = CallSchedule(
            call_type="issuer_soft_call",
            price=100.0,
            start_date=date(2027, 1, 1),
            trigger_ratio=1.20,
        )
        call_b = CallSchedule(
            call_type="issuer_soft_call",
            price=105.0,
            start_date=date(2028, 1, 1),
            trigger_ratio=1.50,
        )
        first = contract_with(calls=[call_a, call_b])
        reversed_order = replace(first, calls=[call_b, call_a])
        assumptions = assumptions_with(steps=160)

        first_value = PricingEngine().price(first, MarketSnapshot(stock_price=50.0), assumptions).fair_value
        reversed_value = PricingEngine().price(
            reversed_order, MarketSnapshot(stock_price=50.0), assumptions
        ).fair_value

        self.assertAlmostEqual(first_value, reversed_value, places=10)

    def test_holder_put_is_not_erased_by_simultaneous_issuer_call(self):
        contract = contract_with(
            maturity_date=date(2027, 1, 1),
            conversion_price=500.0,
            puts=[PutSchedule(put_type="holder_put", price=120.0, date=VALUATION_DATE)],
            calls=[
                CallSchedule(
                    call_type="issuer_soft_call",
                    price=110.0,
                    start_date=VALUATION_DATE,
                    trigger_ratio=0.01,
                )
            ],
        )
        assumptions = assumptions_with(
            volatility=0.0,
            risk_free_rate=0.0,
            credit_spread=0.0,
            borrow_rate=0.0,
            dividend_yield=0.0,
            steps=1,
        )

        result = PricingEngine().price(contract, MarketSnapshot(stock_price=10.0), assumptions)

        self.assertAlmostEqual(result.fair_value, 120.0, places=8)
        self.assertTrue(any("overlapping put/call" in warning for warning in result.diagnostics.warnings))

    def test_put_is_reflected_in_reported_bond_floor(self):
        contract = contract_with(
            conversion_price=1_000.0,
            puts=[PutSchedule(put_type="holder_put", price=100.0, date=date(2028, 1, 1))],
        )
        assumptions = assumptions_with(
            risk_free_rate=0.05,
            credit_spread=0.05,
            dividend_yield=0.0,
            borrow_rate=0.0,
        )

        result = PricingEngine().price(contract, MarketSnapshot(stock_price=1.0), assumptions)

        expected_put_floor = 100.0 * math.exp(-0.10 * ((date(2028, 1, 1) - VALUATION_DATE).days / 365.25))
        self.assertAlmostEqual(result.bond_floor, expected_put_floor, places=8)

    def test_future_put_is_not_rounded_back_to_valuation_on_a_coarse_tree(self):
        put_date = date(2027, 1, 1)
        contract = contract_with(
            conversion_price=1_000.0,
            puts=[PutSchedule(put_type="holder_put", price=100.0, date=put_date)],
        )
        assumptions = assumptions_with(
            volatility=0.0,
            risk_free_rate=0.05,
            credit_spread=0.05,
            borrow_rate=0.0,
            dividend_yield=0.0,
            steps=1,
        )

        result = PricingEngine().price(contract, MarketSnapshot(stock_price=1.0), assumptions)

        expected = 100.0 * math.exp(-0.10 * ((put_date - VALUATION_DATE).days / 365.25))
        self.assertAlmostEqual(result.fair_value, expected, delta=0.03)
        self.assertEqual(result.diagnostics.details["requested_steps"], 1)
        self.assertGreaterEqual(result.diagnostics.details["effective_steps"], 60)


class SolverAndValidationTests(unittest.TestCase):
    def test_implied_vol_rejects_a_volatility_insensitive_price(self):
        contract = contract_with(
            maturity_date=date(2027, 1, 1),
            conversion_end=date(2025, 12, 31),
        )
        assumptions = assumptions_with(steps=60)
        target = PricingEngine().price(contract, MarketSnapshot(stock_price=50.0), assumptions).fair_value

        with self.assertRaisesRegex(ValueError, "not identifiable"):
            PricingEngine().implied_vol(
                contract,
                MarketSnapshot(stock_price=50.0, bond_price=target),
                assumptions,
            )

    def test_implied_vol_rejects_multiple_callable_roots(self):
        contract = contract_with(
            coupon_rate=0.04,
            coupon_frequency=2,
            calls=[
                CallSchedule(
                    call_type="issuer_soft_call",
                    price=100.0,
                    start_date=date(2027, 1, 1),
                    trigger_ratio=1.30,
                )
            ],
        )
        assumptions = assumptions_with(
            volatility=0.30,
            credit_spread=0.03,
            borrow_rate=0.0,
            dividend_yield=0.0,
            steps=100,
        )
        engine = PricingEngine()
        target = engine.price(
            contract,
            MarketSnapshot(stock_price=50.0),
            replace(assumptions, volatility=0.02),
        ).fair_value

        with self.assertRaisesRegex(ValueError, "not unique"):
            engine.implied_vol(
                contract,
                MarketSnapshot(stock_price=50.0, bond_price=target),
                assumptions,
            )

    def test_implied_vol_does_not_double_count_a_sampled_unique_root(self):
        contract = contract_with(
            calls=[
                CallSchedule(
                    call_type="issuer_soft_call",
                    price=100.0,
                    start_date=date(2027, 1, 1),
                    trigger_ratio=1_000_000.0,
                )
            ]
        )
        assumptions = assumptions_with(steps=100)
        engine = PricingEngine()
        grid_vol = 0.0001 + (3.0 - 0.0001) * 8.0 / 32.0
        grid_price = engine.price(
            contract,
            MarketSnapshot(stock_price=45.0),
            replace(assumptions, volatility=grid_vol),
        ).fair_value

        implied = engine.implied_vol(
            contract,
            MarketSnapshot(stock_price=45.0, bond_price=grid_price + 0.5e-6),
            assumptions,
        )

        self.assertAlmostEqual(implied, grid_vol, places=8)

    def test_non_finite_inputs_are_rejected(self):
        contract = contract_with(maturity_date=date(2027, 1, 1))
        with self.assertRaises(ValueError):
            PricingEngine().price(contract, MarketSnapshot(stock_price=math.nan), assumptions_with())
        with self.assertRaises(ValueError):
            PricingEngine().price(
                contract,
                MarketSnapshot(stock_price=50.0),
                assumptions_with(volatility=math.nan),
            )

    def test_post_maturity_valuation_is_rejected(self):
        contract = contract_with(maturity_date=date(2027, 1, 1))
        with self.assertRaisesRegex(ValueError, "after maturity"):
            PricingEngine().price(
                contract,
                MarketSnapshot(stock_price=50.0),
                assumptions_with(valuation_date=date(2027, 1, 2)),
            )


class TraderSanityTests(unittest.TestCase):
    def test_noncallable_tf_shape_and_bounds(self):
        contract = contract_with(coupon_rate=0.02, coupon_frequency=2)
        engine = PricingEngine()
        base = assumptions_with(steps=180)

        stock_results = [engine.price(contract, MarketSnapshot(stock_price=spot), base) for spot in (10, 35, 50, 80, 150)]
        stock_values = [result.fair_value for result in stock_results]
        self.assertEqual(stock_values, sorted(stock_values))
        for result in stock_results:
            self.assertGreaterEqual(result.fair_value + 1e-10, result.bond_floor)
            self.assertGreaterEqual(result.fair_value + 1e-10, result.parity)

        vol_values = [
            engine.price(contract, MarketSnapshot(stock_price=45.0), replace(base, volatility=vol)).fair_value
            for vol in (0.10, 0.30, 0.60)
        ]
        self.assertEqual(vol_values, sorted(vol_values))

        spread_values = [
            engine.price(contract, MarketSnapshot(stock_price=35.0), replace(base, credit_spread=spread)).fair_value
            for spread in (0.0, 0.03, 0.10)
        ]
        self.assertEqual(spread_values, sorted(spread_values, reverse=True))

        dividend_values = [
            engine.price(contract, MarketSnapshot(stock_price=45.0), replace(base, dividend_yield=dividend)).fair_value
            for dividend in (0.0, 0.03, 0.08)
        ]
        self.assertEqual(dividend_values, sorted(dividend_values, reverse=True))

    def test_plain_cb_is_stable_across_grid_refinement(self):
        contract = contract_with(coupon_rate=0.02, coupon_frequency=2)
        assumptions = assumptions_with(steps=250)
        engine = PricingEngine()

        coarse = engine.price(contract, MarketSnapshot(stock_price=50.0), assumptions).fair_value
        refined = engine.price(
            contract,
            MarketSnapshot(stock_price=50.0),
            replace(assumptions, steps=500),
        ).fair_value

        self.assertAlmostEqual(coarse, refined, delta=0.03)

    def test_call_never_raises_value_and_put_never_lowers_it(self):
        call = CallSchedule(
            call_type="issuer_soft_call",
            price=105.0,
            start_date=date(2027, 1, 1),
            trigger_ratio=1.20,
        )
        put = PutSchedule(put_type="holder_put", price=105.0, date=date(2028, 1, 1))
        plain = contract_with()
        callable_contract = replace(plain, calls=[call])
        puttable_contract = replace(plain, puts=[put])
        assumptions = assumptions_with(steps=180)
        market = MarketSnapshot(stock_price=50.0)

        plain_value = PricingEngine().price(plain, market, assumptions).fair_value
        call_value = PricingEngine().price(callable_contract, market, assumptions).fair_value
        put_value = PricingEngine().price(puttable_contract, market, assumptions).fair_value

        self.assertLessEqual(call_value, plain_value + 1e-10)
        self.assertGreaterEqual(put_value + 1e-10, plain_value)
        call_result = PricingEngine().price(callable_contract, market, assumptions)
        self.assertTrue(any("step-sensitive" in warning for warning in call_result.diagnostics.warnings))
        self.assertTrue(any("uncallable cash" in warning for warning in call_result.diagnostics.warnings))

    def test_terminal_split_is_stable_at_exact_parity(self):
        maturity = date(2031, 1, 1)
        contract = contract_with(
            maturity_date=maturity,
            conversion_start=maturity,
            conversion_end=maturity,
        )
        assumptions = assumptions_with(credit_spread=0.10, steps=250)
        engine = PricingEngine()

        at_parity = engine.price(contract, MarketSnapshot(stock_price=50.0), assumptions).fair_value
        epsilon_above = engine.price(contract, MarketSnapshot(stock_price=50.0 + 1e-10), assumptions).fair_value

        self.assertAlmostEqual(at_parity, epsilon_above, delta=1e-6)

    def test_reciprocal_fx_quotes_give_the_same_cross_currency_value(self):
        contract = replace(
            contract_with(),
            stock_currency="TWD",
            conversion=ConversionTerms(
                underlying_ticker="ROBUST TT",
                conversion_price=1_500.0,
                fixed_fx_rate=30.0,
                fixed_fx_convention=FXConvention.STOCK_PER_CB,
            ),
        )
        assumptions = assumptions_with(steps=250)
        direct = PricingEngine().price(
            contract,
            MarketSnapshot(
                stock_price=1_200.0,
                stock_currency="TWD",
                fx_rate=32.0,
                fx_convention=FXConvention.STOCK_PER_CB,
            ),
            assumptions,
        )
        inverse = PricingEngine().price(
            contract,
            MarketSnapshot(
                stock_price=1_200.0,
                stock_currency="TWD",
                fx_rate=1.0 / 32.0,
                fx_convention=FXConvention.CB_PER_STOCK,
            ),
            assumptions,
        )

        self.assertAlmostEqual(direct.parity, 75.0, places=10)
        self.assertAlmostEqual(direct.fair_value, inverse.fair_value, places=10)
        self.assertTrue(any("one-factor" in warning for warning in direct.diagnostics.warnings))


if __name__ == "__main__":
    unittest.main()
