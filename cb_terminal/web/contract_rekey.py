"""Keep a finalized CB's machine identity and its stored references together."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from cb_terminal.domain.identity import cb_identity_from_contract, contract_id_from_isin


def target_contract_id(raw: dict[str, Any]) -> str | None:
    instrument = raw.get("instrument") or {}
    if str(instrument.get("canonical_id_type") or "").upper() != "ISIN":
        return None
    return contract_id_from_isin(str(instrument.get("canonical_id") or raw.get("isin") or ""))


def rekey_contract(
    root: Path, path: Path, raw: dict[str, Any], *, database: Path | None = None,
) -> Path:
    """Move an edited contract and update references after its ISIN changes.

    The caller has already validated ``raw`` and saved a backup of the old JSON.
    Existing destination files and database identities are never overwritten.
    """
    new_id = target_contract_id(raw)
    if not new_id:
        return path
    old_id = path.stem
    new_path = path.with_name(f"{new_id}.json")
    if new_path != path and new_path.exists():
        raise ValueError(f"contract already exists for ISIN: {new_id}")
    if path.stem == new_id and old_id == new_id:
        return path

    identity_raw = {**raw, "id": new_id}
    if identity_raw.get("isin"):
        identity_raw["isin"] = str((raw.get("instrument") or {}).get("canonical_id") or "").strip().upper()
    identity = cb_identity_from_contract(identity_raw)
    old_rel = path.relative_to(root).as_posix()
    new_rel = new_path.relative_to(root).as_posix()
    review = root / "data" / "reviews" / f"{path.stem}_review.md"
    new_review = review.with_name(f"{new_id}_review.md")
    if review != new_review and new_review.exists():
        raise ValueError(f"review report already exists for ISIN: {new_id}")

    indexes: list[tuple[Path, Any]] = []
    for name in ("review_queue.json", "universe.json", "instruments.json", "prospectus_inventory.json"):
        index = root / "data" / "coverage" / name
        if not index.exists():
            continue
        original = json.loads(index.read_text(encoding="utf-8"))
        # Replace identity values and paths, preserving their existing slash style.
        def rewrite(value: Any) -> Any:
            if isinstance(value, dict):
                result = {key: rewrite(item) for key, item in value.items()}
                linked = any(old_rel.replace("/", sep) in str(value.get(key) or "") for key in ("contract_path", "path") for sep in ("/", "\\"))
                if linked and result.get("instrument_key") == f"isin:{old_id.removesuffix('_contract').lower()}":
                    result["instrument_key"] = f"isin:{identity.primary_id.lower()}"
                return result
            if isinstance(value, list):
                return [rewrite(item) for item in value]
            if isinstance(value, str):
                if value == old_id:
                    return new_id
                return value.replace(old_rel, new_rel).replace(old_rel.replace("/", "\\"), new_rel.replace("/", "\\")).replace(review.name, new_review.name)
            return value
        updated = rewrite(original)
        if updated != original:
            indexes.append((index, updated))

    if database and database.exists() and old_id != new_id:
        with closing(sqlite3.connect(database)) as conn:
            exists = conn.execute("SELECT 1 FROM contracts WHERE contract_id=?", (new_id,)).fetchone()
            if exists:
                raise ValueError(f"database contract already exists for ISIN: {new_id}")
            instrument = conn.execute("SELECT instrument_id FROM contracts WHERE contract_id=?", (old_id,)).fetchone()
            if instrument:
                collision = conn.execute("SELECT 1 FROM instruments WHERE instrument_key=? AND id<>?", (identity.instrument_key, instrument[0])).fetchone()
                if collision:
                    raise ValueError(f"database instrument already exists for ISIN: {identity.primary_id}")

    raw["id"] = new_id
    if raw.get("isin"):
        raw["isin"] = identity.primary_id
    if new_path != path:
        path.rename(new_path)
    if review.exists() and review != new_review:
        review.rename(new_review)
        new_review.write_text(new_review.read_text(encoding="utf-8").replace(old_id, new_id), encoding="utf-8")
    for index, updated in indexes:
        index.write_text(json.dumps(updated, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if database and database.exists() and old_id != new_id:
        conn = sqlite3.connect(database)
        try:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT instrument_id FROM contracts WHERE contract_id=?", (old_id,)).fetchone()
            if row:
                inst_id = row[0]
                old_metadata = conn.execute("SELECT metadata_json FROM instruments WHERE id=?", (inst_id,)).fetchone()
                metadata = json.loads(old_metadata[0] or "{}") if old_metadata else {}
                metadata["contract_id"] = new_id
                conn.execute("UPDATE instruments SET instrument_key=?, primary_id_scheme=?, primary_id=?, display_id=?, display_name=?, metadata_json=? WHERE id=?",
                             (identity.instrument_key, identity.primary_id_scheme, identity.primary_id, identity.display_id, identity.display_name, json.dumps(metadata), inst_id))
                conn.execute("UPDATE contracts SET contract_id=?, source_path=?, raw_json=? WHERE contract_id=?",
                             (new_id, new_rel, json.dumps(raw), old_id))
                for table in ("contract_versions", "coverage_universe", "valuation_market_series", "assumption_sets", "valuation_runs"):
                    conn.execute(f"UPDATE {table} SET contract_id=? WHERE contract_id=?", (new_id, old_id))
                conn.execute("UPDATE coverage_universe SET instrument_key=? WHERE contract_id=?", (identity.instrument_key, new_id))
                for version_id, version_raw, source_path in conn.execute(
                    "SELECT id, raw_json, source_path FROM contract_versions WHERE contract_id=?", (new_id,)
                ).fetchall():
                    snapshot = json.loads(version_raw)
                    snapshot["id"] = new_id
                    version_instrument = snapshot.get("instrument")
                    if isinstance(version_instrument, dict):
                        version_instrument["canonical_id_type"] = "ISIN"
                        version_instrument["canonical_id"] = identity.primary_id
                    if snapshot.get("isin"):
                        snapshot["isin"] = identity.primary_id
                    new_source = source_path.replace(old_rel, new_rel).replace(
                        old_rel.replace("/", "\\"), new_rel.replace("/", "\\")
                    )
                    conn.execute("UPDATE contract_versions SET source_path=?, raw_json=? WHERE id=?",
                                 (new_source, json.dumps(snapshot), version_id))
                conn.execute("UPDATE valuation_market_series SET cb_instrument_key=? WHERE contract_id=?", (identity.instrument_key, new_id))
                for source_id, original_path, canonical_path in conn.execute(
                    "SELECT id, original_path, canonical_path FROM source_files WHERE artifact_kind IN ('contract', 'contract_json') AND status='active'"
                ).fetchall():
                    if old_rel in original_path.replace("\\", "/") or old_rel in canonical_path.replace("\\", "/"):
                        conn.execute("UPDATE source_files SET status='superseded' WHERE id=?", (source_id,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return new_path
