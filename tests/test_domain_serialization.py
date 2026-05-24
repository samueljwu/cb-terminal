import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from cb_terminal.domain import (
    Assumptions,
    Contract,
    ConversionTerms,
    CouponSchedule,
    Diagnostics,
    FXConvention,
    MarketRow,
    MarketSnapshot,
    PricingResult,
)
from cb_terminal.storage.json_artifacts import read_json, write_json_atomic
from cb_terminal.storage.sqlite_store import CbTerminalStore
from cb_terminal.web.server import _json_default, _write_json_atomic as web_write_json_atomic


class DomainSerializationTests(unittest.TestCase):
    def test_domain_dataclasses_round_trip_through_canonical_payloads(self):
        contract = Contract(
            id="XS1234567890_contract",
            issuer="Issuer PLC",
            description="Issuer 0 31",
            currency="USD",
            settlement_currency="USD",
            stock_currency="HKD",
            face=100.0,
            issue_price=100.0,
            maturity_price=100.0,
            pricing_date=date(2026, 5, 24),
            maturity_date=date(2031, 5, 24),
            coupon=CouponSchedule(annual_rate=0.0, frequency=0),
            conversion=ConversionTerms(
                underlying_ticker="1234 HK Equity",
                conversion_price=50.0,
                fixed_fx_rate=7.8,
                fixed_fx_convention=FXConvention.STOCK_PER_CB,
            ),
        )

        payload = contract.to_dict()
        self.assertEqual(payload["pricing_date"], "2026-05-24")
        self.assertEqual(payload["conversion"]["fixed_fx_convention"], "STOCK_PER_CB")
        self.assertEqual(Contract.from_dict(payload), contract)

    def test_atomic_json_writer_accepts_nested_domain_objects(self):
        result = PricingResult(
            fair_value=101.2,
            bond_floor=92.0,
            parity=88.0,
            cheapness=-1.3,
            implied_volatility=0.42,
            output_currency="USD",
            diagnostics=Diagnostics(
                steps=80,
                maturity_years=4.0,
                dt=0.05,
                up=1.1,
                down=0.9,
                equity_discount_rate=0.03,
                credit_discount_rate=0.055,
                conversion_ratio=2.0,
                warnings=["check source"],
                details={"as_of": date(2026, 5, 24), "fx_convention": FXConvention.STOCK_PER_CB},
            ),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "valuation_run.json"
            write_json_atomic(path, {"result": result, "written_for": date(2026, 5, 24)})
            payload = read_json(path)

        self.assertEqual(payload["written_for"], "2026-05-24")
        self.assertEqual(payload["result"]["diagnostics"]["details"]["fx_convention"], "STOCK_PER_CB")
        self.assertEqual(payload["result"]["diagnostics"]["warnings"], ["check source"])

    def test_market_payloads_preserve_optional_dates_and_enums(self):
        row = MarketRow(
            as_of_date=date(2026, 5, 24),
            stock_price=22.5,
            bond_price=None,
            market_fx_rate=7.8,
            stock_currency="HKD",
            bond_price_currency="USD",
            fx_convention=FXConvention.STOCK_PER_CB,
            assumption_overrides={"volatility": 0.35},
            source="raw-upload",
            source_row=7,
        )
        snapshot = MarketSnapshot.from_dict(row.to_market_snapshot().to_dict())
        assumptions = Assumptions.from_dict({"volatility": 0.3, "risk_free_rate": 0.04, "valuation_date": None})

        self.assertEqual(MarketRow.from_dict(row.to_dict()), row)
        self.assertEqual(snapshot.as_of_date, date(2026, 5, 24))
        self.assertIs(snapshot.fx_convention, FXConvention.STOCK_PER_CB)
        self.assertIsNone(assumptions.valuation_date)

    def test_web_json_boundary_uses_same_domain_encoder(self):
        payload = {
            "fx_convention": FXConvention.STOCK_PER_CB,
            "valuation_date": date(2026, 5, 24),
            "assumptions": Assumptions(volatility=0.3, risk_free_rate=0.04, valuation_date=date(2026, 5, 24)),
        }

        encoded = json.dumps(payload, default=_json_default, sort_keys=True)
        decoded = json.loads(encoded)

        self.assertEqual(decoded["fx_convention"], "STOCK_PER_CB")
        self.assertEqual(decoded["valuation_date"], "2026-05-24")
        self.assertEqual(decoded["assumptions"]["valuation_date"], "2026-05-24")

    def test_web_atomic_json_writer_accepts_domain_objects(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "web_payload.json"
            web_write_json_atomic(
                path,
                {
                    "market": MarketSnapshot(
                        stock_price=22.5,
                        stock_currency="HKD",
                        fx_rate=7.8,
                        fx_convention=FXConvention.STOCK_PER_CB,
                        as_of_date=date(2026, 5, 24),
                    )
                },
            )
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["market"]["as_of_date"], "2026-05-24")
        self.assertEqual(payload["market"]["fx_convention"], "STOCK_PER_CB")

    def test_sqlite_json_columns_accept_domain_objects(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = CbTerminalStore(Path(tmpdir) / "state.sqlite")
            run = store.create_valuation_run(
                contract_id="XS1234567890_contract",
                assumption_set_id=None,
                model_version="probe",
                run_type="preview",
                inputs={
                    "assumptions": Assumptions(0.3, 0.04, valuation_date=date(2026, 5, 24)),
                    "fx_convention": FXConvention.STOCK_PER_CB,
                },
            )
            loaded_run = store.get_valuation_run(run.id)
            store.save_valuation_results(
                run.id,
                [
                    {
                        "as_of_date": date(2026, 5, 24),
                        "fair_value": 101.2,
                        "diagnostics": {
                            "assumptions": Assumptions(0.3, 0.04, valuation_date=date(2026, 5, 24)),
                            "fx_convention": FXConvention.STOCK_PER_CB,
                        },
                    }
                ],
            )
            results = store.valuation_results(run.id)

        self.assertIsNotNone(loaded_run)
        assert loaded_run is not None
        self.assertEqual(loaded_run.inputs["assumptions"]["valuation_date"], "2026-05-24")
        self.assertEqual(loaded_run.inputs["fx_convention"], "STOCK_PER_CB")
        self.assertEqual(results[0]["as_of_date"], "2026-05-24")
        self.assertEqual(results[0]["diagnostics"]["fx_convention"], "STOCK_PER_CB")


if __name__ == "__main__":
    unittest.main()
