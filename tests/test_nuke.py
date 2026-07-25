import math
import unittest

from cb_terminal.pricing import nuke
from cb_terminal.validation import ValidationError
from cb_terminal.web.server import build_nuke_payload


class NukeTests(unittest.TestCase):
    def test_web_payload_exposes_calculation_and_move_components(self):
        payload = build_nuke_payload(
            {
                "anchor_bond_price": 97.48,
                "anchor_stock_price": 20.45,
                "anchor_fx": 1.0,
                "current_stock_price": 20.65,
                "current_fx": 1.0,
                "delta": 1.76,
            }
        )

        self.assertAlmostEqual(payload["nuked_bond_price"], 97.832)
        self.assertAlmostEqual(payload["bond_price_change"], 0.352)
        self.assertAlmostEqual(payload["stock_move_in_bond_currency"], 0.2)

    def test_unchanged_market_returns_anchor_bond_price(self):
        self.assertEqual(nuke(101.25, 78.0, 7.8, 78.0, 7.8, 1.4), 101.25)

    def test_reprices_from_delta_and_fx_adjusted_stock_move(self):
        # Mirrors the standard dollar-neutral example with same-currency FX.
        result = nuke(97.48, 20.45, 1.0, 20.65, 1.0, 1.76)

        self.assertAlmostEqual(result, 97.832, places=12)

    def test_stock_and_fx_moves_that_offset_leave_bond_unchanged(self):
        # Both stock observations are worth 20 in the bond currency.
        result = nuke(99.5, 156.0, 7.8, 160.0, 8.0, 2.25)

        self.assertAlmostEqual(result, 99.5, places=12)

    def test_stronger_stock_per_cb_fx_lowers_nuked_bond_price(self):
        result = nuke(100.0, 78.0, 7.8, 78.0, 8.0, 2.0)

        self.assertAlmostEqual(result, 99.5, places=12)

    def test_zero_delta_returns_anchor_bond_despite_market_move(self):
        self.assertEqual(nuke(103.0, 50.0, 1.0, 75.0, 1.25, 0.0), 103.0)

    def test_rejects_non_positive_price_and_fx_inputs(self):
        valid = {
            "anchor_bond_price": 100.0,
            "anchor_stock_price": 50.0,
            "anchor_fx": 1.0,
            "current_stock_price": 55.0,
            "current_fx": 1.0,
            "delta": 0.5,
        }
        for name in (
            "anchor_bond_price",
            "anchor_stock_price",
            "anchor_fx",
            "current_stock_price",
            "current_fx",
        ):
            for value in (0.0, -1.0, math.nan, math.inf):
                with self.subTest(name=name, value=value):
                    inputs = {**valid, name: value}
                    with self.assertRaises(ValidationError):
                        nuke(**inputs)

    def test_rejects_non_finite_delta(self):
        for delta in (math.nan, math.inf, -math.inf):
            with self.subTest(delta=delta):
                with self.assertRaises(ValidationError):
                    nuke(100.0, 50.0, 1.0, 55.0, 1.0, delta)


if __name__ == "__main__":
    unittest.main()
