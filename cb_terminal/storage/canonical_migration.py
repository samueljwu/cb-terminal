"""Bootstrap existing cb-terminal JSON/CSV artifacts into CanonicalStore."""

from __future__ import annotations

import csv
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from cb_terminal.storage.canonical_store import CanonicalStore


def bootstrap_project_catalog(project_root: str | Path, db_path: str | Path) -> dict[str, int]:
    root = Path(project_root)
    store = CanonicalStore(db_path)
    counts = {
        "instruments": 0,
        "contracts": 0,
        "coverage_entries": 0,
        "source_files": 0,
        "valuation_market_series": 0,
        "valuation_market_rows": 0,
    }
    instruments_path = root / "data/coverage/instruments.json"
    registry_instruments: list[dict[str, Any]] = []
    instrument_key_by_source_id: dict[str, str] = {}
    pending_cb_registry = _pending_cb_registry_by_alias(registry_instruments)
    if instruments_path.exists():
        registry = json.loads(instruments_path.read_text(encoding="utf-8"))
        registry_instruments = [raw for raw in (registry.get("instruments", []) if isinstance(registry, dict) else []) if isinstance(raw, dict)]
        instrument_key_by_source_id = _instrument_key_by_source_id(registry_instruments)
        pending_cb_registry = _pending_cb_registry_by_alias(registry_instruments)
        for raw_instrument in registry_instruments:
            if not isinstance(raw_instrument, dict):
                continue
            instrument_key = str(raw_instrument.get("registry_id") or "").strip()
            primary_id_scheme = str(raw_instrument.get("canonical_id_type") or raw_instrument.get("primary_id_scheme") or "").strip() or "UNKNOWN"
            primary_id = str(raw_instrument.get("canonical_id") or raw_instrument.get("primary_id") or instrument_key).strip()
            if not instrument_key or not primary_id:
                continue
            aliases = list(raw_instrument.get("aliases") or []) + list(raw_instrument.get("bloomberg_ids") or []) + list(raw_instrument.get("deal_names") or [])
            store.upsert_instrument(
                instrument_key=instrument_key,
                instrument_type=str(raw_instrument.get("instrument_type") or "unknown"),
                primary_id_scheme=primary_id_scheme,
                primary_id=primary_id,
                display_id=str(raw_instrument.get("display_id") or raw_instrument.get("display_name") or ""),
                display_name=str(raw_instrument.get("display_name") or raw_instrument.get("display_id") or ""),
                aliases=aliases,
                metadata=raw_instrument,
            )
            counts["instruments"] += 1

    contracts_by_path: dict[str, dict[str, Any]] = {}
    for contract_path in sorted((root / "data/contracts").glob("*.json")):
        raw = _apply_pending_cb_registry_identity(json.loads(contract_path.read_text(encoding="utf-8")), pending_cb_registry)
        record = store.upsert_contract(raw, source_path=_rel(root, contract_path))
        contracts_by_path[_rel(root, contract_path)] = record
        counts["contracts"] += 1
        source = store.register_source_file(contract_path, artifact_kind="contract_json", canonical_path=_rel(root, contract_path))
        counts["source_files"] += 1 if source else 0

    universe_path = root / "data/coverage/universe.json"
    if universe_path.exists():
        rows = json.loads(universe_path.read_text(encoding="utf-8"))
        for item in rows:
            contract_path = str(item.get("contract_path") or "")
            contract = contracts_by_path.get(contract_path)
            if not contract and contract_path:
                abs_contract = root / contract_path
                if abs_contract.exists():
                    contract = store.upsert_contract(_apply_pending_cb_registry_identity(json.loads(abs_contract.read_text(encoding="utf-8")), pending_cb_registry), source_path=contract_path)
            if not contract:
                continue
            store.upsert_coverage_member(
                universe_name="default",
                contract_id=contract["contract_id"],
                instrument_key=contract["instrument_key"],
                status=str(item.get("status") or "active"),
                market_history_path=str(item.get("market_history_path") or ""),
                raw_price_history_path=str(item.get("raw_price_history_path") or ""),
                metadata=item,
            )
            counts["coverage_entries"] += 1
            market_path = str(item.get("market_history_path") or "")
            if market_path:
                abs_market = root / market_path
                if abs_market.exists():
                    source = store.register_source_file(abs_market, artifact_kind="generated_market_history", canonical_path=market_path)
                    batch = store.record_import_batch(
                        source_file_id=source["id"],
                        processor="valuation_market_history_bootstrap",
                        processor_version="v1",
                        policy_hash=contract["contract_id"],
                        row_count_in=_csv_data_row_count(abs_market),
                        row_count_out=_csv_data_row_count(abs_market),
                    )
                    rows_to_save = _valuation_rows_from_csv(abs_market, expected_contract_id=contract["contract_id"], expected_cb_primary_id=contract.get("primary_id", ""))
                    first_market_row = rows_to_save[0] if rows_to_save else {}
                    first_raw_row = first_market_row.get("raw", {}) if isinstance(first_market_row.get("raw"), dict) else {}
                    series = store.create_valuation_market_series(
                        contract_id=contract["contract_id"],
                        cb_instrument_key=contract["instrument_key"],
                        equity_instrument_key=_canonical_instrument_key_for_source_id(first_raw_row.get("equity_instrument_id") or item.get("underlying_ticker") or "", instrument_key_by_source_id),
                        fx_instrument_key=_canonical_instrument_key_for_source_id(first_raw_row.get("fx_instrument_id") or "", instrument_key_by_source_id),
                        stock_currency=str(first_market_row.get("stock_currency") or ""),
                        bond_price_currency=str(first_market_row.get("bond_price_currency") or ""),
                        fx_convention=str(first_market_row.get("fx_convention") or ""),
                        selection_policy="bootstrap_csv_v1",
                        source_file_id=source["id"],
                        import_batch_id=batch["id"],
                        metadata={"market_history_path": market_path},
                    )
                    counts["valuation_market_series"] += 1
                    store.save_valuation_market_rows(series["id"], rows_to_save)
                    counts["valuation_market_rows"] += len(rows_to_save)
    return counts


