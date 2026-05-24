import tempfile
import unittest
from pathlib import Path

from cb_terminal.domain import Assumptions
from cb_terminal.storage.sqlite_store import CbTerminalStore


class AssumptionStoreTests(unittest.TestCase):
    def test_save_assumptions_is_append_only_and_latest_is_retrievable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = CbTerminalStore(Path(tmpdir) / "cb_terminal.sqlite")
            first = store.save_assumption_set(
                contract_id="XS3236970433_contract",
                scenario_name="base",
                assumptions=Assumptions(
                    volatility=0.38,
                    risk_free_rate=0.015,
                    credit_spread=0.0275,
                    borrow_rate=0.01,
                    dividend_yield=0.0,
                    steps=80,
                ),
                use_yield_curve=True,
                yield_curve_currency="TWD",
                notes="initial PM base case",
                created_by="unittest",
            )
            second = store.save_assumption_set(
                contract_id="XS3236970433_contract",
                scenario_name="base",
                assumptions=Assumptions(
                    volatility=0.44,
                    risk_free_rate=0.016,
                    credit_spread=0.035,
                    borrow_rate=0.012,
                    dividend_yield=0.001,
                    steps=100,
                ),
                use_yield_curve=True,
                yield_curve_currency="TWD",
                notes="updated after borrow check",
                created_by="unittest",
            )

            self.assertNotEqual(first.id, second.id)
            self.assertIsNone(first.supersedes_assumption_set_id)
            self.assertEqual(second.supersedes_assumption_set_id, first.id)
            latest = store.latest_assumption_set("XS3236970433_contract", "base")
            self.assertEqual(latest.id, second.id)
            self.assertEqual(latest.contract_id, "XS3236970433_contract")
            self.assertEqual(latest.scenario_name, "base")
            self.assertAlmostEqual(latest.assumptions.volatility, 0.44)
            self.assertAlmostEqual(latest.assumptions.credit_spread, 0.035)
            self.assertEqual(latest.assumptions.steps, 100)
            self.assertTrue(latest.use_yield_curve)
            self.assertEqual(latest.yield_curve_currency, "TWD")
            self.assertEqual(latest.notes, "updated after borrow check")

    def test_valuation_run_records_assumption_snapshot_and_results(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = CbTerminalStore(Path(tmpdir) / "cb_terminal.sqlite")
            saved = store.save_assumption_set(
                contract_id="XS3236970433_contract",
                scenario_name="pm_stress",
                assumptions=Assumptions(0.50, 0.015, credit_spread=0.08, borrow_rate=0.02, dividend_yield=0.0, steps=60),
                created_by="unittest",
            )
            run = store.create_valuation_run(
                contract_id="XS3236970433_contract",
                assumption_set_id=saved.id,
                model_version="simple_crr:v1",
                run_type="preview",
                inputs={"market_history_path": "tests/fixtures/issuer_synthetic_market_history.csv"},
            )
            store.save_valuation_results(
                run.id,
                [
                    {
                        "as_of_date": "2026-03-27",
                        "fair_value": 123.4,
                        "market_price": 118.0,
                        "cheapness": 5.4,
                        "parity": 110.0,
                        "bond_floor": 91.0,
                        "implied_volatility": 0.42,
                        "warnings": "",
                    }
                ],
            )

            loaded = store.get_valuation_run(run.id)
            self.assertEqual(loaded.id, run.id)
            self.assertEqual(loaded.assumption_set_id, saved.id)
            self.assertEqual(loaded.model_version, "simple_crr:v1")
            rows = store.valuation_results(run.id)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["as_of_date"], "2026-03-27")
            self.assertAlmostEqual(rows[0]["fair_value"], 123.4)
            self.assertAlmostEqual(rows[0]["implied_volatility"], 0.42)


if __name__ == "__main__":
    unittest.main()
