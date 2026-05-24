"""Automated prospectus ingestion, review queue, and duplicate controls."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from cb_terminal.domain import dumps_json
from cb_terminal.prospectus.draft_contract import draft_contract_from_text, draft_contracts_from_text
from cb_terminal.prospectus.evidence import attach_source_evidence, evidence_summary
from cb_terminal.prospectus.extraction import ExtractionResult, PageText, extract_text_from_pdf, inspect_extraction_environment
from cb_terminal.prospectus.inventory import (
    document_type_hint_from_filename,
    issuer_hint_from_filename,
    prospectus_id_from_filename,
)
from cb_terminal.prospectus.review import build_review_report, validate_contract_dict


@dataclass(frozen=True)
class AutoIngestReport:
    scanned: int = 0
    created_contracts: int = 0
    duplicates: int = 0
    needs_extraction: int = 0
    failed: int = 0
    queue_path: Path | None = None
    items: list[dict[str, Any]] = field(default_factory=list)
    extraction_environment: dict[str, Any] = field(default_factory=dict)


def auto_ingest_prospectuses(
    *,
    prospectus_dir: str | Path,
    contracts_dir: str | Path,
    reviews_dir: str | Path,
    coverage_dir: str | Path,
    fixture_dir: str | Path | None = None,
    delete_duplicate_raw: bool = False,
    source_paths: list[str | Path] | None = None,
) -> AutoIngestReport:
    prospectus_root = Path(prospectus_dir)
    contracts_root = Path(contracts_dir)
    reviews_root = Path(reviews_dir)
    coverage_root = Path(coverage_dir)
    fixture_root = Path(fixture_dir) if fixture_dir is not None else None
    for directory in (contracts_root, reviews_root, coverage_root):
        directory.mkdir(parents=True, exist_ok=True)

    existing_by_key = _load_existing_contract_keys(contracts_root)
    existing_by_source = _load_existing_contract_sources(contracts_root)
    queue_items: list[dict[str, Any]] = []
    created = duplicates = needs_extraction = failed = 0
    selected_paths = _selected_pdf_paths(prospectus_root, source_paths)
    pdf_paths = selected_paths if selected_paths is not None else sorted(prospectus_root.glob("*.pdf"), key=lambda path: path.name.lower())
    extraction_environment = inspect_extraction_environment().as_dict()

    for pdf_path in pdf_paths:
        prospectus_id = prospectus_id_from_filename(pdf_path.name)
        base_item = {
            "prospectus_id": prospectus_id,
            "source_path": str(pdf_path),
            "source_filename": pdf_path.name,
            "source_sha256": sha256_file(pdf_path),
            "issuer_hint": issuer_hint_from_filename(pdf_path.name),
            "document_type_hint": document_type_hint_from_filename(pdf_path.name),
            "contract_path": None,
            "review_path": None,
            "duplicate_of": None,
            "raw_delete_allowed_after_review": False,
        }
        existing_sources = existing_by_source.get(base_item["source_sha256"]) or existing_by_source.get(str(pdf_path)) or []
        if existing_sources:
            for existing_source in existing_sources:
                item = dict(base_item)
                item.update(
                    {
                        "status": "contract_available_needs_review",
                        "contract_id": existing_source["id"],
                        "contract_path": existing_source["path"],
                        "message": "Raw prospectus already linked to an existing contract; existing manually reviewed fields were preserved.",
                        "raw_delete_allowed_after_review": True,
                    }
                )
                queue_items.append(item)
            continue
        extraction = _load_fixture_or_extract(pdf_path, prospectus_id, fixture_root)
        base_item["extraction"] = _extraction_summary(extraction)
        if not extraction.has_text:
            if extraction.status == "backend_missing":
                base_item["status"] = "needs_extraction_backend"
                base_item["message"] = "PDF text backend is unavailable; install/configure PyMuPDF or add a reviewed page-text fixture."
                base_item["blocker"] = "backend_missing"
            elif extraction.status == "backend_failed":
                base_item["status"] = "needs_extraction_backend"
                base_item["message"] = "PDF text backend could not open this file; verify the PDF or add a reviewed page-text fixture."
                base_item["blocker"] = "backend_failed"
            else:
                base_item["status"] = "needs_ocr"
                base_item["message"] = "Text-layer extraction found no usable text; scanned/image-only document needs OCR or a reviewed page-text fixture."
                base_item["blocker"] = "scanned_pdf_or_low_text"
            queue_items.append(base_item)
            needs_extraction += 1
            continue
        try:
            contracts = draft_contracts_from_text(extraction.text, source_file=str(pdf_path))
            if not contracts:
                raise ValueError("unsupported prospectus text; no conservative draft template matched")
        except Exception as exc:
            base_item["status"] = "needs_manual_template"
            base_item["message"] = f"Text extracted but no conservative draft template matched: {exc}"
            queue_items.append(base_item)
            failed += 1
            continue

        for contract in contracts:
            item = dict(base_item)
            _attach_ingest_metadata(contract, pdf_path, extraction)
            if extraction.pages:
                contract = attach_source_evidence(contract, extraction)
                _attach_ingest_metadata(contract, pdf_path, extraction)
            key = contract_instrument_key(contract)
            item["instrument_key"] = key
            duplicate_of = existing_by_key.get(key)
            if duplicate_of:
                item["status"] = "duplicate_skipped"
                item["duplicate_of"] = duplicate_of["id"]
                item["contract_path"] = duplicate_of["path"]
                item["message"] = "Duplicate instrument key matched existing contract; no new contract written."
                if delete_duplicate_raw:
                    pdf_path.unlink(missing_ok=True)
                    item["duplicate_raw_deleted"] = True
                queue_items.append(item)
                duplicates += 1
                continue

            contract_id = str(contract["id"])
            contract_path = contracts_root / f"{contract_id}.json"
            review_path = reviews_root / f"{contract_id}_review.md"
            if contract_path.exists():
                item["status"] = "duplicate_skipped"
                item["duplicate_of"] = contract_id
                item["contract_path"] = str(contract_path)
                item["message"] = "Contract path already exists; no overwrite performed."
                queue_items.append(item)
                duplicates += 1
                continue
            contract_path.write_text(dumps_json(contract, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            review_path.write_text(build_review_report(contract, extraction), encoding="utf-8")
            existing_by_key[key] = {"id": contract_id, "path": str(contract_path)}
            evidence = evidence_summary(contract)
            item.update(
                {
                    "status": "contract_available_needs_review",
                    "contract_id": contract_id,
                    "contract_path": str(contract_path),
                    "review_path": str(review_path),
                    "review_status": contract.get("source_review", {}).get("review_status"),
                    "evidence_status": evidence["evidence_status"],
                    "missing_required_evidence": evidence["missing_required_fields"],
                    "raw_delete_allowed_after_review": True,
                }
            )
            queue_items.append(item)
            created += 1

    queue_path = coverage_root / "review_queue.json"
    if selected_paths is not None:
        queue_items = _merge_selected_queue_items(queue_path, queue_items)
    queue_path.write_text(dumps_json(queue_items, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return AutoIngestReport(
        scanned=len(pdf_paths),
        created_contracts=created,
        duplicates=duplicates,
        needs_extraction=needs_extraction,
        failed=failed,
        queue_path=queue_path,
        items=queue_items,
        extraction_environment=extraction_environment,
    )


def approve_reviewed_contract(
    contract_path: str | Path,
    *,
    delete_raw: bool = False,
    archive_dir: str | Path | None = None,
    raw_root: str | Path | None = None,
) -> dict[str, Any]:
    path = Path(contract_path).resolve()
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("status") != "reviewed":
        raise ValueError("raw prospectus cleanup requires contract status='reviewed'")
    source_file = raw.get("source_file") or (raw.get("source_review") or {}).get("raw_prospectus_path")
    if not source_file:
        raise ValueError("contract has no source_file to clean up")
    project_root = path.parent.parent.parent
    allowed_root = Path(raw_root).resolve() if raw_root is not None else (project_root / "data" / "raw" / "prospectuses").resolve()
    source_path = Path(source_file)
    if not source_path.is_absolute():
        source_path = project_root / source_path
    source_path = source_path.resolve()
    result = {"contract_path": str(path), "source_path": str(source_path), "raw_deleted": False, "raw_archived": False}
    if not source_path.exists():
        return result
    try:
        source_path.relative_to(allowed_root)
    except ValueError as exc:
        raise ValueError(f"raw prospectus cleanup source escapes raw root: {source_path}") from exc
    expected_sha = (raw.get("source_review") or {}).get("raw_prospectus_sha256")
    if not expected_sha:
        raise ValueError("raw prospectus cleanup requires source_review.raw_prospectus_sha256")
    if sha256_file(source_path) != expected_sha:
        raise ValueError("raw prospectus checksum differs from reviewed contract metadata")
    if archive_dir is not None:
        archive_root = Path(archive_dir)
        archive_root.mkdir(parents=True, exist_ok=True)
        destination = (archive_root / source_path.name).resolve()
        shutil.move(str(source_path), str(destination))
        result.update({"raw_archived": True, "archive_path": str(destination)})
    elif delete_raw:
        source_path.unlink()
        result["raw_deleted"] = True
    return result


def _selected_pdf_paths(prospectus_root: Path, source_paths: list[str | Path] | None) -> list[Path] | None:
    if source_paths is None:
        return None
    root = prospectus_root.resolve()
    selected: list[Path] = []
    seen: set[Path] = set()
    for raw in source_paths:
        path = Path(raw)
        if not path.is_absolute():
            # Accept either paths relative to the current project/root or bare filenames.
            candidate = path.resolve()
            if not candidate.exists():
                candidate = prospectus_root / path.name if len(path.parts) == 1 else Path.cwd() / path
            path = candidate
        path = path.resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"selected source path must be under {prospectus_root}: {raw}") from exc
        if path.suffix.lower() != ".pdf":
            raise ValueError(f"selected source path must be a PDF: {raw}")
        if not path.exists():
            raise ValueError(f"selected source path does not exist: {raw}")
        if path not in seen:
            selected.append(path)
            seen.add(path)
    return sorted(selected, key=lambda path: path.name.lower())


def _merge_selected_queue_items(queue_path: Path, selected_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected_keys = {str(Path(str(item.get("source_path") or "")).resolve()) for item in selected_items}
    existing_items: list[dict[str, Any]] = []
    if queue_path.exists():
        try:
            raw = json.loads(queue_path.read_text(encoding="utf-8"))
        except Exception:
            raw = []
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                source = item.get("source_path")
                try:
                    key = str(Path(str(source)).resolve()) if source else ""
                except Exception:
                    key = ""
                if key not in selected_keys:
                    existing_items.append(item)
    return existing_items + selected_items


def contract_instrument_key(raw: dict[str, Any]) -> str:
    issuer = _nested(raw, "issuer", "name") or ""
    underlying = _nested(raw, "conversion", "underlying_ticker") or ""
    maturity = _nested(raw, "bond", "maturity_date") or ""
    isin = str(raw.get("isin") or raw.get("ISIN") or _nested(raw, "instrument", "canonical_id") or "").strip().lower()
    if isin:
        return f"isin:{isin}"
    return "|".join(str(part).strip().lower() for part in (issuer, underlying, maturity))


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_existing_contract_keys(contracts_dir: Path) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    if not contracts_dir.exists():
        return result
    for path in sorted(contracts_dir.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        key = contract_instrument_key(raw)
        if key.strip("|"):
            result[key] = {"id": str(raw.get("id") or path.stem), "path": str(path)}
    return result


def _load_existing_contract_sources(contracts_dir: Path) -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = {}
    if not contracts_dir.exists():
        return result
    for path in sorted(contracts_dir.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        contract_id = str(raw.get("id") or path.stem)
        source_review = raw.get("source_review") or {}
        entry = {"id": contract_id, "path": str(path)}
        source_sha = source_review.get("raw_prospectus_sha256")
        if source_sha:
            result.setdefault(str(source_sha), []).append(entry)
        source_file = raw.get("source_file") or source_review.get("raw_prospectus_path")
        if source_file:
            result.setdefault(str(source_file), []).append(entry)
    return result


def _load_fixture_or_extract(pdf_path: Path, prospectus_id: str, fixture_dir: Path | None) -> ExtractionResult:
    if fixture_dir is not None:
        candidates = [
            fixture_dir / f"{prospectus_id}_pages_seed.json",
            fixture_dir / f"{prospectus_id}.pages.json",
            pdf_path.with_suffix(".pages.json"),
        ]
        for candidate in candidates:
            if candidate.exists():
                return _load_text_fixture(candidate, pdf_path)
    return extract_text_from_pdf(pdf_path)


def _load_text_fixture(path: Path, pdf_path: Path) -> ExtractionResult:
    raw = json.loads(path.read_text(encoding="utf-8"))
    pages = [PageText(int(item["page"]), str(item["text"])) for item in raw.get("pages", [])]
    if not pages:
        raise ValueError(f"text fixture {path} must contain at least one page entry")
    return ExtractionResult.from_pages(source_path=pdf_path, pages=pages, method=f"text-fixture:{path.name}")


def _attach_ingest_metadata(contract: dict[str, Any], pdf_path: Path, extraction: ExtractionResult) -> None:
    source_review = contract.setdefault("source_review", {})
    source_review["raw_prospectus_sha256"] = sha256_file(pdf_path)
    source_review["raw_prospectus_path"] = str(pdf_path)
    source_review["automated_ingest"] = True
    source_review["extraction"] = _extraction_summary(extraction)


def _extraction_summary(extraction: ExtractionResult) -> dict[str, Any]:
    page_char_counts = [page.char_count for page in extraction.page_metadata]
    low_text_pages = [page.page_number for page in extraction.page_metadata if page.status in {"empty", "low_text", "failed"}]
    return {
        "method": extraction.method,
        "status": extraction.status,
        "page_count": extraction.page_count,
        "extracted_characters": len(extraction.text),
        "page_text_stats": {
            "pages_measured": len(extraction.page_metadata),
            "min_chars": min(page_char_counts) if page_char_counts else 0,
            "max_chars": max(page_char_counts) if page_char_counts else 0,
            "total_chars": sum(page_char_counts),
            "low_text_pages": low_text_pages[:50],
            "low_text_page_count": len(low_text_pages),
        },
        "pages": [
            {
                "page": page.page_number,
                "char_count": page.char_count,
                "text_density": round(page.text_density, 8),
                "method": page.method,
                "status": page.status,
                "warnings": page.warnings,
            }
            for page in extraction.page_metadata[:200]
        ],
        "warnings": extraction.warnings,
    }


def _nested(raw: dict[str, Any], *keys: str) -> Any:
    current: Any = raw
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current