def _instrument_key_by_source_id(registry_instruments: list[dict[str, Any]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for raw in registry_instruments:
        key = str(raw.get("registry_id") or "").strip()
        if not key:
            continue
        ids = [raw.get("canonical_id"), raw.get("underlying_instrument_id"), raw.get("fx_instrument_id")]
        ids.extend(raw.get("bloomberg_ids") or [])
        ids.extend(raw.get("aliases") or [])
        for value in ids:
            text = str(value or "").strip()
            if text:
                mapping[text.upper()] = key
    return mapping


def _canonical_instrument_key_for_source_id(value: Any, mapping: dict[str, str]) -> str:
    text = str(value or "").strip()
    return mapping.get(text.upper(), text)


def _pending_cb_registry_by_alias(registry_instruments: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    aliases: dict[str, dict[str, Any]] = {}
    for raw in registry_instruments:
        if str(raw.get("instrument_type") or "") != "convertible_bond":
            continue
        if str(raw.get("canonical_id_type") or "").upper() != "PENDING_ISIN":
            continue
        values = [raw.get("registry_id"), raw.get("contract_id"), raw.get("display_name"), raw.get("issuer_legal_name"), raw.get("issuer_short_name")]
        values.extend(raw.get("aliases") or [])
        values.extend(raw.get("deal_names") or [])
        for value in values:
            key = _registry_match_key(value)
            if key:
                aliases[key] = raw
    return aliases


def _apply_pending_cb_registry_identity(raw: Any, pending_registry: dict[str, dict[str, Any]]) -> Any:
    if not isinstance(raw, dict):
        return raw
    instrument = raw.get("instrument") if isinstance(raw.get("instrument"), dict) else {}
    primary_id = str(raw.get("isin") or instrument.get("canonical_id") or "").strip()
    primary_id_type = str(instrument.get("canonical_id_type") or "").strip().upper()
    if primary_id and primary_id != "PENDING_ISIN" and primary_id_type != "PENDING_ISIN":
        return raw
    candidates = [raw.get("id"), instrument.get("display_name"), instrument.get("issuer_legal_name"), instrument.get("issuer_short_name")]
    candidates.extend(instrument.get("aliases") or [])
    candidates.extend(instrument.get("deal_names") or [])
    registry = next((pending_registry[key] for value in candidates if (key := _registry_match_key(value)) in pending_registry), None)
    if not registry:
        return raw
    result = deepcopy(raw)
    result["id"] = str(registry.get("contract_id") or result.get("id") or "")
    result_instrument = result.setdefault("instrument", {})
    result_instrument["registry_id"] = str(registry.get("registry_id") or "")
    result_instrument["canonical_id_type"] = str(registry.get("canonical_id_type") or "PENDING_ISIN")
    result_instrument["canonical_id"] = str(registry.get("canonical_id") or "")
    result_instrument.setdefault("display_name", str(registry.get("display_name") or ""))
    result_instrument.setdefault("issuer_legal_name", str(registry.get("issuer_legal_name") or ""))
    result_instrument.setdefault("issuer_short_name", str(registry.get("issuer_short_name") or ""))
    merged_aliases = list(dict.fromkeys([*(result_instrument.get("aliases") or []), *(registry.get("aliases") or []), *(registry.get("deal_names") or [])]))
    result_instrument["aliases"] = merged_aliases
    return result


def _registry_match_key(value: Any) -> str:
    return " ".join(str(value or "").strip().upper().split())


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _csv_data_row_count(path: Path) -> int:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return sum(1 for _ in csv.DictReader(handle))


def _valuation_rows_from_csv(path: Path, *, expected_contract_id: str = "", expected_cb_primary_id: str = "") -> list[dict[str, Any]]:
    rows = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            date = raw.get("date") or raw.get("as_of_date") or raw.get("valuation_date")
            if not date:
                continue
            raw_contract_id = str(raw.get("cb_contract_id") or "").strip()
            if expected_contract_id and raw_contract_id and raw_contract_id != expected_contract_id:
                raise ValueError(f"valuation CSV {path} belongs to {raw_contract_id}, not {expected_contract_id}")
            raw_cb_id = str(raw.get("cb_instrument_id") or "").strip().upper()
            expected_cb_id = str(expected_cb_primary_id or "").strip().upper()
            if expected_cb_id and raw_cb_id and raw_cb_id != expected_cb_id:
                raise ValueError(f"valuation CSV {path} belongs to CB instrument {raw_cb_id}, not {expected_cb_id}")
            rows.append(
                {
                    "as_of_date": date,
                    "stock_price": _float_or_none(raw.get("stock_price")),
                    "bond_price": _float_or_none(raw.get("bond_price") or raw.get("market_price")),
                    "market_fx_rate": _float_or_none(raw.get("market_fx_rate")),
                    "stock_currency": raw.get("stock_currency") or "",
                    "bond_price_currency": raw.get("bond_price_currency") or raw.get("price_currency") or "",
                    "fx_convention": raw.get("fx_convention") or "",
                    "cb_quote_time": raw.get("cb_quote_time") or raw.get("time") or "",
                    "cb_quote_dealer": raw.get("cb_quote_dealer") or raw.get("dealer") or "",
                    "cb_reference_security": raw.get("cb_reference_security") or raw.get("reference_security") or "",
                    "cb_selection_reason": raw.get("cb_selection_reason") or "bootstrap_csv",
                    "raw": raw,
                }
            )
    return rows


def _float_or_none(value: object) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    return float(str(value).replace(",", ""))
