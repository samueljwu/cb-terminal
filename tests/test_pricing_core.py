import math
import unittest
from datetime import date

from cb_terminal.domain import Assumptions, Contract, ConversionTerms, CouponSchedule, MarketSnapshot
from cb_terminal.pricing import PricingEngine, black_scholes_call, black_scholes_put, norm_cdf


def sample_contract() -> Contract:
    return Contract(
        id="unit_test_cb",
        issuer="Unit Test Issuer",
        description="1y zero coupon test convertible",
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


class PricingCoreTests(unittest.TestCase):
    def setUp(self):
        self.engine = PricingEngine()
        self.contract = sample_contract()
        self.assumptions = Assumptions(
            volatility=0.30,
            risk_free_rate=0.03,
            credit_spread=0.02,
            borrow_rate=0.0,
            dividend_yield=0.0,
            steps=120,
            valuation_date=date(2026, 1, 1),
        )

    def test_norm_cdf_and_black_scholes_parity(self):
        self.assertAlmostEqual(norm_cdf(0.0), 0.5, places=12)
        call = black_scholes_call(100.0, 100.0, 1.0, 0.03, 0.25, dividend_yield=0.01)
        put = black_scholes_put(100.0, 100.0, 1.0, 0.03, 0.25, dividend_yield=0.01)
        parity_rhs = 100.0 * math.exp(-0.01) - 100.0 * math.exp(-0.03)
        self.assertAlmostEqual(call - put, parity_rhs, places=10)

    def test_convertible_value_increases_with_stock_and_vol(self):
        low_stock = self.engine.price(self.contract, MarketSnapshot(stock_price=35.0), self.assumptions)
        high_stock = self.engine.price(self.contract, MarketSnapshot(stock_price=60.0), self.assumptions)
        high_vol = self.engine.price(
            self.contract,
            MarketSnapshot(stock_price=35.0),
            Assumptions(**{**self.assumptions.__dict__, "volatility": 0.60}),
        )
        self.assertGreater(high_stock.fair_value, low_stock.fair_value)
        self.assertGreater(high_vol.fair_value, low_stock.fair_value)
        self.assertGreaterEqual(low_stock.fair_value, low_stock.bond_floor)

    def test_deep_itm_and_otm_sanity(self):
        deep_itm = self.engine.price(self.contract, MarketSnapshot(stock_price=100.0), self.assumptions)
        deep_otm = self.engine.price(self.contract, MarketSnapshot(stock_price=5.0), self.assumptions)
        self.assertGreater(deep_itm.fair_value, 190.0)
        self.assertLess(abs(deep_otm.fair_value - deep_otm.bond_floor), 2.0)

    def test_implied_vol_inverts_model_price(self):
        target_assumptions = Assumptions(**{**self.assumptions.__dict__, "volatility": 0.42})
        target = self.engine.price(self.contract, MarketSnapshot(stock_price=45.0), target_assumptions).fair_value
        implied = self.engine.implied_vol(
            self.contract,
            MarketSnapshot(stock_price=45.0, bond_price=target),
            self.assumptions,
            low=0.05,
            high=1.0,
            tolerance=1e-5,
        )
        self.assertAlmostEqual(implied, 0.42, places=3)

    def test_cheapness_positive_when_market_below_fair(self):
        fair = self.engine.price(self.contract, MarketSnapshot(stock_price=45.0), self.assumptions).fair_value
        cheap = self.engine.cheapness(
            self.contract,
            MarketSnapshot(stock_price=45.0, bond_price=fair - 1.25),
            self.assumptions,
        )
        self.assertAlmostEqual(cheap, 1.25, places=6)

    def test_bond_floor_uses_valuation_date(self):
        early = self.engine.price(self.contract, MarketSnapshot(stock_price=5.0), self.assumptions)
        later = self.engine.price(
            self.contract,
            MarketSnapshot(stock_price=5.0),
            Assumptions(**{**self.assumptions.__dict__, "valuation_date": date(2026, 10, 1)}),
        )
        self.assertGreater(later.bond_floor, early.bond_floor)
        self.assertGreater(later.fair_value, early.fair_value)

    def test_one_year_coupon_is_included_on_exact_calendar_maturity(self):
        contract = Contract(
            **{
                **self.contract.__dict__,
                "coupon": CouponSchedule(annual_rate=0.12, frequency=1),
            }
        )
        result = self.engine.price(
            contract,
            MarketSnapshot(stock_price=1.0),
            Assumptions(**{**self.assumptions.__dict__, "volatility": 0.0, "risk_free_rate": 0.0, "credit_spread": 0.0, "steps": 1}),
        )
        self.assertAlmostEqual(result.bond_floor, 112.0, places=8)
        self.assertAlmostEqual(result.fair_value, 112.0, places=8)

    def test_two_year_semiannual_coupons_are_included(self):
        contract = Contract(
            **{
                **self.contract.__dict__,
                "maturity_date": date(2028, 1, 1),
                "coupon": CouponSchedule(annual_rate=0.08, frequency=2),
            }
        )
        result = self.engine.price(
            contract,
            MarketSnapshot(stock_price=1.0),
            Assumptions(**{**self.assumptions.__dict__, "volatility": 0.0, "risk_free_rate": 0.0, "credit_spread": 0.0, "steps": 4}),
        )
        self.assertAlmostEqual(result.bond_floor, 116.0, places=8)
        self.assertAlmostEqual(result.fair_value, 116.0, places=8)

    def test_conversion_not_allowed_before_future_start_date(self):
        contract = Contract(
            **{
                **self.contract.__dict__,
                "conversion": ConversionTerms(
                    underlying_ticker="TEST",
                    conversion_price=50.0,
                    start_date=date(2027, 1, 2),
                    fixed_fx_rate=1.0,
                ),
            }
        )
        result = self.engine.price(
            contract,
            MarketSnapshot(stock_price=100.0),
            Assumptions(**{**self.assumptions.__dict__, "volatility": 0.0, "risk_free_rate": 0.0, "credit_spread": 0.0, "steps": 1}),
        )
        self.assertAlmostEqual(result.fair_value, 100.0, places=8)
        self.assertAlmostEqual(result.parity, 200.0, places=8)

    def test_conversion_not_allowed_after_expired_end_date(self):
        contract = Contract(
            **{
                **self.contract.__dict__,
                "conversion": ConversionTerms(
                    underlying_ticker="TEST",
                    conversion_price=50.0,
                    end_date=date(2025, 12, 31),
                    fixed_fx_rate=1.0,
                ),
            }
        )
        result = self.engine.price(
            contract,
            MarketSnapshot(stock_price=100.0),
            Assumptions(**{**self.assumptions.__dict__, "volatility": 0.0, "risk_free_rate": 0.0, "credit_spread": 0.0, "steps": 1}),
        )
        self.assertAlmostEqual(result.fair_value, 100.0, places=8)
        self.assertAlmostEqual(result.parity, 200.0, places=8)

    def test_future_conversion_window_retains_value_while_current_gap_blocks_exercise(self):
        contract = Contract(
            **{
                **self.contract.__dict__,
                "maturity_date": date(2027, 1, 1),
                "conversion": ConversionTerms(
                    underlying_ticker="TEST",
                    conversion_price=50.0,
                    start_date=date(2026, 1, 1),
                    end_date=date(2026, 12, 31),
                    fixed_fx_rate=1.0,
                    windows=(
                        (date(2026, 1, 1), date(2026, 3, 31)),
                        (date(2026, 10, 1), date(2026, 12, 31)),
                    ),
                ),
            }
        )
        result = self.engine.price(
            contract,
            MarketSnapshot(stock_price=100.0),
            Assumptions(
                **{
                    **self.assumptions.__dict__,
                    "valuation_date": date(2026, 6, 1),
                    "volatility": 0.0,
                    "risk_free_rate": 0.0,
                    "credit_spread": 0.0,
                    "dividend_yield": 0.12,
                    "borrow_rate": 0.0,
                    "steps": 1,
                }
            ),
        )

        # Waiting for the October window loses carry, so the CB is worth less
        # than immediate parity but more than redemption.  A one-step lattice
        # must not make that future conversion right disappear.
        self.assertGreater(result.fair_value, 100.0)
        self.assertLess(result.fair_value, 200.0)
        self.assertAlmostEqual(result.parity, 200.0, places=8)

    def test_tf_split_tree_reports_cash_and_equity_components_and_reduces_credit_hit_for_equity_like_cb(self):
        market = MarketSnapshot(stock_price=60.0)
        low_credit = Assumptions(**{**self.assumptions.__dict__, "credit_spread": 0.01, "steps": 80})
        high_credit = Assumptions(**{**self.assumptions.__dict__, "credit_spread": 0.10, "steps": 80})
        simple_engine = PricingEngine(model_mode="simple_crr")
        split_engine = PricingEngine(model_mode="tf_split_tree")

        simple_low = simple_engine.price(self.contract, market, low_credit)
        simple_high = simple_engine.price(self.contract, market, high_credit)
        split_low = split_engine.price(self.contract, market, low_credit)
        split_high = split_engine.price(self.contract, market, high_credit)

        self.assertEqual(split_low.diagnostics.details["model_mode"], "tf_split_tree")
        self.assertGreater(split_low.diagnostics.details["equity_component"], 0)
        self.assertGreaterEqual(split_low.diagnostics.details["cash_component"], 0)
        simple_credit_hit = simple_low.fair_value - simple_high.fair_value
        split_credit_hit = split_low.fair_value - split_high.fair_value
        self.assertLess(split_credit_hit, simple_credit_hit)
        self.assertGreater(split_high.fair_value, split_high.bond_floor)

    def test_unknown_pricing_model_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            PricingEngine(model_mode="mystery")


if __name__ == "__main__":
    unittest.main()
