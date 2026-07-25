"""SQLite database for raw CB quote/price-history imports."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from cb_terminal.domain import dumps_json
from cb_terminal.domain.identity import InstrumentIdentityRef, identity_from_observation
from cb_terminal.io.daily_quote_selection import (
    DailyQuoteCandidate,
    is_clean_quote_values,
    select_daily_quote_candidate,
)
from cb_terminal.io.market_data_history import MarketDataPoint, load_market_data_file
from cb_terminal.io.outliers import robust_scale_outlier_keys
from cb_terminal.io.price_history import PriceQuoteRow, load_price_history_file
from cb_terminal.storage.sqlite_connection import managed_sqlite_connection

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
    batch-tracked for audit. Observations from distinct source paths accumulate,
    while reimporting the same source path atomically refreshes only that source
    so a revised workbook cannot mix with its older version.
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
        supplied_source = Path(path).expanduser()
        source = supplied_source.resolve()
        source_aliases = _source_file_aliases(supplied_source, source)
        digest = _sha256(source)
        rows = load_price_history_file(source, instrument_id=instrument_id, contract_id=contract_id)
        imported_at = _utc_now()
        with self._connect() as conn:
            self._delete_source_observations(
                conn,
                source_aliases,
                delete_quotes=True,
                delete_market_data=True,
            )
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
        supplied_source = Path(path).expanduser()
        source = supplied_source.resolve()
        source_aliases = _source_file_aliases(supplied_source, source)
        digest = _sha256(source)
        rows = load_market_data_file(source)
        imported_at = _utc_now()
        with self._connect() as conn:
            self._delete_source_observations(
                conn,
                source_aliases,
                delete_quotes=True,
                delete_market_data=True,
            )
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

        supplied_source = Path(path).expanduser()
        source = supplied_source.resolve()
        source_aliases = _source_file_aliases(supplied_source, source)
        digest = _sha256(source)
        quotes = list(quote_rows)
        points = list(market_data_points)
        imported_at = _utc_now()
        with self._connect() as conn:
            self._delete_source_observations(
                conn,
                source_aliases,
                delete_quotes=True,
                delete_market_data=True,
            )
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

    def remove_source_data(self, path: str | Path) -> dict[str, int]:
        """Remove imported observations when their source file is removed.

        Source files are mutable library objects in the GUI. Keeping their old
        observations after deletion would let unavailable data continue to win
        market-series selection.
        """

        supplied_source = Path(path).expanduser()
        resolved_source = supplied_source.resolve()
        aliases = _source_file_aliases(supplied_source, resolved_source)
        placeholders = ",".join("?" for _ in aliases)
        with self._connect() as conn:
            quote_count = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM cb_price_quotes WHERE source_file IN ({placeholders})",
                    aliases,
                ).fetchone()[0]
            )
            market_data_count = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM market_data_points WHERE source_file IN ({placeholders})",
                    aliases,
                ).fetchone()[0]
            )
            self._delete_source_observations(
                conn,
                aliases,
                delete_quotes=True,
                delete_market_data=True,
            )
            conn.execute(
                f"DELETE FROM price_history_import_batches WHERE source_file IN ({placeholders})",
                aliases,
            )
        return {
            "quote_count": quote_count,
            "market_data_count": market_data_count,
        }

    def rename_source_data(self, old_path: str | Path, new_path: str | Path) -> dict[str, int]:
        """Keep imported provenance aligned when a source file is renamed."""

        supplied_old = Path(old_path).expanduser()
        resolved_old = supplied_old.resolve()
        resolved_new = Path(new_path).expanduser().resolve()
        aliases = _source_file_aliases(supplied_old, resolved_old)
        placeholders = ",".join("?" for _ in aliases)
        with self._connect() as conn:
            quote_count = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM cb_price_quotes WHERE source_file IN ({placeholders})",
                    aliases,
                ).fetchone()[0]
            )
            market_data_count = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM market_data_points WHERE source_file IN ({placeholders})",
                    aliases,
                ).fetchone()[0]
            )
            params = (str(resolved_new), *aliases)
            conn.execute(
                f"UPDATE cb_price_quotes SET source_file = ? WHERE source_file IN ({placeholders})",
                params,
            )
            conn.execute(
                f"UPDATE market_data_points SET source_file = ? WHERE source_file IN ({placeholders})",
                params,
            )
            conn.execute(
                f"UPDATE price_history_import_batches SET source_file = ? WHERE source_file IN ({placeholders})",
                params,
            )
        return {
            "quote_count": quote_count,
            "market_data_count": market_data_count,
        }

    def get_import_batch(self, batch_id: int) -> PriceHistoryImportBatch | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM price_history_import_batches WHERE id = ?", (batch_id,)).fetchone()
        return _batch_record(row) if row else None

    def quote_count(self, *, instrument_id: str = "", contract_id: str = "", instrument_key: str = "") -> int:
        where, params = _quote_filters(instrument_id=instrument_id, contract_id=contract_id, instrument_key=instrument_key)
        with self._connect() as conn:
            rows = conn.execute(f"SELECT source_file FROM cb_price_quotes{where}", params).fetchall()
        return len(self._active_source_rows(rows))

    def quote_date_range(self, *, instrument_id: str = "", contract_id: str = "", instrument_key: str = "") -> dict[str, Any]:
        where, params = _quote_filters(instrument_id=instrument_id, contract_id=contract_id, instrument_key=instrument_key)
        with self._connect() as conn:
            rows = self._active_source_rows(
                conn.execute(f"SELECT as_of_date, source_file FROM cb_price_quotes{where}", params).fetchall()
            )
        dates = [str(row["as_of_date"]) for row in rows]
        return {"count": len(rows), "first_date": min(dates) if dates else "", "latest_date": max(dates) if dates else ""}

    def quote_source_files(self, *, instrument_id: str = "", contract_id: str = "", instrument_key: str = "") -> list[dict[str, Any]]:
        where, params = _quote_filters(instrument_id=instrument_id, contract_id=contract_id, instrument_key=instrument_key)
        with self._connect() as conn:
            rows = self._active_source_rows(
                conn.execute(
                    f"""
                    SELECT source_file, COUNT(*) AS row_count, MIN(as_of_date) AS first_date, MAX(as_of_date) AS latest_date
                    FROM cb_price_quotes{where}
                    GROUP BY source_file
                    ORDER BY latest_date DESC, source_file
                    """,
                    params,
                ).fetchall()
            )
        return [{"source_file": row["source_file"] or "", "row_count": int(row["row_count"] or 0), "first_date": row["first_date"] or "", "latest_date": row["latest_date"] or ""} for row in rows]

    def market_data_date_range(self, *, instrument_id: str = "", instrument_type: str = "") -> dict[str, Any]:
        where, params = _market_data_filters(instrument_id=instrument_id, instrument_type=instrument_type)
        with self._connect() as conn:
            rows = self._active_source_rows(
                conn.execute(f"SELECT as_of_date, source_file FROM market_data_points{where}", params).fetchall()
            )
        dates = [str(row["as_of_date"]) for row in rows]
        return {"count": len(rows), "first_date": min(dates) if dates else "", "latest_date": max(dates) if dates else ""}

    def market_data_source_files(self, *, instrument_id: str = "", instrument_type: str = "") -> list[dict[str, Any]]:
        where, params = _market_data_filters(instrument_id=instrument_id, instrument_type=instrument_type)
        with self._connect() as conn:
            rows = self._active_source_rows(
                conn.execute(
                    f"""
                    SELECT source_file, COUNT(*) AS row_count, MIN(as_of_date) AS first_date, MAX(as_of_date) AS latest_date
                    FROM market_data_points{where}
                    GROUP BY source_file
                    ORDER BY latest_date DESC, source_file
                    """,
                    params,
                ).fetchall()
            )
        return [{"source_file": row["source_file"] or "", "row_count": int(row["row_count"] or 0), "first_date": row["first_date"] or "", "latest_date": row["latest_date"] or ""} for row in rows]

    def latest_quotes(self, *, instrument_id: str = "", contract_id: str = "", instrument_key: str = "", limit: int = 20) -> list[dict[str, Any]]:
        where, params = _quote_filters(instrument_id=instrument_id, contract_id=contract_id, instrument_key=instrument_key)
        with self._connect() as conn:
            rows = self._active_source_rows(
                conn.execute(
                    f"""
                    SELECT * FROM cb_price_quotes{where}
                    ORDER BY as_of_date DESC, as_of_time DESC, id DESC
                    """,
                    params,
                ).fetchall()
            )
        return [_quote_dict(row) for row in rows[: int(limit)]]

    def selected_daily_quotes(
        self,
        *,
        instrument_id: str = "",
        contract_id: str = "",
        instrument_key: str = "",
        equity_instrument_id: str = "",
        fx_instrument_id: str = "",
        fx_convention: str = "",
    ) -> list[dict[str, Any]]:
        """Return robust daily observations while retaining raw rows separately."""

        where, params = _quote_filters(
            instrument_id=instrument_id,
            contract_id=contract_id,
            instrument_key=instrument_key,
        )
        with self._connect() as conn:
            quote_rows = self._active_source_rows(
                conn.execute(
                    f"""
                    SELECT * FROM cb_price_quotes{where}
                    ORDER BY as_of_date, COALESCE(NULLIF(as_of_time, ''), '00:00'), id
                    """,
                    params,
                ).fetchall()
            )
            equity_by_date = (
                _points_by_date(
                    self._active_source_rows(
                        conn.execute(
                            """
                            SELECT p.*, b.source_sha256
                            FROM market_data_points p
                            JOIN price_history_import_batches b ON b.id = p.import_batch_id
                            WHERE p.instrument_id = ? AND p.field IN ('PX_LAST', 'Last Price')
                            ORDER BY p.as_of_date, p.source_file, p.source_sheet, p.source_column, p.source_row
                            """,
                            (equity_instrument_id,),
                        ).fetchall()
                    )
                )
                if equity_instrument_id
                else {}
            )
            fx_by_date = (
                _points_by_date(
                    self._active_source_rows(
                        conn.execute(
                            """
                            SELECT p.*, b.source_sha256
                            FROM market_data_points p
                            JOIN price_history_import_batches b ON b.id = p.import_batch_id
                            WHERE p.instrument_id = ? AND p.field IN ('PX_LAST', 'Last Price')
                            ORDER BY p.as_of_date, p.source_file, p.source_sheet, p.source_column, p.source_row
                            """,
                            (fx_instrument_id,),
                        ).fetchall()
                    )
                )
                if fx_instrument_id
                else {}
            )
        selected = _select_clean_daily_quotes(
            quote_rows,
            equity_by_date,
            fx_by_date=fx_by_date,
            fx_convention=fx_convention,
        )
        result: list[dict[str, Any]] = []
        for row, reason in reversed(selected):
            item = _quote_dict(row)
            item["selection_reason"] = reason
            result.append(item)
        return result

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
            rows = conn.execute(f"SELECT source_file FROM market_data_points{where}", params).fetchall()
        return len(self._active_source_rows(rows))

    def market_data_points(self, *, instrument_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = self._active_source_rows(
                conn.execute(
                    """
                    SELECT * FROM market_data_points
                    WHERE instrument_id = ?
                    ORDER BY as_of_date DESC, id DESC
                    """,
                    (instrument_id,),
                ).fetchall()
            )
        return [dict(row) for row in rows[: int(limit)]]

    def build_valuation_market_rows(
        self,
        *,
        cb_instrument_id: str,
        equity_instrument_id: str,
        cb_contract_id: str = "",
        fx_instrument_id: str = "",
        stock_currency: str = "",
        bond_price_currency: str = "",
        fx_convention: str = "",
        quote_policy: str = "latest",
    ) -> list[dict[str, Any]]:
        if quote_policy != "latest":
            raise ValueError("only quote_policy='latest' is currently supported")
        with self._connect() as conn:
            all_quote_rows = self._active_source_rows(
                conn.execute(
                    """
                    SELECT q.* FROM cb_price_quotes q
                    WHERE q.instrument_key = ? OR q.instrument_id = ?
                    ORDER BY q.as_of_date, COALESCE(NULLIF(q.as_of_time, ''), '00:00'), q.id
                    """,
                    (cb_instrument_id, cb_instrument_id),
                ).fetchall()
            )
            equity_by_date, equity_outliers_by_date = _points_by_date_with_diagnostics(
                self._active_source_rows(
                    conn.execute(
                        """
                        SELECT p.*, b.source_sha256
                        FROM market_data_points p
                        JOIN price_history_import_batches b ON b.id = p.import_batch_id
                        WHERE p.instrument_id = ? AND p.field IN ('PX_LAST', 'Last Price')
                        ORDER BY p.as_of_date, p.source_file, p.source_sheet, p.source_column, p.source_row
                        """,
                        (equity_instrument_id,),
                    ).fetchall()
                )
            )
            fx_by_date, fx_outliers_by_date = _points_by_date_with_diagnostics(
                self._active_source_rows(
                    conn.execute(
                        """
                        SELECT p.*, b.source_sha256
                        FROM market_data_points p
                        JOIN price_history_import_batches b ON b.id = p.import_batch_id
                        WHERE p.instrument_id = ? AND p.field IN ('PX_LAST', 'Last Price')
                        ORDER BY p.as_of_date, p.source_file, p.source_sheet, p.source_column, p.source_row
                        """,
                        (fx_instrument_id,),
                    ).fetchall()
                )
            ) if fx_instrument_id else ({}, {})
        quote_rows = _select_clean_daily_quotes(
            all_quote_rows,
            equity_by_date,
            fx_by_date=fx_by_date,
            fx_convention=fx_convention,
        )
        rows: list[dict[str, Any]] = []
        for quote, selection_reason in quote_rows:
            as_of_date = quote["as_of_date"]
            equity = equity_by_date.get(as_of_date)
            if equity is None:
                continue
            fx = fx_by_date.get(as_of_date) if fx_instrument_id else None
            if fx_instrument_id and fx is None:
                # Never turn a cross-currency row into a same-currency row.
                # The exact-date intersection is the valuation-ready history.
                continue
            if equity_outliers_by_date.get(as_of_date):
                selection_reason += (
                    f";equity_price_outliers_excluded:{equity_outliers_by_date[as_of_date]}"
                )
            if fx_outliers_by_date.get(as_of_date):
                selection_reason += f";fx_rate_outliers_excluded:{fx_outliers_by_date[as_of_date]}"
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
                    # Raw quotes can predate the correct termsheet (or be
                    # uploaded while another CB is selected). The exact ISIN
                    # selects the quote; the requested target contract is the
                    # authoritative contract for the generated valuation row.
                    "cb_contract_id": cb_contract_id or quote["contract_id"],
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

    def _active_source_rows(self, rows: Iterable[sqlite3.Row]) -> list[sqlite3.Row]:
        """Ignore observations whose raw source is no longer available."""

        resolved_db = self.path.expanduser().resolve()
        project_root = resolved_db.parents[2] if len(resolved_db.parents) > 2 else resolved_db.parent
        active: list[sqlite3.Row] = []
        availability: dict[str, bool] = {}
        for row in rows:
            source_value = str(row["source_file"] or "")
            if source_value not in availability:
                source = Path(source_value).expanduser()
                candidates = [source] if source.is_absolute() else [Path.cwd() / source, project_root / source]
                availability[source_value] = any(candidate.exists() for candidate in candidates)
            if availability[source_value]:
                active.append(row)
        return active

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

    @staticmethod
    def _delete_source_observations(
        conn: sqlite3.Connection,
        source_aliases: tuple[str, ...],
        *,
        delete_quotes: bool,
        delete_market_data: bool,
    ) -> None:
        placeholders = ",".join("?" for _ in source_aliases)
        if delete_quotes:
            conn.execute(
                f"DELETE FROM cb_price_quotes WHERE source_file IN ({placeholders})",
                source_aliases,
            )
        if delete_market_data:
            conn.execute(
                f"DELETE FROM market_data_points WHERE source_file IN ({placeholders})",
                source_aliases,
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

    def _connect(self) -> AbstractContextManager[sqlite3.Connection]:
        return managed_sqlite_connection(self.path)

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


def _source_file_aliases(supplied: Path, resolved: Path) -> tuple[str, ...]:
    """Return stable spellings used by current and older imports of one file."""

    aliases = [str(supplied), str(resolved)]
    return tuple(dict.fromkeys(alias for alias in aliases if alias))


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
    selected, _ = _points_by_date_with_diagnostics(rows)
    return selected


def _points_by_date_with_diagnostics(
    rows: Iterable[sqlite3.Row],
) -> tuple[dict[str, sqlite3.Row], dict[str, int]]:
    """Select one observed market-data point per date without upload-order bias.

    A source column is treated as a time series.  The broadest series is the
    primary source, with the most recent endpoint used as the next preference.
    Stable file provenance breaks any remaining tie.  This keeps a generated
    history on one coherent observed series where possible and prevents SQLite
    insertion ids (and therefore upload order) from changing equity or FX
    closes.  Shorter sources remain available as fallbacks on dates the primary
    series does not cover.
    """

    row_list = list(rows)
    dates_by_series: dict[tuple[str, str, str, str, str], set[str]] = {}
    latest_date_by_series: dict[tuple[str, str, str, str, str], str] = {}
    for row in row_list:
        series_key = _market_data_series_key(row)
        as_of_date = str(row["as_of_date"])
        dates_by_series.setdefault(series_key, set()).add(as_of_date)
        latest_date_by_series[series_key] = max(latest_date_by_series.get(series_key, ""), as_of_date)

    by_date: dict[str, list[sqlite3.Row]] = {}
    for row in row_list:
        by_date.setdefault(str(row["as_of_date"]), []).append(row)

    preliminary_by_date: dict[str, tuple[list[sqlite3.Row], list[sqlite3.Row]]] = {}
    outlier_series: set[tuple[str, str, str, str, str]] = set()
    inlier_series: set[tuple[str, str, str, str, str]] = set()
    for as_of_date, candidates in by_date.items():
        retained, excluded = _partition_market_data_point_outliers(candidates)
        preliminary_by_date[as_of_date] = (retained, excluded)
        scale_outliers = [
            row for row in excluded if _positive_finite_market_value(row["value"])
        ]
        if scale_outliers:
            outlier_series.update(_market_data_series_key(row) for row in scale_outliers)
            inlier_series.update(_market_data_series_key(row) for row in retained)

    # A source column that is a scale outlier wherever it overlaps a robust
    # consensus remains suspect on its non-overlap dates too.  If that same
    # series is an inlier on another decisive date, only its directly bad
    # observations are removed rather than blacklisting the whole series.
    suspect_series = outlier_series - inlier_series
    result: dict[str, sqlite3.Row] = {}
    outliers_by_date: dict[str, int] = {}
    for as_of_date, original_candidates in by_date.items():
        preliminary, _ = preliminary_by_date[as_of_date]
        candidates = [
            row
            for row in preliminary
            if _market_data_series_key(row) not in suspect_series
        ]
        if not candidates:
            continue
        result[as_of_date] = min(
            candidates,
            key=lambda row: _market_data_point_rank(row, dates_by_series, latest_date_by_series),
        )
        outlier_count = len(original_candidates) - len(candidates)
        if outlier_count:
            outliers_by_date[as_of_date] = outlier_count
    return result, outliers_by_date


def _partition_market_data_point_outliers(
    candidates: Iterable[sqlite3.Row],
) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    candidate_list = list(candidates)
    valid = [row for row in candidate_list if _positive_finite_market_value(row["value"])]
    if not valid:
        return [], candidate_list

    values_by_observation: dict[tuple[Any, ...], list[float]] = {}
    for row in valid:
        values_by_observation.setdefault(
            _market_data_observation_group_key(row),
            [],
        ).append(float(row["value"]))
    outlier_keys = robust_scale_outlier_keys(
        values_by_observation,
        minimum_groups=3,
        minimum_factor=2.0,
    )
    retained = [
        row
        for row in valid
        if _market_data_observation_group_key(row) not in outlier_keys
    ]
    retained_ids = {id(row) for row in retained}
    excluded = [row for row in candidate_list if id(row) not in retained_ids]
    return retained, excluded


def _positive_finite_market_value(value: object) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0.0


def _market_data_series_key(row: sqlite3.Row) -> tuple[str, str, str, str, str]:
    return (
        str(row["source_sha256"] or ""),
        str(row["source_sheet"] or ""),
        str(row["source_column"] or ""),
        str(row["instrument_id"] or ""),
        str(row["field"] or ""),
    )


def _market_data_observation_group_key(row: sqlite3.Row) -> tuple[Any, ...]:
    # The content hash and source row collapse repeat uploads of the same
    # observation so duplicate chat exports cannot manufacture a majority.
    return (*_market_data_series_key(row), int(row["source_row"] or 0))


def _market_data_point_rank(
    row: sqlite3.Row,
    dates_by_series: Mapping[tuple[str, str, str, str, str], set[str]],
    latest_date_by_series: Mapping[tuple[str, str, str, str, str], str],
) -> tuple[Any, ...]:
    series_key = _market_data_series_key(row)
    latest_date_desc = -int(latest_date_by_series[series_key].replace("-", ""))
    return (
        -len(dates_by_series[series_key]),
        latest_date_desc,
        0 if str(row["field"] or "").upper() == "PX_LAST" else 1,
        str(row["source_sha256"] or ""),
        str(row["source_file"] or "").casefold(),
        str(row["source_sheet"] or "").casefold(),
        str(row["source_column"] or "").casefold(),
        int(row["source_row"] or 0),
    )


def _select_clean_daily_quotes(
    rows: Iterable[sqlite3.Row],
    equity_by_date: dict[str, sqlite3.Row],
    *,
    fx_by_date: dict[str, sqlite3.Row] | None = None,
    fx_convention: str = "",
) -> list[tuple[sqlite3.Row, str]]:
    fx_by_date = fx_by_date or {}
    by_date: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        if not _clean_quote_row(row):
            continue
        by_date.setdefault(row["as_of_date"], []).append(row)
    selected: list[tuple[sqlite3.Row, str]] = []
    for as_of_date in sorted(by_date):
        equity = equity_by_date.get(as_of_date)
        stock_close = float(equity["value"]) if equity is not None else None
        fx = fx_by_date.get(as_of_date)
        stock_fx_rate = float(fx["value"]) if fx is not None else None
        candidates = [
            DailyQuoteCandidate(
                payload=row,
                mid_price=float(row["mid_price"]),
                bid_price=None if row["bid_price"] is None else float(row["bid_price"]),
                ask_price=None if row["ask_price"] is None else float(row["ask_price"]),
                stock_price=_raw_stock_price(row),
                as_of_time=_parse_quote_time(row["as_of_time"]),
                stable_key=(
                    row["source_file"],
                    row["source_sheet"],
                    int(row["source_row"]),
                    row["dealer"],
                    row["reference_security"],
                ),
            )
            for row in by_date[as_of_date]
        ]
        chosen, reason = select_daily_quote_candidate(
            candidates,
            stock_close,
            stock_fx_rate=stock_fx_rate,
            fx_convention=fx_convention,
        )
        selected.append((chosen.payload, reason))
    return selected


def _clean_quote_row(row: sqlite3.Row) -> bool:
    return is_clean_quote_values(
        mid_price=row["mid_price"],
        bid_price=row["bid_price"],
        ask_price=row["ask_price"],
        stock_price=_raw_stock_price(row),
        min_price=1.0,
        max_price=1000.0,
        max_bid_ask_spread=10.0,
        max_bid_ask_spread_pct=0.10,
        require_positive_stock_if_present=True,
    )


def _raw_stock_price(row: sqlite3.Row) -> float | None:
    try:
        value = json.loads(row["raw_json"] or "{}").get("stock_price")
        return None if value in (None, "") else float(value)
    except (AttributeError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _parse_quote_time(value: object) -> time | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return time.fromisoformat(text)
    except ValueError:
        return None


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
