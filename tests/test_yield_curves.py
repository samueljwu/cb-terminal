import unittest
from dataclasses import replace
from datetime import date

from cb_terminal.domain import Contract, ConversionTerms, CouponSchedule
from cb_terminal.io.yield_curves import (
    COUNTRY_BY_CURRENCY,
    SUPPORTED_YIELD_CURVE_CURRENCIES,
    curve_currency_for_contract,
    curve_currency_from_contract_dict,
)


def _contract(*, currency: str, stock_currency: str, economic_currency: str = "") -> Contract:
    metadata = {"term_extensions": {}}
    if economic_currency:
        metadata["term_extensions"]["economic_currency"] = economic_currency
    return Contract(
        id="TEST",
        issuer="Test Issuer",
        description="Test CB",
        currency=currency,
        settlement_currency=currency,
        stock_currency=stock_currency,
        face=100.0,
        issue_price=100.0,
        maturity_price=100.0,
        pricing_date=date(2026, 1, 1),
        maturity_date=date(2031, 1, 1),
        coupon=CouponSchedule(),
        conversion=ConversionTerms(
            underlying_ticker="TEST Equity",
            conversion_price=50.0,
        ),
        economic_currency=economic_currency,
        metadata=metadata,
    )


class YieldCurveCurrencyTests(unittest.TestCase):
    def test_dashboard_supported_curve_currencies_are_explicit_and_include_australia(self):
        self.assertEqual(
            SUPPORTED_YIELD_CURVE_CURRENCIES,
            ("USD", "HKD", "TWD", "CNY", "JPY", "KRW", "AUD"),
        )
        self.assertEqual(COUNTRY_BY_CURRENCY["AUD"], "australia")

    def test_cross_currency_equity_does_not_change_cash_curve_currency(self):
        lenovo_like = _contract(currency="USD", stock_currency="HKD")

        self.assertEqual(curve_currency_for_contract(lenovo_like), "USD")

    def test_explicit_economic_currency_controls_currency_linked_cb_curve(self):
        phison_series_a_like = _contract(
            currency="USD",
            stock_currency="TWD",
            economic_currency="TWD",
        )

        self.assertEqual(curve_currency_for_contract(phison_series_a_like), "TWD")

    def test_legal_currency_remains_fallback_when_economic_metadata_is_blank(self):
        ordinary_usd_cb = _contract(currency="USD", stock_currency="TWD")
        blank_metadata = replace(ordinary_usd_cb, metadata={})

        self.assertEqual(curve_currency_for_contract(blank_metadata), "USD")

    def test_draft_terms_expose_economic_curve_currency_before_full_validation(self):
        raw = {
            "bond": {
                "currency": "USD",
                "settlement_currency": "USD",
                "economic_currency": "TWD",
            },
            "redemption": {
                "yield_to_maturity": 0.02,
                # An unrelated missing compounding frequency can keep a draft
                # from loading as a final Contract.
            },
        }

        self.assertEqual(curve_currency_from_contract_dict(raw), "TWD")


if __name__ == "__main__":
    unittest.main()
