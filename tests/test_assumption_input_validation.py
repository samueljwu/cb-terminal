import unittest

from cb_terminal.web.server import (
    _mapping_rate_decimal,
    _query_rate_decimal,
    _require_interactive_assumptions,
)


class AssumptionInputValidationTests(unittest.TestCase):
    def test_blank_display_unit_field_does_not_rescale_decimal_default(self):
        payload = {"input_units": "display", "volatility": ""}
        query = {"input_units": ["display"], "volatility": [""]}

        self.assertEqual(
            _mapping_rate_decimal(payload, "volatility", 0.38, unit="percent"),
            0.38,
        )
        self.assertEqual(
            _query_rate_decimal(query, "volatility", 0.38, unit="percent"),
            0.38,
        )

    def test_explicit_display_values_are_converted_to_decimals(self):
        payload = {
            "input_units": "display",
            "volatility": "38",
            "credit_spread": "160",
        }

        self.assertEqual(
            _mapping_rate_decimal(payload, "volatility", 0.0, unit="percent"),
            0.38,
        )
        self.assertEqual(
            _mapping_rate_decimal(payload, "credit_spread", 0.0, unit="bps"),
            0.016,
        )

    def test_interactive_pricing_requires_explicit_economic_assumptions(self):
        with self.assertRaisesRegex(
            ValueError,
            "volatility, credit spread, borrow cost, dividend yield, risk-free source",
        ):
            _require_interactive_assumptions({"input_units": "display"})

    def test_explicit_zero_is_not_treated_as_a_missing_assumption(self):
        _require_interactive_assumptions(
            {
                "volatility": "38",
                "credit_spread": "0",
                "borrow_rate": "0",
                "dividend_yield": "0",
                "use_yield_curve": True,
            }
        )


if __name__ == "__main__":
    unittest.main()
