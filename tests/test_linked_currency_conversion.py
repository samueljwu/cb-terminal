import json
import unittest
from unittest.mock import patch

from cb_terminal.io.contract_loader import contract_from_dict
from cb_terminal.io.yield_curves import (
    HKMA_OFFSHORE_RMB_CURVE_URL,
    curve_currency_for_contract,
    fetch_worldgovernmentbonds_curve,
)
from cb_terminal.prospectus.review import validate_contract_dict
from cb_terminal.prospectus.draft_contract import _extract_economic_currency, _extract_fixed_fx_with_basis
from cb_terminal.web.server import _curve_currency_for_pricing


def _linked_contract():
    return {
        "id": "linked_cb",
        "status": "needs_review",
        "issuer": {"name": "Test Issuer"},
        "bond": {
            "currency": "RMB", "economic_currency": "CNH", "settlement_currency": "USD",
            "stock_currency": "HKD", "pricing_face": 100, "issue_size": 1000000,
            "denomination": 1000000, "issue_price": 100, "coupon_rate": 0,
            "pricing_date": "2026-10-07", "closing_date": "2026-10-14",
            "maturity_date": "2027-10-12",
        },
        "redemption": {"maturity_price": 100},
        "conversion": {
            "underlying_ticker": "TEST HK", "initial_conversion_price": 128.82,
            "fixed_exchange_rate": 1 / 0.8543,
            "fixed_exchange_rate_basis_currency": "CNH",
            "fixed_exchange_rate_units": "HKD per CNH",
        },
    }


class LinkedCurrencyConversionTests(unittest.TestCase):
    def test_conversion_start_is_derived_from_closing_rule(self):
        raw = _linked_contract()
        raw["bond"]["currency"] = "USD"
        raw["bond"]["economic_currency"] = "TWD"
        raw["bond"]["settlement_currency"] = "USD"
        raw["bond"]["stock_currency"] = "TWD"
        raw["conversion"]["fixed_exchange_rate"] = 31.753
        raw["conversion"]["fixed_exchange_rate_basis_currency"] = "USD"
        raw["conversion"]["fixed_exchange_rate_units"] = "TWD per USD"
        raw["conversion"]["start_date_rule"] = (
            "Next day immediately after the end of the three-month period following the actual Closing Date."
        )
        raw["redemption"]["maturity_price_rule"] = "Settlement Equivalent of principal"
        raw["bond"]["closing_date"] = "2026-10-13"
        contract = contract_from_dict(raw)
        self.assertEqual(contract.conversion.start_date.isoformat(), "2027-01-14")
        self.assertTrue(contract.cash_fx_linked)

    def test_draft_normalizes_cnh_linked_fixed_quote(self):
        text = "RMB denominated CNH-linked bonds. Fixed Exchange Rate: CNH 0.8543 = HKD 1.00"
        economic = _extract_economic_currency(text, series_label=None, bond_currency="RMB", stock_currency="HKD")
        rate, units, _, basis = _extract_fixed_fx_with_basis(text, "RMB", economic, "HKD")

        self.assertEqual(economic, "CNH")
        self.assertEqual(basis, "CNH")
        self.assertEqual(units, "HKD per CNH")
        self.assertAlmostEqual(rate, 1 / 0.8543)

    def test_cnh_basis_preserves_legal_and_settlement_currencies(self):
        raw = _linked_contract()
        contract = contract_from_dict(raw)

        self.assertEqual((contract.currency, contract.economic_currency, contract.settlement_currency),
                         ("RMB", "CNH", "USD"))
        self.assertAlmostEqual(contract.conversion.fixed_fx_rate, 1 / 0.8543)
        self.assertEqual(curve_currency_for_contract(contract), "CNH")
        self.assertFalse([issue for issue in validate_contract_dict(raw)
                          if issue.severity == "error" and "exchange_rate" in issue.field])

    def test_quote_without_explicit_basis_gives_field_error(self):
        raw = _linked_contract()
        del raw["conversion"]["fixed_exchange_rate_basis_currency"]

        issues = validate_contract_dict(raw)
        self.assertTrue(any(issue.field == "conversion.fixed_exchange_rate_basis_currency"
                            and "basis_currency" in issue.message for issue in issues))
        with self.assertRaisesRegex(ValueError, "basis_currency"):
            contract_from_dict(raw)

    def test_cnh_basis_requires_cnh_economic_link(self):
        raw = _linked_contract()
        raw["bond"]["economic_currency"] = "RMB"

        issues = validate_contract_dict(raw)
        self.assertTrue(any(issue.field == "conversion.fixed_exchange_rate_basis_currency"
                            and issue.severity == "error" for issue in issues))

    def test_any_linked_currency_can_be_the_conversion_fx_basis(self):
        raw = _linked_contract()
        raw["bond"].update(currency="USD", economic_currency="TWD", settlement_currency="EUR")
        raw["conversion"].update(
            fixed_exchange_rate=31.5,
            fixed_exchange_rate_basis_currency="TWD",
            fixed_exchange_rate_units="HKD per TWD",
        )
        contract = contract_from_dict(raw)

        self.assertEqual(contract.conversion.fixed_fx_rate, 31.5)
        self.assertEqual(curve_currency_for_contract(contract), "TWD")
        self.assertEqual(_curve_currency_for_pricing(contract), "TWD")
        with self.assertRaisesRegex(ValueError, "must match bond economic currency"):
            _curve_currency_for_pricing(contract, "USD")

        raw["conversion"].pop("fixed_exchange_rate_basis_currency")
        self.assertTrue(any(issue.field == "conversion.fixed_exchange_rate_basis_currency"
                            for issue in validate_contract_dict(raw)))

    def test_generic_linked_currency_quote_is_extracted(self):
        text = "USD denominated EUR-linked bonds. Fixed Exchange Rate: EUR 0.92 = GBP 1.00"
        economic = _extract_economic_currency(text, series_label=None, bond_currency="USD", stock_currency="GBP")
        rate, units, _, basis = _extract_fixed_fx_with_basis(text, "USD", economic, "GBP")

        self.assertEqual(economic, "EUR")
        self.assertEqual(basis, "EUR")
        self.assertEqual(units, "GBP per EUR")
        self.assertAlmostEqual(rate, 1 / 0.92)
        raw = _linked_contract()
        raw["bond"].update(currency="USD", economic_currency="EUR", stock_currency="GBP")
        raw["conversion"].update(
            fixed_exchange_rate=rate,
            fixed_exchange_rate_basis_currency=basis,
            fixed_exchange_rate_units=units,
        )
        self.assertEqual(curve_currency_for_contract(contract_from_dict(raw)), "EUR")

    @patch("cb_terminal.io.yield_curves._urlopen_text")
    def test_cnh_fetch_uses_offshore_rmb_benchmark(self, read_text):
        read_text.return_value = json.dumps({"result": {"records": [{
            "daily_figure": "2026-10-23", "closing_ref_rate_1y": 2.25,
            "closing_ref_rate_2y": None, "ind_pricing_2y": 2.5,
        }]}})

        curve = fetch_worldgovernmentbonds_curve("CNH")

        read_text.assert_called_once_with(HKMA_OFFSHORE_RMB_CURVE_URL, timeout=30)
        self.assertEqual((curve.currency, curve.as_of, curve.source),
                         ("CNH", "2026-10-23", HKMA_OFFSHORE_RMB_CURVE_URL))
        self.assertEqual([(point.years, point.rate) for point in curve.points],
                         [(1.0, 0.0225), (2.0, 0.025)])


if __name__ == "__main__":
    unittest.main()
