"""Automated prospectus ingestion, review queue, and duplicate controls."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from cb_terminal.domain import dumps_json
from cb_terminal.io.contract_loader import contract_from_dict
from cb_terminal.pricing.yields import issuance_yield_checks
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


def backfill_missing_issuance_economics(
    *,
    contract_path: str | Path,
    source_path: str | Path,
    fixture_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Backfill or safely reconcile issuance economics from the linked PDF.

    Existing human-edited terms are never replaced. A non-empty automated yield
    may be corrected only when the old value fails the independent cash-flow
    check and a fresh extraction passes it. Any update returns the contract to
    human review and writes a timestamped backup first.
    """

    contract_file = Path(contract_path).resolve()
    source_file = Path(source_path).resolve()
    fixture_root = Path(fixture_dir) if fixture_dir is not None else None
    if not contract_file.is_file():
        raise ValueError(f"contract does not exist: {contract_file}")
    if not source_file.is_file() or source_file.suffix.lower() != ".pdf":
        raise ValueError(f"linked prospectus PDF does not exist: {source_file}")

    existing = json.loads(contract_file.read_text(encoding="utf-8"))
    if not isinstance(existing, dict):
        raise ValueError("contract JSON must contain an object")
    source_review = existing.get("source_review") if isinstance(existing.get("source_review"), Mapping) else {}
    expected_sha = str(source_review.get("raw_prospectus_sha256") or "").strip()
    linked_path = str(existing.get("source_file") or source_review.get("raw_prospectus_path") or "").strip()
    if expected_sha:
        if sha256_file(source_file) != expected_sha:
            raise ValueError("linked prospectus hash does not match the contract review record")
    elif linked_path:
        if Path(linked_path).resolve() != source_file:
            raise ValueError("selected PDF is not the source linked to this contract")
    else:
        raise ValueError("contract has no auditable linked prospectus")

    prospectus_id = prospectus_id_from_filename(source_file.name)
    extraction = _load_fixture_or_extract(source_file, prospectus_id, fixture_root)
    if not extraction.has_text:
        raise ValueError("linked prospectus has no usable text layer; OCR or a reviewed text fixture is required")
    drafts = draft_contracts_from_text(extraction.text, source_file=str(source_file))
    existing_key = contract_instrument_key(existing)
    matches = [draft for draft in drafts if contract_instrument_key(draft) == existing_key]
    if not matches:
        existing_id = str(existing.get("id") or contract_file.stem)
        matches = [draft for draft in drafts if str(draft.get("id") or "") == existing_id]
    if len(matches) != 1:
        raise ValueError("could not uniquely match the linked PDF draft to this contract")
    extracted = matches[0]
    _attach_ingest_metadata(extracted, source_file, extraction)
    extracted = attach_source_evidence(extracted, extraction)

    updated = deepcopy(existing)
    added_fields: list[str] = []
    corrected_fields: list[str] = []
    protected_fields = _human_edited_fields(updated)

    extracted_brokerage = _nested(extracted, "bond", "brokerage")
    if (
        "bond.brokerage" not in protected_fields
        and _missing(_nested(updated, "bond", "brokerage"))
        and not _missing(extracted_brokerage)
    ):
        _set_nested(updated, ("bond", "brokerage"), extracted_brokerage)
        added_fields.append("bond.brokerage")
    issue_price = _nested(updated, "bond", "issue_price")
    updated_brokerage = _nested(updated, "bond", "brokerage")
    if (
        "bond.investor_offer_price" not in protected_fields
        and _missing(_nested(updated, "bond", "investor_offer_price"))
        and not _missing(issue_price)
        and not _missing(updated_brokerage)
    ):
        _set_nested(
            updated,
            ("bond", "investor_offer_price"),
            round(float(issue_price) + float(updated_brokerage), 10),
        )
        added_fields.append("bond.investor_offer_price")

    _backfill_pair(
        updated,
        extracted,
        ("redemption", "yield_to_maturity"),
        ("redemption", "yield_to_maturity_frequency"),
        added_fields,
        protected_fields=protected_fields,
    )
    _reconcile_machine_extracted_yield_pair(
        updated,
        extracted,
        first_path=("redemption", "yield_to_maturity"),
        second_path=("redemption", "yield_to_maturity_frequency"),
        check_kind="yield_to_maturity",
        corrected_fields=corrected_fields,
    )

    extracted_scheduled_by_date = {
        str(put.get("date") or ""): (index, put)
        for index, put in enumerate(extracted.get("puts") or [])
        if isinstance(put, Mapping)
        and put.get("model_type") == "scheduled_put"
        and put.get("date")
    }
    for index, put in enumerate(updated.get("puts") or []):
        if not isinstance(put, dict) or put.get("model_type") != "scheduled_put":
            continue
        candidate = extracted_scheduled_by_date.get(str(put.get("date") or ""))
        if candidate is None:
            continue
        _candidate_index, extracted_put = candidate
        yield_missing = _missing(put.get("yield_to_put"))
        frequency_missing = _missing(put.get("yield_to_put_frequency"))
        extracted_yield = extracted_put.get("yield_to_put")
        extracted_frequency = extracted_put.get("yield_to_put_frequency")
        if _missing(extracted_yield) or _missing(extracted_frequency):
            continue
        yield_field = f"puts.{index}.yield_to_put"
        frequency_field = f"puts.{index}.yield_to_put_frequency"
        if {yield_field, frequency_field} & protected_fields:
            continue
        if yield_missing and (
            frequency_missing
            or _values_match(put.get("yield_to_put_frequency"), extracted_frequency)
        ):
            put["yield_to_put"] = extracted_yield
            added_fields.append(yield_field)
        if frequency_missing and (
            yield_missing
            or _values_match(put.get("yield_to_put"), extracted_yield)
        ):
            put["yield_to_put_frequency"] = extracted_frequency
            added_fields.append(frequency_field)
        _reconcile_machine_extracted_put_yield(
            updated,
            extracted_put,
            put_index=index,
            corrected_fields=corrected_fields,
        )

    changed_fields = added_fields + corrected_fields
    if not changed_fields:
        return {
            "updated": False,
            "contract_path": contract_file,
            "backup_path": None,
            "added_fields": [],
            "corrected_fields": [],
            "extraction": _extraction_summary(extraction),
        }

    updated_review = updated.setdefault("source_review", {})
    if not isinstance(updated_review, dict):
        raise ValueError("source_review must be an object")
    extracted_evidence = _nested(extracted, "source_review", "term_evidence") or {}
    existing_evidence = updated_review.setdefault("term_evidence", {})
    if isinstance(existing_evidence, dict) and isinstance(extracted_evidence, Mapping):
        for field in changed_fields:
            if field == "bond.investor_offer_price":
                continue
            source_key = field
            if field.endswith("_frequency"):
                source_key = field.removesuffix("_frequency")
            source_key = _dot_put_path_to_brackets(source_key)
            target_key = _dot_put_path_to_brackets(field)
            if source_key in extracted_evidence and (
                target_key not in existing_evidence or field in corrected_fields
            ):
                existing_evidence[target_key] = deepcopy(extracted_evidence[source_key])

    updated["status"] = "needs_review"
    updated_review["review_status"] = (
        "reconciled_needs_human_review"
        if corrected_fields
        else "backfilled_needs_human_review"
    )
    updated_review["last_economics_backfill"] = {
        "backfilled_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "fields": added_fields,
        "corrected_fields": corrected_fields,
        "source_sha256": sha256_file(source_file),
    }
    added_field_set = set(changed_fields)
    errors = [
        issue
        for issue in validate_contract_dict(updated)
        if issue.severity == "error"
        and (
            issue.field in added_field_set
            or any(issue.field.startswith(field + ".") for field in added_field_set)
        )
    ]
    if errors:
        raise ValueError(
            "backfilled contract failed validation: "
            + "; ".join(f"{issue.field}: {issue.message}" for issue in errors)
        )

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = contract_file.with_name(f"{contract_file.name}.{timestamp}.bak")
    backup_path.write_text(dumps_json(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    candidate_json = dumps_json(updated, indent=2, ensure_ascii=False) + "\n"
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=contract_file.parent,
        delete=False,
        suffix=".tmp",
    ) as handle:
        handle.write(candidate_json)
        temp_path = Path(handle.name)
    temp_path.replace(contract_file)
    return {
        "updated": True,
        "contract_path": contract_file,
        "backup_path": backup_path,
        "added_fields": added_fields,
        "corrected_fields": corrected_fields,
        "extraction": _extraction_summary(extraction),
    }


def _missing(value: Any) -> bool:
    return value in (None, "", "needs_review")


def _machine_reconciliation_allowed(raw: Mapping[str, Any], fields: set[str]) -> bool:
    source_review = raw.get("source_review") if isinstance(raw.get("source_review"), Mapping) else {}
    automated = bool(source_review.get("automated_ingest")) or str(
        source_review.get("created_from") or ""
    ).startswith("automated_")
    if not automated:
        return False
    return fields.isdisjoint(_human_edited_fields(raw))


def _human_edited_fields(raw: Mapping[str, Any]) -> set[str]:
    source_review = raw.get("source_review") if isinstance(raw.get("source_review"), Mapping) else {}
    last_gui_edit = (
        source_review.get("last_gui_edit")
        if isinstance(source_review.get("last_gui_edit"), Mapping)
        else {}
    )
    edited_fields = {
        str(field)
        for field in (last_gui_edit.get("fields") or [])
    }
    edited_fields.update(
        str(field)
        for field in (source_review.get("human_edited_fields") or [])
    )
    return edited_fields


def _issuance_check(
    raw: dict[str, Any],
    *,
    check_kind: str,
    put_index: int | None = None,
) -> dict[str, Any]:
    try:
        checks = issuance_yield_checks(contract_from_dict(raw))
    except (TypeError, ValueError, OverflowError):
        return {}
    if check_kind == "yield_to_maturity":
        check = checks.get("yield_to_maturity")
        return dict(check) if isinstance(check, Mapping) else {}
    for check in checks.get("yield_to_puts") or []:
        if isinstance(check, Mapping) and check.get("put_index") == put_index:
            return dict(check)
    return {}


def _reconcile_machine_extracted_yield_pair(
    target: dict[str, Any],
    source: dict[str, Any],
    *,
    first_path: tuple[str, ...],
    second_path: tuple[str, ...],
    check_kind: str,
    corrected_fields: list[str],
) -> None:
    field_names = {".".join(first_path), ".".join(second_path)}
    if not _machine_reconciliation_allowed(target, field_names):
        return
    source_first = _nested(source, *first_path)
    source_second = _nested(source, *second_path)
    if _missing(source_first) or _missing(source_second):
        return
    if _values_match(_nested(target, *first_path), source_first) and _values_match(
        _nested(target, *second_path),
        source_second,
    ):
        return
    current_check = _issuance_check(target, check_kind=check_kind)
    candidate = deepcopy(target)
    _set_nested(candidate, first_path, source_first)
    _set_nested(candidate, second_path, source_second)
    candidate_check = _issuance_check(candidate, check_kind=check_kind)
    if (
        current_check.get("status") != "mismatch"
        or candidate_check.get("status") != "match"
    ):
        return
    _set_nested(target, first_path, source_first)
    _set_nested(target, second_path, source_second)
    corrected_fields.extend(sorted(field_names))


def _reconcile_machine_extracted_put_yield(
    target: dict[str, Any],
    extracted_put: Mapping[str, Any],
    *,
    put_index: int,
    corrected_fields: list[str],
) -> None:
    yield_field = f"puts.{put_index}.yield_to_put"
    frequency_field = f"puts.{put_index}.yield_to_put_frequency"
    fields = {yield_field, frequency_field}
    if not _machine_reconciliation_allowed(target, fields):
        return
    source_yield = extracted_put.get("yield_to_put")
    source_frequency = extracted_put.get("yield_to_put_frequency")
    if _missing(source_yield) or _missing(source_frequency):
        return
    puts = target.get("puts")
    if (
        not isinstance(puts, list)
        or put_index >= len(puts)
        or not isinstance(puts[put_index], dict)
    ):
        return
    put = puts[put_index]
    if _values_match(put.get("yield_to_put"), source_yield) and _values_match(
        put.get("yield_to_put_frequency"),
        source_frequency,
    ):
        return
    current_check = _issuance_check(
        target,
        check_kind="yield_to_put",
        put_index=put_index,
    )
    candidate = deepcopy(target)
    candidate_put = candidate["puts"][put_index]
    candidate_put["yield_to_put"] = source_yield
    candidate_put["yield_to_put_frequency"] = source_frequency
    candidate_check = _issuance_check(
        candidate,
        check_kind="yield_to_put",
        put_index=put_index,
    )
    if (
        current_check.get("status") != "mismatch"
        or candidate_check.get("status") != "match"
    ):
        return
    put["yield_to_put"] = source_yield
    put["yield_to_put_frequency"] = source_frequency
    corrected_fields.extend(sorted(fields))


def _set_nested(raw: dict[str, Any], keys: tuple[str, ...], value: Any) -> None:
    target = raw
    for key in keys[:-1]:
        child = target.setdefault(key, {})
        if not isinstance(child, dict):
            raise ValueError(".".join(keys[:-1]) + " must be an object")
        target = child
    target[keys[-1]] = value


def _backfill_pair(
    target: dict[str, Any],
    source: dict[str, Any],
    first_path: tuple[str, ...],
    second_path: tuple[str, ...],
    added_fields: list[str],
    *,
    protected_fields: set[str] | None = None,
) -> None:
    protected = protected_fields or set()
    if {".".join(first_path), ".".join(second_path)} & protected:
        return
    target_first = _nested(target, *first_path)
    target_second = _nested(target, *second_path)
    first_value = _nested(source, *first_path)
    second_value = _nested(source, *second_path)
    if _missing(first_value) or _missing(second_value):
        return
    first_missing = _missing(target_first)
    second_missing = _missing(target_second)
    if first_missing and (second_missing or _values_match(target_second, second_value)):
        _set_nested(target, first_path, first_value)
        added_fields.append(".".join(first_path))
    if second_missing and (first_missing or _values_match(target_first, first_value)):
        _set_nested(target, second_path, second_value)
        added_fields.append(".".join(second_path))


def _values_match(left: Any, right: Any) -> bool:
    try:
        return abs(float(left) - float(right)) <= 1e-9
    except (TypeError, ValueError):
        return str(left).strip() == str(right).strip()


def _dot_put_path_to_brackets(field: str) -> str:
    return re.sub(r"^puts\.(\d+)\.", r"puts[\1].", field)


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
