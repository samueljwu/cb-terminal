import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from cb_terminal.domain import Assumptions, MarketRow
from cb_terminal.web import server


class SensitivityPreviewTests(unittest.TestCase):
    def test_context_loads_once_uses_latest_row_and_applies_curve(self):
        first = MarketRow(
            as_of_date=date(2026, 7, 23),
            stock_price=49.0,
            bond_price=100.0,
        )
        latest = MarketRow(
            as_of_date=date(2026, 7, 24),
            stock_price=50.0,
            bond_price=101.0,
            assumption_overrides={"credit_spread": 0.08},
        )
        contract = MagicMock()
        validation = MagicMock()
        matched = SimpleNamespace(rate=0.045)
        payload = {
            "contract_path": "data/contracts/test.json",
            "market_history_path": "data/price_history/generated/test.csv",
            "use_history_assumptions": False,
            "use_yield_curve": True,
            "yield_curve_currency": "USD",
        }

        with (
            patch.object(server, "resolve_project_path", side_effect=lambda value: Path(str(value))),
            patch.object(server, "_canonical_source_for_contract", return_value={}),
            patch.object(server, "load_contract_json", return_value=contract) as load_contract,
            patch.object(
                server,
                "validate_market_history_file_for_contract",
                return_value=validation,
            ) as validate,
            patch.object(server, "load_market_history_csv", return_value=[first, latest]) as load_history,
            patch.object(server, "_cached_worldgovernmentbonds_curve", return_value=MagicMock()) as load_curve,
            patch.object(server, "match_curve_for_contract", return_value=matched) as match_curve,
        ):
            loaded_contract, loaded_row = server._latest_sensitivity_market_context(payload)

        self.assertIs(loaded_contract, contract)
        self.assertEqual(loaded_row.as_of_date, latest.as_of_date)
        self.assertEqual(loaded_row.assumption_overrides, {"risk_free_rate": 0.045})
        load_contract.assert_called_once()
        validate.assert_called_once()
        validation.raise_for_errors.assert_called_once()
        load_history.assert_called_once()
        load_curve.assert_called_once_with("USD")
        match_curve.assert_called_once()

    def test_standard_grid_keeps_legacy_order_and_floors(self):
        base = Assumptions(
            volatility=0.08,
            risk_free_rate=0.03,
            credit_spread=0.005,
            borrow_rate=0.01,
            dividend_yield=0.02,
            steps=3,
        )

        scenarios = server._standard_sensitivity_scenarios(base)

        self.assertEqual(
            [name for name, _ in scenarios],
            [
                "Vol -10 pts",
                "Vol -5 pts",
                "Vol +5 pts",
                "Vol +10 pts",
                "Spread -100 bp",
                "Spread +100 bp",
                "Spread +300 bp",
                "Borrow +100 bp",
                "Dividend +100 bp",
            ],
        )
        self.assertEqual(scenarios[0][1].volatility, 0.0)
        self.assertAlmostEqual(scenarios[1][1].volatility, 0.03)
        self.assertEqual(scenarios[4][1].credit_spread, 0.0)
        self.assertAlmostEqual(scenarios[5][1].credit_spread, 0.015)

    def test_batch_prepares_once_prices_one_row_and_reuses_base_iv(self):
        latest_row = MarketRow(
            as_of_date=date(2026, 7, 24),
            stock_price=50.0,
            bond_price=101.0,
        )
        contract = MagicMock()

        class FakeEngine:
            def __init__(self, model_mode):
                self.model_mode = model_mode
                self.price_calls = []
                self.iv_calls = []

            def price(self, _contract, _market, assumptions):
                self.price_calls.append(assumptions)
                return SimpleNamespace(
                    fair_value=100.0 + assumptions.volatility + assumptions.credit_spread,
                    cheapness=assumptions.volatility,
                    diagnostics=SimpleNamespace(warnings=[]),
                )

            def implied_vol(self, _contract, _market, assumptions, *, target_price):
                self.iv_calls.append((assumptions, target_price))
                return 0.42

        engine = FakeEngine("simple_crr")
        payload = {
            "volatility": 35,
            "risk_free_rate": 3,
            "credit_spread": 200,
            "borrow_rate": 1,
            "dividend_yield": 2,
            "steps": 3,
            "use_yield_curve": False,
            "use_history_assumptions": False,
            "input_units": "display",
            "model_mode": "simple_crr",
            "record_run": True,
        }

        with (
            patch.object(
                server,
                "_latest_sensitivity_market_context",
                return_value=(contract, latest_row),
            ) as prepare,
            patch.object(server, "PricingEngine", return_value=engine),
            patch.object(server, "_store") as persistent_store,
        ):
            result = server.preview_sensitivity_payload(payload)

        prepare.assert_called_once_with(payload)
        persistent_store.assert_not_called()
        self.assertEqual(result["scenario_count"], 9)
        self.assertEqual(len(engine.price_calls), 9)
        self.assertEqual(len(engine.iv_calls), 5)
        self.assertTrue(
            all(
                item["reuse_base_implied_volatility"]
                for item in result["scenarios"][:4]
            )
        )
        self.assertTrue(
            all(
                not item["reuse_base_implied_volatility"]
                for item in result["scenarios"][4:]
            )
        )
        self.assertEqual(
            [item["row"]["date"] for item in result["scenarios"]],
            ["2026-07-24"] * 9,
        )

    def test_row_overrides_and_one_scenario_failure_preserve_other_results(self):
        latest_row = MarketRow(
            as_of_date=date(2026, 7, 24),
            stock_price=50.0,
            bond_price=101.0,
            assumption_overrides={"credit_spread": 0.07},
        )

        class PartiallyFailingEngine:
            model_mode = "simple_crr"

            def price(self, _contract, _market, assumptions):
                if abs(assumptions.borrow_rate - 0.02) < 1e-12:
                    raise ValueError("borrow scenario failed")
                return SimpleNamespace(
                    fair_value=100.0,
                    cheapness=-1.0,
                    diagnostics=SimpleNamespace(warnings=[]),
                )

            def implied_vol(self, _contract, _market, _assumptions, *, target_price):
                return 0.41

        payload = {
            "volatility": 35,
            "risk_free_rate": 3,
            "credit_spread": 200,
            "borrow_rate": 1,
            "dividend_yield": 2,
            "steps": 3,
            "use_yield_curve": False,
            "use_history_assumptions": True,
            "input_units": "display",
            "model_mode": "simple_crr",
        }

        with (
            patch.object(
                server,
                "_latest_sensitivity_market_context",
                return_value=(MagicMock(), latest_row),
            ),
            patch.object(server, "PricingEngine", return_value=PartiallyFailingEngine()),
        ):
            result = server.preview_sensitivity_payload(payload)

        self.assertEqual(len(result["scenarios"]), 9)
        self.assertEqual(
            [item["row"]["credit_spread"] for item in result["scenarios"][4:7]],
            [0.07, 0.07, 0.07],
        )
        failures = [item for item in result["scenarios"] if item["error"]]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["name"], "Borrow +100 bp")
        self.assertIn("borrow scenario failed", failures[0]["error"])


if __name__ == "__main__":
    unittest.main()
