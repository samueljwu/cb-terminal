"""Prospectus contract review and validation helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cb_terminal.prospectus.evidence import REQUIRED_EVIDENCE_FIELDS, evidence_summary, missing_approval_evidence_fields
from cb_terminal.prospectus.extraction import ExtractionResult


@dataclass(frozen=True)
class ReviewIssue:
    severity: str
    field: str
    message: str


def validate_contract_dict(raw: dict[str, Any]) -> list[ReviewIssue]:
    issues: list[ReviewIssue] = []
    _require(raw, issues, "id")
    _require(raw, issues, "issuer.name")
    _require(raw, issues, "bond.currency")
    _require(raw, issues, "bond.settlement_currency")
    _require(raw, issues, "bond.stock_currency")
    _require_positive(raw, issues, "bond.pricing_face")
    _require(raw, issues, "bond.maturity_date")
    _require_positive(raw, issues, "redemption.maturity_price")
    _require_positive(raw, issues, "conversion.initial_conversion_price")
    _require(raw, issues, "conversion.underlying_ticker")
    if _get(raw, "bond.currency") != _get(raw, "bond.stock_currency"):
        if _get(raw, "conversion.fixed_exchange_rate") in (None, ""):
            issues.append(ReviewIssue("error", "conversion.fixed_exchange_rate", "Cross-currency CB requires fixed conversion FX or explicit exception."))
        if _get(raw, "conversion.fixed_exchange_rate_units") in (None, ""):
            issues.append(ReviewIssue("error", "conversion.fixed_exchange_rate_units", "Cross-currency CB requires fixed FX units, standardized as stock currency per CB currency."))
    status = raw.get("status")
    if status not in {"needs_review", "reviewed", "indicative"}:
        issues.append(ReviewIssue("warning", "status", "Use status needs_review, reviewed, or indicative."))
    review_items = _get(raw, "source_review.review_items") or []
    if len(review_items) < 5:
        issues.append(ReviewIssue("warning", "source_review.review_items", "Add a fuller manual review checklist before using in a universe."))
    term_evidence = _get(raw, "source_review.term_evidence") or {}
    missing_evidence = missing_approval_evidence_fields(term_evidence, raw)
    if missing_evidence:
        issues.append(
            ReviewIssue(
                "warning",
                "source_review.term_evidence",
                "Missing page-level source evidence for approval-required fields: " + ", ".join(missing_evidence),
            )
        )
    return issues


def _require(raw: dict[str, Any], issues: list[ReviewIssue], dotted: str) -> None:
    if _get(raw, dotted) in (None, ""):
        issues.append(ReviewIssue("error", dotted, "Required field is missing."))


def _require_positive(raw: dict[str, Any], issues: list[ReviewIssue], dotted: str) -> None:
    value = _get(raw, dotted)
    try:
        if float(value) <= 0:
            raise ValueError
    except Exception:
        issues.append(ReviewIssue("error", dotted, "Required numeric field must be positive."))


def _get(raw: dict[str, Any], dotted: str) -> Any:
    current: Any = raw
    for part in dotted.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def build_review_report(raw: dict[str, Any], extraction: ExtractionResult | None) -> str:
    issues = validate_contract_dict(raw)
    lines = [
        f"# Prospectus Review: {raw.get('id', '<unknown>')}",
        "",
        f"Issuer: {_get(raw, 'issuer.name') or '<missing>'}",
        f"Description: {_get(raw, 'bond.description') or '<missing>'}",
        f"Status: {raw.get('status', '<missing>')}",
        "",
        "## Extraction",
    ]
    if extraction is None:
        lines.append("- No extraction result attached; review generated from contract draft only.")
    else:
        lines.extend(
            [
                f"- Source: {extraction.source_path}",
                f"- Method: {extraction.method}",
                f"- Pages: {extraction.page_count}",
                f"- Extracted characters: {len(extraction.text)}",
            ]
        )
        for warning in extraction.warnings:
            lines.append(f"- Warning: {warning}")
    lines.extend(["", "## Validation issues"])
    if not issues:
        lines.append("- None.")
    else:
        for issue in issues:
            lines.append(f"- {issue.severity.upper()} {issue.field}: {issue.message}")
    lines.extend(["", "## Manual review checklist"])
    for item in (_get(raw, "source_review.review_items") or []):
        lines.append(f"- [ ] {item}")

    summary = evidence_summary(raw)
    lines.extend(
        [
            "",
            "## Source evidence matrix",
            f"- Audit fields covered: {summary['covered_required_fields']}/{summary['required_fields']}",
            f"- Approval fields covered: {summary['covered_approval_fields']}/{summary['approval_required_fields']}",
        ]
    )
    term_evidence = _get(raw, "source_review.term_evidence") or {}
    if not term_evidence:
        lines.append("- No page-level term evidence attached yet.")
    else:
        for field in REQUIRED_EVIDENCE_FIELDS:
            entries = term_evidence.get(field) or []
            if not entries:
                lines.append(f"- {field}: MISSING")
                continue
            entry = entries[0]
            lines.append(
                f"- {field}: page {entry.get('page')} confidence {entry.get('confidence')} — {entry.get('snippet')}"
            )
    lines.extend(["", "## Evidence gaps"])
    if summary["missing_required_fields"]:
        for field in summary["missing_required_fields"]:
            lines.append(f"- {field}")
    else:
        lines.append("- No required evidence gaps detected. Human review is still required before status=reviewed.")

    targeted_candidates = _get(raw, "source_review.targeted_term_candidates") or {}
    lines.extend(["", "## Targeted term-search candidates"])
    if not targeted_candidates:
        lines.append("- No targeted term candidates attached.")
    else:
        for term_key in sorted(targeted_candidates):
            entries = targeted_candidates.get(term_key) or []
            if not entries:
                continue
            lines.append(f"- {term_key}:")
            for entry in entries[:3]:
                lines.append(
                    f"  - page {entry.get('page')} confidence {entry.get('confidence')} — {entry.get('snippet')}"
                )
    lines.extend(
        [
            "",
            "## Key modeled terms",
            f"- Bond currency: {_get(raw, 'bond.currency')}",
            f"- Stock currency: {_get(raw, 'bond.stock_currency')}",
            f"- Underlying: {_get(raw, 'conversion.underlying_ticker')}",
            f"- Conversion price: {_get(raw, 'conversion.initial_conversion_price')}",
            f"- Fixed exchange rate: {_get(raw, 'conversion.fixed_exchange_rate')} ({_get(raw, 'conversion.fixed_exchange_rate_units')})",
            f"- Maturity: {_get(raw, 'bond.maturity_date')}",
            f"- Maturity redemption: {_get(raw, 'redemption.maturity_price')}",
        ]
    )
    return "\n".join(lines) + "\n"
