"""Prospectus directory inventory helpers.

A PDF in the raw prospectus folder is not a priced contract. It stays as a
prospectus-only record until extraction and review produce contract JSON.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from cb_terminal.prospectus.extraction import extract_text_from_pdf


def prospectus_id_from_filename(filename: str) -> str:
    stem = Path(filename).stem.lower()
    stem = stem.replace("&", "_")
    stem = re.sub(r"[^a-z0-9]+", "_", stem)
    stem = re.sub(r"_+", "_", stem).strip("_")
    return stem or "prospectus"


def issuer_hint_from_filename(filename: str) -> str:
    stem = Path(filename).stem
    for separator in (" - ", "_CB", " CB", "_cb", " cb"):
        if separator in stem:
            stem = stem.split(separator, 1)[0]
            break
    return " ".join(stem.replace("_", " ").split()).strip()


def document_type_hint_from_filename(filename: str) -> str:
    lower = filename.lower()
    if "final offering circular" in lower:
        return "final_offering_circular"
    if "termsheet" in lower or "term sheet" in lower:
        return "pricing_termsheet"
    return "unknown"


def build_prospectus_inventory(
    prospectus_dir: str | Path,
    *,
    contracts_dir: str | Path,
) -> list[dict[str, Any]]:
    prospectus_root = Path(prospectus_dir)
    contracts_root = Path(contracts_dir)
    items: list[dict[str, Any]] = []
    for pdf_path in sorted(prospectus_root.glob("*.pdf"), key=lambda path: path.name.lower()):
        prospectus_id = prospectus_id_from_filename(pdf_path.name)
        contract_path = _find_existing_contract(contracts_root, prospectus_id)
        extraction = extract_text_from_pdf(pdf_path, max_pages=3)
        item = {
            "id": prospectus_id,
            "source_path": str(pdf_path),
            "source_filename": pdf_path.name,
            "issuer_hint": issuer_hint_from_filename(pdf_path.name),
            "document_type_hint": document_type_hint_from_filename(pdf_path.name),
            "status": "contract_available_needs_review" if contract_path else "prospectus_only_needs_extraction",
            "contract_path": str(contract_path) if contract_path else None,
            "extraction": {
                "method": extraction.method,
                "page_count": extraction.page_count,
                "extracted_characters": len(extraction.text),
                "warnings": extraction.warnings,
            },
        }
        items.append(item)
    return items


def _find_existing_contract(contracts_dir: Path, prospectus_id: str) -> Path | None:
    exact = contracts_dir / f"{prospectus_id}.json"
    if exact.exists():
        return exact
    for candidate in sorted(contracts_dir.glob("*.json"), key=lambda path: path.name.lower()):
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        source_file = str(raw.get("source_file") or "")
        if source_file and prospectus_id_from_filename(Path(source_file).name) == prospectus_id:
            return candidate
    return None
