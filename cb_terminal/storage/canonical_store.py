"""Canonical SQLite metadata/state store for CB Terminal.

This store is the professional-grade backend catalog introduced alongside the
existing JSON/CSV artifacts.  Raw files remain on disk; this database records
canonical identities, contracts, source artifacts, import batches, coverage
membership, FX canonicals, valuation-ready market rows, assumptions, and
valuation-run provenance.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from cb_terminal.domain import dumps_json
from cb_terminal.domain.identity import cb_identity_from_contract

SCHEMA_VERSION = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json_dumps(value: Any) -> str:
    return dumps_json(value if value is not None else {}, sort_keys=True, separators=(",", ":"))


def _json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    return json.loads(value)


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class CanonicalStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def pragma_foreign_keys(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("PRAGMA foreign_keys").fetchone()[0])

    def schema_version(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                  version INTEGER PRIMARY KEY,
                  name TEXT NOT NULL,
                  applied_at TEXT NOT NULL,
                  checksum TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS instruments (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  instrument_key TEXT NOT NULL UNIQUE,
                  instrument_type TEXT NOT NULL,
                  primary_id_scheme TEXT NOT NULL,
                  primary_id TEXT NOT NULL,
                  display_id TEXT NOT NULL DEFAULT '',
                  display_name TEXT NOT NULL DEFAULT '',
                  status TEXT NOT NULL DEFAULT 'active',
                  metadata_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  UNIQUE(instrument_type, primary_id_scheme, primary_id)
                );

                CREATE TABLE IF NOT EXISTS instrument_aliases (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  instrument_id INTEGER NOT NULL REFERENCES instruments(id) ON DELETE CASCADE,
                  alias_scheme TEXT NOT NULL DEFAULT 'source_label',
                  alias_value TEXT NOT NULL,
                  source TEXT NOT NULL DEFAULT '',
                  confidence REAL,
                  created_at TEXT NOT NULL,
                  UNIQUE(alias_scheme, alias_value)
                );

                CREATE TABLE IF NOT EXISTS contracts (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  contract_id TEXT NOT NULL UNIQUE,
                  instrument_id INTEGER NOT NULL REFERENCES instruments(id),
                  display_id TEXT NOT NULL DEFAULT '',
                  issuer TEXT NOT NULL DEFAULT '',
                  status TEXT NOT NULL DEFAULT '',
                  source_path TEXT NOT NULL DEFAULT '',
                  current_version_id INTEGER,
                  raw_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS contract_versions (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  contract_id TEXT NOT NULL REFERENCES contracts(contract_id) ON DELETE CASCADE,
                  version_num INTEGER NOT NULL,
                  status TEXT NOT NULL DEFAULT '',
                  raw_json TEXT NOT NULL DEFAULT '{}',
                  source_path TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  UNIQUE(contract_id, version_num)
                );

                CREATE TABLE IF NOT EXISTS source_files (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  artifact_kind TEXT NOT NULL,
                  original_filename TEXT NOT NULL,
                  original_path TEXT NOT NULL,
                  canonical_path TEXT NOT NULL DEFAULT '',
                  sha256 TEXT NOT NULL,
                  byte_size INTEGER NOT NULL,
                  metadata_json TEXT NOT NULL DEFAULT '{}',
                  status TEXT NOT NULL DEFAULT 'active',
                  created_at TEXT NOT NULL,
                  UNIQUE(artifact_kind, sha256, original_path)
                );

                CREATE TABLE IF NOT EXISTS import_batches (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  source_file_id INTEGER NOT NULL REFERENCES source_files(id),
                  idempotency_key TEXT NOT NULL UNIQUE,
                  processor TEXT NOT NULL,
                  processor_version TEXT NOT NULL,
                  policy_hash TEXT NOT NULL DEFAULT '',
                  status TEXT NOT NULL DEFAULT 'completed',
                  row_count_in INTEGER NOT NULL DEFAULT 0,
                  row_count_out INTEGER NOT NULL DEFAULT 0,
                  warnings_json TEXT NOT NULL DEFAULT '[]',
                  metadata_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL,
                  completed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS coverage_universe (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  universe_name TEXT NOT NULL DEFAULT 'default',
                  instrument_id INTEGER NOT NULL REFERENCES instruments(id),
                  contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
                  instrument_key TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'active',
                  market_history_path TEXT NOT NULL DEFAULT '',
                  raw_price_history_path TEXT NOT NULL DEFAULT '',
                  metadata_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  UNIQUE(universe_name, instrument_id)
                );

                CREATE TABLE IF NOT EXISTS fx_source_canonicals (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  source_file_id INTEGER NOT NULL REFERENCES source_files(id),
                  import_batch_id INTEGER NOT NULL REFERENCES import_batches(id),
                  source_instrument_id TEXT NOT NULL,
                  base_currency TEXT NOT NULL,
                  quote_currency TEXT NOT NULL,
                  pair TEXT NOT NULL,
                  convention TEXT NOT NULL,
                  as_of_date TEXT NOT NULL,
                  rate REAL NOT NULL CHECK(rate > 0),
                  metadata_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL,
                  UNIQUE(source_instrument_id, as_of_date, convention, import_batch_id)
                );

                CREATE TABLE IF NOT EXISTS valuation_market_series (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  series_key TEXT NOT NULL UNIQUE,
                  contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
                  cb_instrument_id INTEGER NOT NULL REFERENCES instruments(id),
                  cb_instrument_key TEXT NOT NULL,
                  equity_instrument_key TEXT NOT NULL DEFAULT '',
                  fx_instrument_key TEXT NOT NULL DEFAULT '',
                  stock_currency TEXT NOT NULL DEFAULT '',
                  bond_price_currency TEXT NOT NULL DEFAULT '',
                  fx_convention TEXT NOT NULL DEFAULT '',
                  selection_policy TEXT NOT NULL DEFAULT '',
                  source_file_id INTEGER REFERENCES source_files(id),
                  import_batch_id INTEGER REFERENCES import_batches(id),
                  metadata_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS valuation_market_rows (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  series_id INTEGER NOT NULL REFERENCES valuation_market_series(id) ON DELETE CASCADE,
                  as_of_date TEXT NOT NULL,
                  stock_price REAL,
                  bond_price REAL,
                  market_fx_rate REAL,
                  stock_currency TEXT NOT NULL DEFAULT '',
                  bond_price_currency TEXT NOT NULL DEFAULT '',
                  fx_convention TEXT NOT NULL DEFAULT '',
                  cb_quote_time TEXT NOT NULL DEFAULT '',
                  cb_quote_dealer TEXT NOT NULL DEFAULT '',
                  cb_reference_security TEXT NOT NULL DEFAULT '',
                  cb_selection_reason TEXT NOT NULL DEFAULT '',
                  raw_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  UNIQUE(series_id, as_of_date)
                );

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
                );

                CREATE TABLE IF NOT EXISTS valuation_runs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  contract_id TEXT NOT NULL,
                  assumption_set_id INTEGER REFERENCES assumption_sets(id),
                  model_version TEXT NOT NULL,
                  run_type TEXT NOT NULL,
                  inputs_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL
                );

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
                );

                CREATE INDEX IF NOT EXISTS idx_assumption_sets_latest ON assumption_sets(contract_id, scenario_name, id);
                CREATE INDEX IF NOT EXISTS idx_valuation_runs_contract ON valuation_runs(contract_id, id);

                CREATE INDEX IF NOT EXISTS idx_contracts_instrument ON contracts(instrument_id);
                CREATE INDEX IF NOT EXISTS idx_coverage_contract ON coverage_universe(contract_id);
                CREATE INDEX IF NOT EXISTS idx_valuation_rows_date ON valuation_market_rows(series_id, as_of_date);
                """
            )
            now = _utc_now()
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, name, applied_at, checksum) VALUES (?, ?, ?, ?)",
                (SCHEMA_VERSION, "initial_canonical_catalog", now, "v1"),
            )
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()

    def upsert_instrument(
        self,
        *,
        instrument_key: str,
        instrument_type: str,
        primary_id_scheme: str,
        primary_id: str,
        display_id: str = "",
        display_name: str = "",
        aliases: Iterable[str] = (),
        status: str = "active",
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO instruments(instrument_key, instrument_type, primary_id_scheme, primary_id, display_id, display_name, status, metadata_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(instrument_key) DO UPDATE SET
                  instrument_type=excluded.instrument_type,
                  primary_id_scheme=excluded.primary_id_scheme,
                  primary_id=excluded.primary_id,
                  display_id=excluded.display_id,
                  display_name=excluded.display_name,
                  status=excluded.status,
                  metadata_json=excluded.metadata_json,
                  updated_at=excluded.updated_at
                """,
                (
                    instrument_key,
                    instrument_type,
                    primary_id_scheme.upper(),
                    primary_id,
                    display_id,
                    display_name,
                    status,
                    _json_dumps(metadata or {}),
                    now,
                    now,
                ),
            )
            inst_id = int(conn.execute("SELECT id FROM instruments WHERE instrument_key = ?", (instrument_key,)).fetchone()[0])
            for alias in aliases:
                alias_value = str(alias or "").strip()
                if not alias_value:
                    continue
                conn.execute(
                    """
                    INSERT OR IGNORE INTO instrument_aliases(instrument_id, alias_scheme, alias_value, source, confidence, created_at)
                    VALUES (?, 'source_label', ?, '', NULL, ?)
                    """,
                    (inst_id, alias_value, now),
                )
            conn.commit()
        return self.get_instrument_by_key(instrument_key) or {}

    def get_instrument_by_key(self, instrument_key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = _row_dict(conn.execute("SELECT * FROM instruments WHERE instrument_key = ?", (instrument_key,)).fetchone())
            if not row:
                return None
            aliases = [r[0] for r in conn.execute("SELECT alias_value FROM instrument_aliases WHERE instrument_id = ? ORDER BY alias_value", (row["id"],)).fetchall()]
        row["aliases"] = aliases
        row["metadata"] = _json_loads(row.pop("metadata_json"), {})
        return row

    def upsert_contract(self, raw: Mapping[str, Any], *, source_path: str = "") -> dict[str, Any]:
        identity = cb_identity_from_contract(raw, fallback_id=str(raw.get("id") or ""))
        contract_id = identity.contract_id
        if identity.primary_id_scheme == "ISIN" and raw.get("id") and str(raw.get("id")) != contract_id:
            raise ValueError(f"finalized CB contract id must be {contract_id!r}, got {raw.get('id')!r}")
        instrument = self.upsert_instrument(
            instrument_key=identity.instrument_key,
            instrument_type=identity.instrument_type,
            primary_id_scheme=identity.primary_id_scheme,
            primary_id=identity.primary_id,
            display_id=identity.display_id,
            display_name=identity.display_name,
            aliases=identity.aliases,
            metadata={"contract_id": contract_id},
        )
        issuer_mapping = raw.get("issuer") if isinstance(raw.get("issuer"), Mapping) else {}
        issuer = str(issuer_mapping.get("name") or raw.get("issuer_name") or raw.get("issuer") or "")
        status = str(raw.get("status") or "")
        now = _utc_now()
        raw_json = _json_dumps(dict(raw))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO contracts(contract_id, instrument_id, display_id, issuer, status, source_path, raw_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(contract_id) DO UPDATE SET
                  instrument_id=excluded.instrument_id,
                  display_id=excluded.display_id,
                  issuer=excluded.issuer,
                  status=excluded.status,
                  source_path=excluded.source_path,
                  raw_json=excluded.raw_json,
                  updated_at=excluded.updated_at
                """,
                (contract_id, instrument["id"], identity.display_id, issuer, status, source_path, raw_json, now, now),
            )
            latest = conn.execute(
                "SELECT id, version_num, status, raw_json, source_path FROM contract_versions WHERE contract_id = ? ORDER BY version_num DESC LIMIT 1",
                (contract_id,),
            ).fetchone()
            if latest is None:
                version_num = 1
                conn.execute(
                    "INSERT INTO contract_versions(contract_id, version_num, status, raw_json, source_path, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (contract_id, version_num, status, raw_json, source_path, now),
                )
                version_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            elif str(latest["raw_json"] or "") == raw_json and str(latest["source_path"] or "") == source_path and str(latest["status"] or "") == status:
                version_id = int(latest["id"])
            else:
                version_num = int(latest["version_num"]) + 1
                conn.execute(
                    "INSERT INTO contract_versions(contract_id, version_num, status, raw_json, source_path, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (contract_id, version_num, status, raw_json, source_path, now),
                )
                version_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute("UPDATE contracts SET current_version_id = ? WHERE contract_id = ?", (version_id, contract_id))
            conn.commit()
        return self.get_contract(contract_id) or {}

    def get_contract(self, contract_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = _row_dict(
                conn.execute(
                    """
                    SELECT c.*, i.instrument_key, i.primary_id_scheme, i.primary_id, i.display_name
                    FROM contracts c JOIN instruments i ON c.instrument_id = i.id
                    WHERE c.contract_id = ?
                    """,
                    (contract_id,),
                ).fetchone()
            )
        if not row:
            return None
        row["raw_json"] = _json_loads(row["raw_json"], {})
        return row

    def register_source_file(self, path: str | Path, *, artifact_kind: str, metadata: Mapping[str, Any] | None = None, canonical_path: str = "") -> dict[str, Any]:
        source_path = Path(path).expanduser().resolve()
        data = source_path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO source_files(artifact_kind, original_filename, original_path, canonical_path, sha256, byte_size, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (artifact_kind, source_path.name, str(source_path), canonical_path, sha, len(data), _json_dumps(metadata or {}), now),
            )
            row = _row_dict(conn.execute("SELECT * FROM source_files WHERE artifact_kind = ? AND sha256 = ? AND original_path = ?", (artifact_kind, sha, str(source_path))).fetchone())
            conn.commit()
        assert row is not None
        row["metadata"] = _json_loads(row.pop("metadata_json"), {})
        return row

    def deactivate_source_file(self, source_file_id: int, *, reason: str = "") -> None:
        now = _utc_now()
        with self._connect() as conn:
            existing = conn.execute("SELECT metadata_json FROM source_files WHERE id = ?", (source_file_id,)).fetchone()
            if existing is None:
                return
            metadata = _json_loads(existing["metadata_json"], {})
            metadata["deactivated_reason"] = reason
            metadata["deactivated_at"] = now
            conn.execute(
                "UPDATE source_files SET status = 'inactive', metadata_json = ? WHERE id = ?",
                (_json_dumps(metadata), source_file_id),
            )
            conn.commit()

    def record_import_batch(
        self,
        *,
        source_file_id: int,
        processor: str,
        processor_version: str,
        policy_hash: str = "",
        row_count_in: int = 0,
        row_count_out: int = 0,
        warnings: Iterable[str] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        idempotency_key = hashlib.sha256(f"{source_file_id}|{processor}|{processor_version}|{policy_hash}".encode()).hexdigest()
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO import_batches(source_file_id, idempotency_key, processor, processor_version, policy_hash, status, row_count_in, row_count_out, warnings_json, metadata_json, created_at, completed_at)
                VALUES (?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?)
                """,
                (source_file_id, idempotency_key, processor, processor_version, policy_hash, row_count_in, row_count_out, _json_dumps(list(warnings)), _json_dumps(metadata or {}), now, now),
            )
            row = _row_dict(conn.execute("SELECT * FROM import_batches WHERE idempotency_key = ?", (idempotency_key,)).fetchone())
            conn.commit()
        assert row is not None
        row["warnings"] = _json_loads(row.pop("warnings_json"), [])
        row["metadata"] = _json_loads(row.pop("metadata_json"), {})
        return row

    def upsert_coverage_member(
        self,
        *,
        universe_name: str = "default",
        contract_id: str,
        instrument_key: str,
        status: str = "active",
        market_history_path: str = "",
        raw_price_history_path: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        inst = self.get_instrument_by_key(instrument_key)
        if not inst:
            raise ValueError(f"unknown instrument_key {instrument_key!r}")
        if not self.get_contract(contract_id):
            raise ValueError(f"unknown contract_id {contract_id!r}")
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO coverage_universe(universe_name, instrument_id, contract_id, instrument_key, status, market_history_path, raw_price_history_path, metadata_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(universe_name, instrument_id) DO UPDATE SET
                  contract_id=excluded.contract_id,
                  instrument_key=excluded.instrument_key,
                  status=excluded.status,
                  market_history_path=COALESCE(NULLIF(excluded.market_history_path, ''), coverage_universe.market_history_path),
                  raw_price_history_path=COALESCE(NULLIF(excluded.raw_price_history_path, ''), coverage_universe.raw_price_history_path),
                  metadata_json=excluded.metadata_json,
                  updated_at=excluded.updated_at
                """,
                (universe_name, inst["id"], contract_id, instrument_key, status, market_history_path, raw_price_history_path, _json_dumps(metadata or {}), now, now),
            )
            row = _row_dict(conn.execute("SELECT * FROM coverage_universe WHERE universe_name = ? AND instrument_id = ?", (universe_name, inst["id"])).fetchone())
            conn.commit()
        assert row is not None
        row["metadata"] = _json_loads(row.pop("metadata_json"), {})
        return row

    def coverage_member_for_contract(self, contract_id: str, *, universe_name: str = "default") -> dict[str, Any] | None:
        with self._connect() as conn:
            row = _row_dict(
                conn.execute(
                    "SELECT * FROM coverage_universe WHERE universe_name = ? AND contract_id = ? ORDER BY id LIMIT 1",
                    (universe_name, contract_id),
                ).fetchone()
            )
        if row:
            row["metadata"] = _json_loads(row.pop("metadata_json"), {})
        return row

    def record_fx_canonical(self, *, source_file_id: int, import_batch_id: int, source_instrument_id: str, base_currency: str, quote_currency: str, convention: str, as_of_date: str, rate: float, metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if rate <= 0:
            raise ValueError("FX rate must be positive")
        base = base_currency.upper().strip()
        quote = quote_currency.upper().strip()
        conv = convention.upper().strip()
        pair = f"{base}{quote}"
        now = _utc_now()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM fx_source_canonicals WHERE source_instrument_id = ? AND as_of_date = ? AND convention = ? AND import_batch_id = ?",
                (source_instrument_id, as_of_date, conv, import_batch_id),
            ).fetchone()
            if existing is not None:
                existing_row = dict(existing)
                if (
                    existing_row["base_currency"] != base
                    or existing_row["quote_currency"] != quote
                    or existing_row["pair"] != pair
                    or abs(float(existing_row["rate"]) - float(rate)) > 1e-12
                ):
                    raise ValueError("conflicting FX canonical row for same source instrument/date/convention/import batch")
            conn.execute(
                """
                INSERT OR IGNORE INTO fx_source_canonicals(source_file_id, import_batch_id, source_instrument_id, base_currency, quote_currency, pair, convention, as_of_date, rate, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (source_file_id, import_batch_id, source_instrument_id, base, quote, pair, conv, as_of_date, rate, _json_dumps(metadata or {}), now),
            )
            row = _row_dict(conn.execute("SELECT * FROM fx_source_canonicals WHERE source_instrument_id = ? AND as_of_date = ? AND convention = ? AND import_batch_id = ?", (source_instrument_id, as_of_date, conv, import_batch_id)).fetchone())
            conn.commit()
        assert row is not None
        row["metadata"] = _json_loads(row.pop("metadata_json"), {})
        return row

    def create_valuation_market_series(self, *, contract_id: str, cb_instrument_key: str, equity_instrument_key: str = "", fx_instrument_key: str = "", stock_currency: str = "", bond_price_currency: str = "", fx_convention: str = "", selection_policy: str = "", source_file_id: int | None = None, import_batch_id: int | None = None, metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
        inst = self.get_instrument_by_key(cb_instrument_key)
        if not inst:
            raise ValueError(f"unknown cb_instrument_key {cb_instrument_key!r}")
        series_key = hashlib.sha256(
            f"{contract_id}|{cb_instrument_key}|{equity_instrument_key}|{fx_instrument_key}|{selection_policy}|source:{source_file_id or ''}|batch:{import_batch_id or ''}".encode()
        ).hexdigest()
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO valuation_market_series(series_key, contract_id, cb_instrument_id, cb_instrument_key, equity_instrument_key, fx_instrument_key, stock_currency, bond_price_currency, fx_convention, selection_policy, source_file_id, import_batch_id, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (series_key, contract_id, inst["id"], cb_instrument_key, equity_instrument_key, fx_instrument_key, stock_currency, bond_price_currency, fx_convention, selection_policy, source_file_id, import_batch_id, _json_dumps(metadata or {}), now),
            )
            row = _row_dict(conn.execute("SELECT * FROM valuation_market_series WHERE series_key = ?", (series_key,)).fetchone())
            conn.commit()
        assert row is not None
        row["metadata"] = _json_loads(row.pop("metadata_json"), {})
        return row

    def valuation_series_for_contract(self, contract_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = _row_dict(conn.execute("SELECT * FROM valuation_market_series WHERE contract_id = ? ORDER BY id LIMIT 1", (contract_id,)).fetchone())
        if row:
            row["metadata"] = _json_loads(row.pop("metadata_json"), {})
        return row

    def save_valuation_market_rows(self, series_id: int, rows: Iterable[Mapping[str, Any]]) -> None:
        now = _utc_now()
        with self._connect() as conn:
            series = conn.execute("SELECT stock_currency, bond_price_currency, fx_convention FROM valuation_market_series WHERE id = ?", (series_id,)).fetchone()
            if series is None:
                raise sqlite3.IntegrityError(f"unknown valuation market series {series_id}")
            for row in rows:
                raw = row.get("raw", {})
                stock_currency = str(row.get("stock_currency") or series["stock_currency"] or "").upper()
                bond_price_currency = str(row.get("bond_price_currency") or series["bond_price_currency"] or "").upper()
                fx_convention = str(row.get("fx_convention") or series["fx_convention"] or "").upper()
                if stock_currency and bond_price_currency and stock_currency != bond_price_currency:
                    existing_fx = conn.execute("SELECT market_fx_rate, fx_convention FROM valuation_market_rows WHERE series_id = ? AND as_of_date = ?", (series_id, str(row["as_of_date"]))).fetchone()
                    market_fx_rate = row.get("market_fx_rate") if row.get("market_fx_rate") is not None else (existing_fx["market_fx_rate"] if existing_fx else None)
                    effective_fx_convention = fx_convention or (str(existing_fx["fx_convention"] or "").upper() if existing_fx else "")
                    if market_fx_rate is None:
                        raise ValueError("cross-currency valuation market rows require market_fx_rate")
                    if float(market_fx_rate) <= 0:
                        raise ValueError("cross-currency valuation market rows require positive market_fx_rate")
                    if effective_fx_convention not in {"STOCK_PER_CB", "CB_PER_STOCK"}:
                        raise ValueError("cross-currency valuation market rows require fx_convention STOCK_PER_CB or CB_PER_STOCK")
                conn.execute(
                    """
                    INSERT INTO valuation_market_rows(series_id, as_of_date, stock_price, bond_price, market_fx_rate, stock_currency, bond_price_currency, fx_convention, cb_quote_time, cb_quote_dealer, cb_reference_security, cb_selection_reason, raw_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(series_id, as_of_date) DO UPDATE SET
                      stock_price=COALESCE(excluded.stock_price, valuation_market_rows.stock_price),
                      bond_price=COALESCE(excluded.bond_price, valuation_market_rows.bond_price),
                      market_fx_rate=COALESCE(excluded.market_fx_rate, valuation_market_rows.market_fx_rate),
                      stock_currency=COALESCE(NULLIF(excluded.stock_currency, ''), valuation_market_rows.stock_currency),
                      bond_price_currency=COALESCE(NULLIF(excluded.bond_price_currency, ''), valuation_market_rows.bond_price_currency),
                      fx_convention=COALESCE(NULLIF(excluded.fx_convention, ''), valuation_market_rows.fx_convention),
                      cb_quote_time=COALESCE(NULLIF(excluded.cb_quote_time, ''), valuation_market_rows.cb_quote_time),
                      cb_quote_dealer=COALESCE(NULLIF(excluded.cb_quote_dealer, ''), valuation_market_rows.cb_quote_dealer),
                      cb_reference_security=COALESCE(NULLIF(excluded.cb_reference_security, ''), valuation_market_rows.cb_reference_security),
                      cb_selection_reason=COALESCE(NULLIF(excluded.cb_selection_reason, ''), valuation_market_rows.cb_selection_reason),
                      raw_json=CASE WHEN excluded.raw_json != '{}' THEN excluded.raw_json ELSE valuation_market_rows.raw_json END,
                      updated_at=excluded.updated_at
                    """,
                    (
                        series_id,
                        str(row["as_of_date"]),
                        row.get("stock_price"),
                        row.get("bond_price"),
                        row.get("market_fx_rate"),
                        stock_currency,
                        bond_price_currency,
                        fx_convention,
                        str(row.get("cb_quote_time") or ""),
                        str(row.get("cb_quote_dealer") or ""),
                        str(row.get("cb_reference_security") or ""),
                        str(row.get("cb_selection_reason") or ""),
                        _json_dumps(raw),
                        now,
                        now,
                    ),
                )
            conn.commit()

    def valuation_market_rows(self, series_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = [dict(row) for row in conn.execute("SELECT * FROM valuation_market_rows WHERE series_id = ? ORDER BY as_of_date", (series_id,)).fetchall()]
        for row in rows:
            row["raw"] = _json_loads(row.pop("raw_json"), {})
        return rows
