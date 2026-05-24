"""SQLite database for raw CB quote/price-history imports."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from cb_terminal.domain import dumps_json
from cb_terminal.domain.identity import InstrumentIdentityRef, identity_from_observation
from cb_terminal.io.market_data_history import MarketDataPoint, load_market_data_file
from cb_terminal.io.price_history import PriceQuoteRow, load_price_history_file

PRICE_HISTORY_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class PriceHistoryImportBatch:
    id: int
    source_file: str
    source_sha256: str
    row_count: int
    imported_at: str
    notes: str = ""


class PriceHistoryStore:
    """Dedicated SQLite store for observed CB quote history.

    This database is separate from assumption/valuation provenance because raw
    dealer quotes have different lifecycle and provenance semantics.  Imports are
    batch-tracked by file hash and row number so repeated imports are idempotent.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def import_file(
        self,
        path: str | Path,
        *,
        instrument_id: str = "",
        contract_id: str = "",
        notes: str = "",
    ) -> PriceHistoryImportBatch:
        source = Path(path)
        digest = _sha256(source)
        rows = load_price_history_file(source, instrument_id=instrument_id, contract_id=contract_id)
        imported_at = _utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO price_history_import_batches (
                  source_file, source_sha256, row_count, imported_at, notes
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (str(source), digest, len(rows), imported_at, notes),
            )
            batch_id = int(cursor.lastrowid)
            self._insert_quotes(conn, batch_id, rows)
        loaded = self.get_import_batch(batch_id)
        if loaded is None:  # pragma: no cover - sqlite invariant
            raise RuntimeError("inserted import batch could not be reloaded")
        return loaded

    def import_market_data_file(self, path: str | Path, *, notes: str = "") -> PriceHistoryImportBatch:
        source = Path(path)
        digest = _sha256(source)
        rows = load_market_data_file(source)
        imported_at = _utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO price_history_import_batches (
                  source_file, source_sha256, row_count, imported_at, notes
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (str(source), digest, len(rows), imported_at, notes),
            )
            batch_id = int(cursor.lastrowid)
            self._insert_market_data(conn, batch_id, rows)
        loaded = self.get_import_batch(batch_id)
        if loaded is None:  # pragma: no cover
            raise RuntimeError("inserted import batch could not be reloaded")
        return loaded

    def import_detected_rows(
        self,
        path: str | Path,
        *,
        quote_rows: Iterable[PriceQuoteRow] = (),
        market_data_points: Iterable[MarketDataPoint] = (),
        notes: str = "",
    ) -> PriceHistoryImportBatch:
        """Import already-classified quote and market-data rows from one mixed source file."""

        source = Path(path)
        digest = _sha256(source)
        quotes = list(quote_rows)
        points = list(market_data_points)
        imported_at = _utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO price_history_import_batches (
                  source_file, source_sha256, row_count, imported_at, notes
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (str(source), digest, len(quotes) + len(points), imported_at, notes),
            )
            batch_id = int(cursor.lastrowid)
            self._insert_quotes(conn, batch_id, quotes)
            self._insert_market_data(conn, batch_id, points)
        loaded = self.get_import_batch(batch_id)
        if loaded is None:  # pragma: no cover
            raise RuntimeError("inserted import batch could not be reloaded")
        return loaded

    def get_import_batch(self, batch_id: int) -> PriceHistoryImportBatch | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM price_history_import_batches WHERE id = ?", (batch_id,)).fetchone()
        return _batch_record(row) if row else None

    def quote_count(self, *, instrument_id: str = "", contract_id: str = "", instrument_key: str = "") -> int:
        where, params = _quote_filters(instrument_id=instrument_id, contract_id=contract_id, instrument_key=instrument_key)
        with self._connect() as conn:
            row = conn.execute(f"SELECT COUNT(*) AS count FROM cb_price_quotes{where}", params).fetchone()
        return int(row["count"])

    def quote_date_range(self, *, instrument_id: str = "", contract_id: str = "", instrument_key: str = "") -> dict[str, Any]:
        where, params = _quote_filters(instrument_id=instrument_id, contract_id=contract_id, instrument_key=instrument_key)
        with self._connect() as conn:
            row = conn.execute(f"SELECT COUNT(*) AS count, MIN(as_of_date) AS first_date, MAX(as_of_date) AS latest_date FROM cb_price_quotes{where}", params).fetchone()
        return {"count": int(row["count"] or 0), "first_date": row["first_date"] or "", "latest_date": row["latest_date"] or ""}

    def quote_source_files(self, *, instrument_id: str = "", contract_id: str = "", instrument_key: str = "") -> list[dict[str, Any]]:
        where, params = _quote_filters(instrument_id=instrument_id, contract_id=contract_id, instrument_key=instrument_key)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT source_file, COUNT(*) AS row_count, MIN(as_of_date) AS first_date, MAX(as_of_date) AS latest_date
                FROM cb_price_quotes{where}
                GROUP BY source_file
                ORDER BY latest_date DESC, source_file
                """,
                params,
            ).fetchall()
        return [{"source_file": row["source_file"] or "", "row_count": int(row["row_count"] or 0), "first_date": row["first_date"] or "", "latest_date": row["latest_date"] or ""} for row in rows]

    def market_data_date_range(self, *, instrument_id: str = "", instrument_type: str = "") -> dict[str, Any]:
        where, params = _market_data_filters(instrument_id=instrument_id, instrument_type=instrument_type)
        with self._connect() as conn:
            row = conn.execute(f"SELECT COUNT(*) AS count, MIN(as_of_date) AS first_date, MAX(as_of_date) AS latest_date FROM market_data_points{where}", params).fetchone()
        return {"count": int(row["count"] or 0), "first_date": row["first_date"] or "", "latest_date": row["latest_date"] or ""}

    def market_data_source_files(self, *, instrument_id: str = "", instrument_type: str = "") -> list[dict[str, Any]]:
        where, params = _market_data_filters(instrument_id=instrument_id, instrument_type=instrument_type)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT source_file, COUNT(*) AS row_count, MIN(as_of_date) AS first_date, MAX(as_of_date) AS latest_date
                FROM market_data_points{where}
                GROUP BY source_file
                ORDER BY latest_date DESC, source_file
                """,
                params,
            ).fetchall()
        return [{"source_file": row["source_file"] or "", "row_count": int(row["row_count"] or 0), "first_date": row["first_date"] or "", "latest_date": row["latest_date"] or ""} for row in rows]

    def latest_quotes(self, *, instrument_id: str = "", contract_id: str = "", instrument_key: str = "", limit: int = 20) -> list[dict[str, Any]]:
        where, params = _quote_filters(instrument_id=instrument_id, contract_id=contract_id, instrument_key=instrument_key)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM cb_price_quotes{where}
                ORDER BY as_of_date DESC, as_of_time DESC, id DESC
                LIMIT ?
                """,
                [*params, int(limit)],
            ).fetchall()
        return [_quote_dict(row) for row in rows]

    def instrument_identities(self, *, instrument_type: str = "") -> list[dict[str, Any]]:
        clauses = []
        params: list[str] = []
        if instrument_type:
            clauses.append("instrument_type = ?")
            params.append(instrument_type)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM instrument_identities{where}
                ORDER BY instrument_type, instrument_key
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def market_data_count(self, *, instrument_id: str = "", instrument_type: str = "") -> int:
        where, params = _market_data_filters(instrument_id=instrument_id, instrument_type=instrument_type)
        with self._connect() as conn:
            row = conn.execute(f"SELECT COUNT(*) AS count FROM market_data_points{where}", params).fetchone()
        return int(row["count"])

    def market_data_points(self, *, instrument_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM market_data_points
                WHERE instrument_id = ?
                ORDER BY as_of_date DESC, id DESC
                LIMIT ?
                """,
                (instrument_id, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def build_valuation_market_rows(
        self,
        *,
        cb_instrument_id: str,
        equity_instrument_id: str,
        fx_instrument_id: str = "",
        stock_currency: str = "",
        bond_price_currency: str = "",
        fx_convention: str = "",
        quote_policy: str = "latest",
    ) -> list[dict[str, Any]]:
        if quote_policy != "latest":
            raise ValueError("only quote_policy='latest' is currently supported")
        with self._connect() as conn:
            all_quote_rows = conn.execute(
                """
                SELECT q.* FROM cb_price_quotes q
                WHERE q.instrument_key = ? OR q.instrument_id = ?
                ORDER BY q.as_of_date, COALESCE(NULLIF(q.as_of_time, ''), '00:00'), q.id
                """,
                (cb_instrument_id, cb_instrument_id),
            ).fetchall()
            equity_by_date = _points_by_date(
                conn.execute(
                    """
                    SELECT * FROM market_data_points
                    WHERE instrument_id = ? AND field IN ('PX_LAST', 'Last Price')
                    ORDER BY as_of_date, id
                    """,
                    (equity_instrument_id,),
                ).fetchall()
            )
            fx_by_date = _points_by_date(
                conn.execute(
                    """
                    SELECT * FROM market_data_points
                    WHERE instrument_id = ? AND field IN ('PX_LAST', 'Last Price')
                    ORDER BY as_of_date, id
                    """,
                    (fx_instrument_id,),
                ).fetchall()
            ) if fx_instrument_id else {}
        quote_rows = _select_clean_daily_quotes(all_quote_rows, equity_by_date)
        rows: list[dict[str, Any]] = []
        for quote, selection_reason in quote_rows:
            as_of_date = quote["as_of_date"]
            equity = equity_by_date.get(as_of_date)
            if equity is None:
                continue
            fx = fx_by_date.get(as_of_date) if fx_instrument_id else None
            rows.append(
                {
                    "date": as_of_date,
                    "stock_price": equity["value"],
                    "bond_price": quote["mid_price"],
                    "market_fx_rate": fx["value"] if fx is not None else 1.0,
                    "stock_currency": stock_currency,
                    "bond_price_currency": bond_price_currency,
                    "fx_convention": fx_convention,
                    "cb_instrument_id": quote["instrument_id"],
                    "cb_reference_security": quote["reference_security"],
                    "cb_contract_id": quote["contract_id"],
                    "cb_quote_time": quote["as_of_time"],
                    "cb_quote_dealer": quote["dealer"],
                    "cb_bid_price": quote["bid_price"],
                    "cb_ask_price": quote["ask_price"],
                    "cb_selection_reason": selection_reason,
                    "equity_instrument_id": equity_instrument_id,
                    "fx_instrument_id": fx_instrument_id,
                }
            )
        return rows

    def _insert_quotes(self, conn: sqlite3.Connection, batch_id: int, rows: Iterable[PriceQuoteRow]) -> None:
        row_list = list(rows)
        identities = [_quote_identity(row) for row in row_list]
        self._upsert_instrument_identities(conn, identities)
        conn.executemany(
            """
            INSERT OR IGNORE INTO cb_price_quotes (
              import_batch_id, instrument_key, primary_id_scheme, primary_id,
              contract_id, instrument_id, reference_security,
              as_of_date, as_of_time, dealer, source_type, security,
              bid_price, ask_price, mid_price, sender_name, subject, keyword,
              source_file, source_sheet, source_row, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    batch_id,
                    identity.instrument_key,
                    identity.primary_id_scheme,
                    identity.primary_id,
                    row.contract_id,
                    row.instrument_id,
                    row.reference_security,
                    row.as_of_date.isoformat(),
                    row.as_of_time.isoformat(timespec="minutes") if row.as_of_time else "",
                    row.dealer,
                    row.source_type,
                    row.security,
                    row.bid_price,
                    row.ask_price,
                    row.mid_price,
                    row.sender_name,
                    row.subject,
                    row.keyword,
                    row.source_file,
                    row.source_sheet,
                    row.source_row,
                    dumps_json(_raw_payload(row), sort_keys=True),
                )
                for row, identity in zip(row_list, identities)
            ],
        )

    def _insert_market_data(self, conn: sqlite3.Connection, batch_id: int, rows: Iterable[MarketDataPoint]) -> None:
        row_list = list(rows)
        identities = [_market_data_identity(row) for row in row_list]
        self._upsert_instrument_identities(conn, identities)
        conn.executemany(
            """
            INSERT OR IGNORE INTO market_data_points (
              import_batch_id, instrument_key, primary_id_scheme, primary_id,
              instrument_id, instrument_type, as_of_date, field,
              value, source_file, source_sheet, source_row, source_column
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    batch_id,
                    identity.instrument_key,
                    identity.primary_id_scheme,
                    identity.primary_id,
                    row.instrument_id,
                    row.instrument_type,
                    row.as_of_date.isoformat(),
                    row.field,
                    row.value,
                    row.source_file,
                    row.source_sheet,
                    row.source_row,
                    row.source_column,
                )
                for row, identity in zip(row_list, identities)
            ],
        )

    def _upsert_instrument_identities(self, conn: sqlite3.Connection, identities: Iterable[InstrumentIdentityRef]) -> None:
        conn.executemany(
            """
            INSERT INTO instrument_identities (
              instrument_key, instrument_type, primary_id_scheme, primary_id,
              display_name, contract_id, aliases_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(instrument_key) DO UPDATE SET
              instrument_type=excluded.instrument_type,
              primary_id_scheme=excluded.primary_id_scheme,
              primary_id=excluded.primary_id,
              display_name=COALESCE(NULLIF(excluded.display_name, ''), instrument_identities.display_name),
              contract_id=COALESCE(NULLIF(excluded.contract_id, ''), instrument_identities.contract_id),
              aliases_json=excluded.aliases_json
            """,
            [
                (
                    identity.instrument_key,
                    identity.instrument_type,
                    identity.primary_id_scheme,
                    identity.primary_id,
                    identity.display_name,
                    identity.contract_id,
                    dumps_json(list(identity.aliases), sort_keys=True),
                )
                for identity in identities
            ],
        )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA user_version = %d" % PRICE_HISTORY_SCHEMA_VERSION)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS instrument_identities (
                  instrument_key TEXT PRIMARY KEY,
                  instrument_type TEXT NOT NULL,
                  primary_id_scheme TEXT NOT NULL,
                  primary_id TEXT NOT NULL,
                  display_name TEXT NOT NULL DEFAULT '',
                  contract_id TEXT NOT NULL DEFAULT '',
                  aliases_json TEXT NOT NULL DEFAULT '[]'
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS price_history_import_batches (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  source_file TEXT NOT NULL,
                  source_sha256 TEXT NOT NULL,
                  row_count INTEGER NOT NULL,
                  imported_at TEXT NOT NULL,
                  notes TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cb_price_quotes (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  import_batch_id INTEGER NOT NULL REFERENCES price_history_import_batches(id) ON DELETE CASCADE,
                  instrument_key TEXT NOT NULL DEFAULT '',
                  primary_id_scheme TEXT NOT NULL DEFAULT '',
                  primary_id TEXT NOT NULL DEFAULT '',
                  contract_id TEXT NOT NULL DEFAULT '',
                  instrument_id TEXT NOT NULL,
                  reference_security TEXT NOT NULL,
                  as_of_date TEXT NOT NULL,
                  as_of_time TEXT NOT NULL DEFAULT '',
                  dealer TEXT NOT NULL DEFAULT '',
                  source_type TEXT NOT NULL DEFAULT '',
                  security TEXT NOT NULL DEFAULT '',
                  bid_price REAL,
                  ask_price REAL,
                  mid_price REAL,
                  sender_name TEXT NOT NULL DEFAULT '',
                  subject TEXT NOT NULL DEFAULT '',
                  keyword TEXT NOT NULL DEFAULT '',
                  source_file TEXT NOT NULL,
                  source_sheet TEXT NOT NULL DEFAULT '',
                  source_row INTEGER NOT NULL,
                  raw_json TEXT NOT NULL DEFAULT '{}',
                  UNIQUE(source_file, source_sheet, source_row, reference_security, as_of_date, as_of_time, dealer)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_cb_price_quotes_instrument_date
                ON cb_price_quotes(instrument_id, as_of_date, as_of_time)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_cb_price_quotes_instrument_key_date
                ON cb_price_quotes(instrument_key, as_of_date, as_of_time)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_cb_price_quotes_contract_date
                ON cb_price_quotes(contract_id, as_of_date, as_of_time)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS market_data_points (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  import_batch_id INTEGER NOT NULL REFERENCES price_history_import_batches(id) ON DELETE CASCADE,
                  instrument_key TEXT NOT NULL DEFAULT '',
                  primary_id_scheme TEXT NOT NULL DEFAULT '',
                  primary_id TEXT NOT NULL DEFAULT '',
                  instrument_id TEXT NOT NULL,
                  instrument_type TEXT NOT NULL DEFAULT '',
                  as_of_date TEXT NOT NULL,
                  field TEXT NOT NULL DEFAULT 'PX_LAST',
                  value REAL NOT NULL,
                  source_file TEXT NOT NULL,
                  source_sheet TEXT NOT NULL DEFAULT '',
                  source_row INTEGER NOT NULL,
                  source_column TEXT NOT NULL DEFAULT '',
                  UNIQUE(source_file, source_sheet, source_row, source_column, instrument_id, field)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_market_data_points_instrument_key_date
                ON market_data_points(instrument_key, as_of_date, field)
                """
            )
            _ensure_columns(
                conn,
                "cb_price_quotes",
                {
                    "instrument_key": "TEXT NOT NULL DEFAULT ''",
                    "primary_id_scheme": "TEXT NOT NULL DEFAULT ''",
                    "primary_id": "TEXT NOT NULL DEFAULT ''",
                },
            )
            _ensure_columns(
                conn,
                "market_data_points",
                {
                    "instrument_key": "TEXT NOT NULL DEFAULT ''",
                    "primary_id_scheme": "TEXT NOT NULL DEFAULT ''",
                    "primary_id": "TEXT NOT NULL DEFAULT ''",
                },
            )


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: Mapping[str, str]) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _quote_filters(*, instrument_id: str = "", contract_id: str = "", instrument_key: str = "") -> tuple[str, list[str]]:
    clauses = []
    params = []
    if instrument_id:
        clauses.append("instrument_id = ?")
        params.append(instrument_id)
    if contract_id:
        clauses.append("contract_id = ?")
        params.append(contract_id)
    if instrument_key:
        clauses.append("instrument_key = ?")
        params.append(instrument_key)
    return (" WHERE " + " AND ".join(clauses), params) if clauses else ("", params)


def _market_data_filters(*, instrument_id: str = "", instrument_type: str = "") -> tuple[str, list[str]]:
    clauses = []
    params = []
    if instrument_id:
        clauses.append("instrument_id = ?")
        params.append(instrument_id)
    if instrument_type:
        clauses.append("instrument_type = ?")
        params.append(instrument_type)
    return (" WHERE " + " AND ".join(clauses), params) if clauses else ("", params)


def _points_by_date(rows: Iterable[sqlite3.Row]) -> dict[str, sqlite3.Row]:
    result: dict[str, sqlite3.Row] = {}
    for row in rows:
        result[row["as_of_date"]] = row
    return result


def _select_clean_daily_quotes(
    rows: Iterable[sqlite3.Row], equity_by_date: dict[str, sqlite3.Row]
) -> list[tuple[sqlite3.Row, str]]:
    by_date: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        if not _clean_quote_row(row):
            continue
        by_date.setdefault(row["as_of_date"], []).append(row)
    selected: list[tuple[sqlite3.Row, str]] = []
    for as_of_date in sorted(by_date):
        candidates = by_date[as_of_date]
        equity = equity_by_date.get(as_of_date)
        stock_close = float(equity["value"]) if equity is not None else None
        if stock_close is not None:
            with_stock = [row for row in candidates if _raw_stock_price(row) is not None]
            if with_stock:
                chosen = min(
                    with_stock,
                    key=lambda row: (abs(float(_raw_stock_price(row)) - stock_close), _negative_time_key(row), -int(row["id"])),
                )
                selected.append((chosen, f"closest_quote_stock_to_close:{stock_close:g};latest_tiebreak"))
                continue
        selected.append((max(candidates, key=lambda row: (_time_key(row), int(row["id"]))), "latest_clean_quote"))
    return selected


def _clean_quote_row(row: sqlite3.Row) -> bool:
    mid = row["mid_price"]
    if mid is None or not (1.0 <= float(mid) <= 1000.0):
        return False
    bid = row["bid_price"]
    ask = row["ask_price"]
    if bid is not None and ask is not None:
        bid_f = float(bid)
        ask_f = float(ask)
        if bid_f <= 0.0 or ask_f <= 0.0 or ask_f < bid_f:
            return False
        spread = ask_f - bid_f
        if spread > 10.0 or spread / float(mid) > 0.10:
            return False
    stock = _raw_stock_price(row)
    if stock is not None and stock <= 0.0:
        return False
    return True


def _raw_stock_price(row: sqlite3.Row) -> float | None:
    try:
        value = json.loads(row["raw_json"] or "{}").get("stock_price")
    except json.JSONDecodeError:
        return None
    return None if value in (None, "") else float(value)


def _time_key(row: sqlite3.Row) -> str:
    return row["as_of_time"] or "00:00"


def _negative_time_key(row: sqlite3.Row) -> tuple[int, int, int]:
    parts = [int(part) for part in (_time_key(row) + ":00").split(":")[:3]]
    while len(parts) < 3:
        parts.append(0)
    return (-parts[0], -parts[1], -parts[2])


def _batch_record(row: sqlite3.Row) -> PriceHistoryImportBatch:
    return PriceHistoryImportBatch(
        id=int(row["id"]),
        source_file=row["source_file"],
        source_sha256=row["source_sha256"],
        row_count=int(row["row_count"]),
        imported_at=row["imported_at"],
        notes=row["notes"],
    )


def _quote_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["raw"] = json.loads(item.pop("raw_json") or "{}")
    return item


def _quote_identity(row: PriceQuoteRow) -> InstrumentIdentityRef:
    aliases = tuple(value for value in (row.reference_security, row.security) if value)
    return identity_from_observation(
        instrument_type="convertible_bond",
        observed_id=row.instrument_id,
        display_name=row.reference_security or row.security,
        contract_id=row.contract_id,
        aliases=aliases,
    )


def _market_data_identity(row: MarketDataPoint) -> InstrumentIdentityRef:
    return identity_from_observation(
        instrument_type=row.instrument_type or "market_data",
        observed_id=row.instrument_id,
        display_name=row.instrument_id,
    )


def _raw_payload(row: PriceQuoteRow) -> dict[str, Any]:
    return {
        "reference_security": row.reference_security,
        "as_of_date": row.as_of_date.isoformat(),
        "as_of_time": row.as_of_time.isoformat(timespec="minutes") if row.as_of_time else "",
        "dealer": row.dealer,
        "source_type": row.source_type,
        "security": row.security,
        "bid_price": row.bid_price,
        "ask_price": row.ask_price,
        "mid_price": row.mid_price,
        "stock_price": row.stock_price,
        "price_currency": row.price_currency,
        "sender_name": row.sender_name,
        "subject": row.subject,
        "keyword": row.keyword,
        "instrument_id": row.instrument_id,
        "contract_id": row.contract_id,
        "source_file": row.source_file,
        "source_sheet": row.source_sheet,
        "source_row": row.source_row,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
