"""Small stdlib SQLite store for CB assumptions and valuation provenance.

Assumption changes are append-only. Saving a PM assumption set creates a row
linked to the prior row for the same contract/scenario, which keeps historical
marks reproducible.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from cb_terminal.domain import Assumptions, dumps_json
from cb_terminal.storage.sqlite_connection import managed_sqlite_connection

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class AssumptionSetRecord:
    id: int
    contract_id: str
    scenario_name: str
    assumptions: Assumptions
    use_yield_curve: bool = False
    yield_curve_currency: str = ""
    notes: str = ""
    created_by: str = "local_gui"
    created_at: str = ""
    supersedes_assumption_set_id: int | None = None


@dataclass(frozen=True)
class ValuationRunRecord:
    id: int
    contract_id: str
    assumption_set_id: int | None
    model_version: str
    run_type: str
    inputs: dict[str, Any]
    created_at: str


class CbTerminalStore:
    """SQLite persistence facade used by the local GUI/API."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def save_assumption_set(
        self,
        *,
        contract_id: str,
        scenario_name: str,
        assumptions: Assumptions,
        use_yield_curve: bool = False,
        yield_curve_currency: str = "",
        notes: str = "",
        created_by: str = "local_gui",
    ) -> AssumptionSetRecord:
        contract_id = contract_id.strip()
        scenario_name = scenario_name.strip() or "base"
        if not contract_id:
            raise ValueError("contract_id is required")
        latest = self.latest_assumption_set(contract_id, scenario_name)
        created_at = _utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO assumption_sets (
                  contract_id, scenario_name, volatility, risk_free_rate,
                  credit_spread, borrow_rate, dividend_yield, steps,
                  use_yield_curve, yield_curve_currency, notes, created_by,
                  created_at, supersedes_assumption_set_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    contract_id,
                    scenario_name,
                    assumptions.volatility,
                    assumptions.risk_free_rate,
                    assumptions.credit_spread,
                    assumptions.borrow_rate,
                    assumptions.dividend_yield,
                    int(assumptions.steps),
                    1 if use_yield_curve else 0,
                    yield_curve_currency.strip().upper(),
                    notes,
                    created_by,
                    created_at,
                    latest.id if latest else None,
                ),
            )
            record_id = int(cursor.lastrowid)
        loaded = self.get_assumption_set(record_id)
        if loaded is None:  # pragma: no cover - sqlite insert/read invariant
            raise RuntimeError("inserted assumption set could not be reloaded")
        return loaded

    def latest_assumption_set(self, contract_id: str, scenario_name: str = "base") -> AssumptionSetRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM assumption_sets
                WHERE contract_id = ? AND scenario_name = ?
                ORDER BY id DESC LIMIT 1
                """,
                (contract_id, scenario_name or "base"),
            ).fetchone()
        return _assumption_record(row) if row else None

    def get_assumption_set(self, assumption_set_id: int) -> AssumptionSetRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM assumption_sets WHERE id = ?", (assumption_set_id,)).fetchone()
        return _assumption_record(row) if row else None

    def create_valuation_run(
        self,
        *,
        contract_id: str,
        assumption_set_id: int | None,
        model_version: str,
        run_type: str,
        inputs: dict[str, Any] | None = None,
    ) -> ValuationRunRecord:
        created_at = _utc_now()
        payload = dumps_json(inputs or {}, sort_keys=True)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO valuation_runs (
                  contract_id, assumption_set_id, model_version, run_type, inputs_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (contract_id, assumption_set_id, model_version, run_type, payload, created_at),
            )
            run_id = int(cursor.lastrowid)
        loaded = self.get_valuation_run(run_id)
        if loaded is None:  # pragma: no cover
            raise RuntimeError("inserted valuation run could not be reloaded")
        return loaded

    def get_valuation_run(self, run_id: int) -> ValuationRunRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM valuation_runs WHERE id = ?", (run_id,)).fetchone()
        return _valuation_run_record(row) if row else None

    def save_valuation_results(self, valuation_run_id: int, rows: Iterable[dict[str, Any]]) -> None:
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO valuation_results (
                  valuation_run_id, as_of_date, fair_value, market_price, cheapness,
                  parity, bond_floor, implied_volatility, delta, gamma, vega,
                  credit_delta, warnings, diagnostics_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        valuation_run_id,
                        str(row.get("as_of_date", "")),
                        row.get("fair_value"),
                        row.get("market_price"),
                        row.get("cheapness"),
                        row.get("parity"),
                        row.get("bond_floor"),
                        row.get("implied_volatility"),
                        row.get("delta"),
                        row.get("gamma"),
                        row.get("vega"),
                        row.get("credit_delta"),
                        row.get("warnings", ""),
                        dumps_json(row.get("diagnostics", {}), sort_keys=True),
                    )
                    for row in rows
                ],
            )

    def valuation_results(self, valuation_run_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM valuation_results WHERE valuation_run_id = ? ORDER BY as_of_date, id",
                (valuation_run_id,),
            ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item.pop("id", None)
            item["diagnostics"] = json.loads(item.pop("diagnostics_json") or "{}")
            results.append(item)
        return results

    def _connect(self) -> AbstractContextManager[sqlite3.Connection]:
        return managed_sqlite_connection(self.path)

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS assumption_sets (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  contract_id TEXT NOT NULL,
                  scenario_name TEXT NOT NULL DEFAULT 'base',
                  volatility REAL NOT NULL,
                  risk_free_rate REAL NOT NULL,
                  credit_spread REAL NOT NULL DEFAULT 0,
                  borrow_rate REAL NOT NULL DEFAULT 0,
                  dividend_yield REAL NOT NULL DEFAULT 0,
                  steps INTEGER NOT NULL DEFAULT 100,
                  use_yield_curve INTEGER NOT NULL DEFAULT 0,
                  yield_curve_currency TEXT NOT NULL DEFAULT '',
                  notes TEXT NOT NULL DEFAULT '',
                  created_by TEXT NOT NULL DEFAULT 'local_gui',
                  created_at TEXT NOT NULL,
                  supersedes_assumption_set_id INTEGER REFERENCES assumption_sets(id)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_assumption_sets_latest
                ON assumption_sets(contract_id, scenario_name, id)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS valuation_runs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  contract_id TEXT NOT NULL,
                  assumption_set_id INTEGER REFERENCES assumption_sets(id),
                  model_version TEXT NOT NULL,
                  run_type TEXT NOT NULL,
                  inputs_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS valuation_results (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  valuation_run_id INTEGER NOT NULL REFERENCES valuation_runs(id) ON DELETE CASCADE,
                  as_of_date TEXT NOT NULL,
                  fair_value REAL,
                  market_price REAL,
                  cheapness REAL,
                  parity REAL,
                  bond_floor REAL,
                  implied_volatility REAL,
                  delta REAL,
                  gamma REAL,
                  vega REAL,
                  credit_delta REAL,
                  warnings TEXT NOT NULL DEFAULT '',
                  diagnostics_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _assumption_record(row: sqlite3.Row) -> AssumptionSetRecord:
    return AssumptionSetRecord(
        id=int(row["id"]),
        contract_id=row["contract_id"],
        scenario_name=row["scenario_name"],
        assumptions=Assumptions(
            volatility=float(row["volatility"]),
            risk_free_rate=float(row["risk_free_rate"]),
            credit_spread=float(row["credit_spread"]),
            borrow_rate=float(row["borrow_rate"]),
            dividend_yield=float(row["dividend_yield"]),
            steps=int(row["steps"]),
        ),
        use_yield_curve=bool(row["use_yield_curve"]),
        yield_curve_currency=row["yield_curve_currency"],
        notes=row["notes"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        supersedes_assumption_set_id=row["supersedes_assumption_set_id"],
    )


def _valuation_run_record(row: sqlite3.Row) -> ValuationRunRecord:
    return ValuationRunRecord(
        id=int(row["id"]),
        contract_id=row["contract_id"],
        assumption_set_id=row["assumption_set_id"],
        model_version=row["model_version"],
        run_type=row["run_type"],
        inputs=json.loads(row["inputs_json"] or "{}"),
        created_at=row["created_at"],
    )
