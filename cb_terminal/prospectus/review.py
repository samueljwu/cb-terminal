"""Prospectus contract review and validation helpers."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Any

from cb_terminal.io.contract_loader import contract_from_dict
from cb_terminal.pricing.yields import issuance_yield_checks
from cb_terminal.prospectus.evidence import REQUIRED_EVIDENCE_FIELDS, evidence_summary, missing_approval_evidence_fields
from cb_terminal.prospectus.extraction import ExtractionResult


ASSUMPTION_LIMITED_YIELD_WARNING_BPS = 25.0


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
    _require_positive(raw, issues, "bond.issue_size")
    _require_positive(raw, issues, "bond.denomination")
    _require_positive(raw, issues, "bond.issue_price")
    _require(raw, issues, "bond.coupon_rate")
    _require(raw, issues, "bond.coupon_frequency")
    _require(raw, issues, "bond.pricing_date")
    _require(raw, issues, "bond.closing_date")
    _require(raw, issues, "bond.maturity_date")
    _require_positive(raw, issues, "redemption.maturity_price")
    _require_positive(raw, issues, "conversion.initial_conversion_price")
    _require(raw, issues, "conversion.underlying_ticker")
    _validate_investor_economics(raw, issues)
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


def _validate_investor_economics(raw: dict[str, Any], issues: list[ReviewIssue]) -> None:
    issuer = str(_get(raw, "issuer.name") or "").strip()
    if len(issuer) > 140 or any(token in issuer.lower() for token in ("important notice", "use of proceeds", "denomination", "securities offered")):
        issues.append(ReviewIssue("error", "issuer.name", "Issuer name resembles boilerplate or a term-table value rather than a legal entity."))

    issue_size = _as_float(_get(raw, "bond.issue_size"))
    denomination = _as_float(_get(raw, "bond.denomination"))
    denomination_increment = _as_float(_get(raw, "bond.denomination_increment"))
    if issue_size is not None and denomination is not None and denomination > issue_size:
        issues.append(ReviewIssue("error", "bond.denomination", "Denomination cannot exceed aggregate issue size."))
    if denomination_increment is not None:
        if denomination_increment <= 0:
            issues.append(ReviewIssue("error", "bond.denomination_increment", "Denomination increment must be positive."))
        elif denomination is not None and denomination_increment > denomination:
            issues.append(ReviewIssue("warning", "bond.denomination_increment", "Increment exceeds the minimum denomination; confirm the source wording."))

    coupon_rate = _as_float(_get(raw, "bond.coupon_rate"))
    coupon_frequency = _as_float(_get(raw, "bond.coupon_frequency"))
    if coupon_rate is not None and coupon_rate < 0:
        issues.append(ReviewIssue("error", "bond.coupon_rate", "Coupon rate cannot be negative."))
    if coupon_frequency is not None and (coupon_frequency < 0 or not coupon_frequency.is_integer()):
        issues.append(ReviewIssue("error", "bond.coupon_frequency", "Coupon frequency must be a non-negative integer."))
    elif coupon_rate is not None and coupon_rate > 0 and coupon_frequency == 0:
        issues.append(ReviewIssue("error", "bond.coupon_frequency", "A positive coupon requires at least one coupon payment per year."))

    brokerage_raw = _get(raw, "bond.brokerage")
    brokerage = _as_float(brokerage_raw)
    if brokerage_raw not in (None, ""):
        if brokerage is None or not math.isfinite(brokerage):
            issues.append(ReviewIssue("error", "bond.brokerage", "Brokerage must be a finite number of percentage points."))
        elif not 0.0 <= brokerage <= 100.0:
            issues.append(ReviewIssue("error", "bond.brokerage", "Brokerage must be between 0 and 100 percentage points."))

    issue_price = _as_float(_get(raw, "bond.issue_price"))
    investor_offer_raw = _get(raw, "bond.investor_offer_price")
    investor_offer_price = _as_float(investor_offer_raw)
    if investor_offer_raw not in (None, "") and (
        investor_offer_price is None or not math.isfinite(investor_offer_price)
    ):
        issues.append(ReviewIssue("error", "bond.investor_offer_price", "Investor offer price must be a finite price per 100."))
    if investor_offer_raw not in (None, "") and brokerage_raw in (None, ""):
        issues.append(
            ReviewIssue(
                "error",
                "bond.brokerage",
                "Brokerage is required whenever investor offer price is populated.",
            )
        )
    if brokerage is not None and math.isfinite(brokerage) and 0.0 <= brokerage <= 100.0:
        if investor_offer_price is None or not math.isfinite(investor_offer_price):
            issues.append(
                ReviewIssue(
                    "error",
                    "bond.investor_offer_price",
                    "Investor offer price is required when brokerage is stated.",
                )
            )
        elif issue_price is not None and math.isfinite(issue_price):
            expected_offer_price = issue_price + brokerage
            if not math.isclose(investor_offer_price, expected_offer_price, rel_tol=0.0, abs_tol=1e-6):
                issues.append(
                    ReviewIssue(
                        "error",
                        "bond.investor_offer_price",
                        "Investor offer price must equal issue price plus brokerage.",
                    )
                )

    _validate_quoted_yield_pair(
        _get(raw, "redemption.yield_to_maturity"),
        _get(raw, "redemption.yield_to_maturity_frequency"),
        issues,
        yield_field="redemption.yield_to_maturity",
        frequency_field="redemption.yield_to_maturity_frequency",
    )

    legal_currency = str(_get(raw, "bond.currency") or "").upper()
    economic_currency = str(_get(raw, "bond.economic_currency") or legal_currency).upper()
    if economic_currency and legal_currency and economic_currency != legal_currency:
        issues.append(
            ReviewIssue(
                "warning",
                "bond.economic_currency",
                "Principal is economically linked to a different currency; the current lattice is a legal-currency approximation and does not revalue the contractual settlement-equivalent amount.",
            )
        )

    exchangeable_terms = raw.get("exchangeable_terms") or {}
    if isinstance(exchangeable_terms, dict) and exchangeable_terms:
        if exchangeable_terms.get("issuer_cash_election"):
            issues.append(
                ReviewIssue(
                    "warning",
                    "exchangeable_terms.issuer_cash_election",
                    "Issuer may satisfy exchange rights in cash using an averaging period; the lattice assumes share-equivalent value and does not model election timing or averaging optionality.",
                )
            )
        if exchangeable_terms.get("share_redemption_option"):
            issues.append(
                ReviewIssue(
                    "warning",
                    "exchangeable_terms.share_redemption_option",
                    "Share-redemption election changes maturity and exchange cutoffs; the conservative conditional cutoff is used and the election itself is not modeled.",
                )
            )

    for field in ("bond.issue_price", "bond.investor_offer_price", "redemption.maturity_price"):
        value = _as_float(_get(raw, field))
        if value is not None and not 50.0 <= value <= 200.0:
            issues.append(ReviewIssue("error", field, "Price per 100 is outside the conservative 50–200 review range."))

    pricing = _as_date(_get(raw, "bond.pricing_date"))
    closing = _as_date(_get(raw, "bond.closing_date"))
    maturity = _as_date(_get(raw, "bond.maturity_date"))
    if pricing and closing and pricing > closing:
        issues.append(ReviewIssue("error", "bond.pricing_date", "Pricing date must not be after closing/issue date."))
    if closing and maturity and closing >= maturity:
        issues.append(ReviewIssue("error", "bond.closing_date", "Closing/issue date must be before maturity."))

    conversion_start = _as_date(_get(raw, "conversion.start_date"))
    conversion_end = _as_date(_get(raw, "conversion.end_date"))
    if conversion_start and closing and conversion_start < closing:
        issues.append(ReviewIssue("error", "conversion.start_date", "Conversion cannot begin before the bond is issued."))
    if conversion_start and pricing and conversion_start < pricing:
        issues.append(ReviewIssue("error", "conversion.start_date", "Conversion cannot begin before the bond is priced."))
    if conversion_start and conversion_end and conversion_start > conversion_end:
        issues.append(ReviewIssue("error", "conversion.start_date", "Conversion start date must not be after conversion end date."))
    if conversion_end and maturity and conversion_end > maturity:
        issues.append(ReviewIssue("error", "conversion.end_date", "Conversion end date must not be after maturity."))
    conversion_windows = _get(raw, "conversion.windows") or []
    previous_end: date | None = None
    for index, window in enumerate(conversion_windows):
        if not isinstance(window, dict):
            issues.append(ReviewIssue("error", f"conversion.windows.{index}", "Conversion window must be an object."))
            continue
        window_start = _as_date(window.get("start_date"))
        window_end = _as_date(window.get("end_date"))
        if window_start is None or window_end is None or window_start > window_end:
            issues.append(ReviewIssue("error", f"conversion.windows.{index}", "Conversion window needs ordered start and end dates."))
            continue
        if pricing and window_start < pricing:
            issues.append(ReviewIssue("error", f"conversion.windows.{index}.start_date", "Conversion window cannot begin before pricing."))
        if closing and window_start < closing:
            issues.append(ReviewIssue("error", f"conversion.windows.{index}.start_date", "Conversion window cannot begin before issuance."))
        if maturity and window_end > maturity:
            issues.append(ReviewIssue("error", f"conversion.windows.{index}.end_date", "Conversion window cannot end after maturity."))
        if conversion_start and window_start < conversion_start:
            issues.append(ReviewIssue("error", f"conversion.windows.{index}.start_date", "Conversion window starts before the outer conversion period."))
        if conversion_end and window_end > conversion_end:
            issues.append(ReviewIssue("error", f"conversion.windows.{index}.end_date", "Conversion window ends after the outer conversion period."))
        if previous_end is not None and window_start <= previous_end:
            issues.append(ReviewIssue("error", f"conversion.windows.{index}", "Conversion windows must be ordered and non-overlapping."))
        previous_end = window_end
    if len(conversion_windows) > 1:
        issues.append(
            ReviewIssue(
                "warning",
                "conversion.windows",
                "Multiple disjoint conversion windows are enforced by the lattice; verify each boundary and any event-driven exceptions.",
            )
        )
    if _get(raw, "conversion.calendar_status"):
        issues.append(
            ReviewIssue(
                "warning",
                "conversion.end_date",
                "Conditional or working/trading-day conversion cutoff was resolved using a conservative weekday approximation; confirm the contractual exchange and banking calendars before approval.",
            )
        )
    if _get(raw, "conversion.conditional_early_start_rule"):
        issues.append(
            ReviewIssue(
                "warning",
                "conversion.conditional_early_start_rule",
                "Conversion can open early only after specified call or event conditions; the lattice uses the ordinary conversion start and does not model those event-contingent rights.",
            )
        )

    reference = _as_float(_get(raw, "conversion.reference_share_price"))
    conversion_price = _as_float(_get(raw, "conversion.initial_conversion_price"))
    premium = _as_float(_get(raw, "conversion.conversion_premium"))
    if reference is not None and conversion_price is not None and premium is not None:
        expected = reference * (1.0 + premium / 100.0)
        if expected > 0 and abs(conversion_price - expected) / expected > 0.005:
            issues.append(
                ReviewIssue(
                    "error",
                    "conversion.initial_conversion_price",
                    "Conversion price does not reconcile with reference share price and stated premium within 0.5%.",
                )
            )

    for index, call in enumerate(raw.get("calls") or []):
        if not isinstance(call, dict):
            continue
        if call.get("model_type") == "soft_call_unresolved" or not call.get("start_date"):
            issues.append(ReviewIssue("warning", f"calls.{index}.start_date", "Soft-call start is unresolved and will not be applied by the lattice."))
        if call.get("start_date_calendar_status"):
            issues.append(ReviewIssue("warning", f"calls.{index}.start_date", "Trading-day call start uses a weekday-only approximation; confirm the contractual exchange calendar."))
        if call.get("price_rule") == "early_redemption_amount":
            issues.append(ReviewIssue("warning", f"calls.{index}.price", "Call pays a dynamic Early Redemption Amount; a static call price is only an approximation."))
        if call.get("trigger_basis") not in (None, "", "conversion_price"):
            issues.append(ReviewIssue("warning", f"calls.{index}.trigger_basis", "Call trigger uses a dynamic Early Redemption Amount/conversion-ratio basis; the static barrier is only an approximation."))
        call_date = _as_date(call.get("start_date"))
        if call_date and maturity and call_date >= maturity:
            issues.append(ReviewIssue("error", f"calls.{index}.start_date", "Issuer call cannot start on or after maturity."))

    for index, put in enumerate(raw.get("puts") or []):
        if not isinstance(put, dict):
            continue
        if put.get("model_type") != "scheduled_put":
            if put.get("yield_to_put") not in (None, "") or put.get("yield_to_put_frequency") not in (None, ""):
                issues.append(
                    ReviewIssue(
                        "error",
                        f"puts.{index}.yield_to_put",
                        "Quoted yield to put is only valid for a scheduled holder put.",
                    )
                )
            continue
        put_date = _as_date(put.get("date"))
        if put_date and closing and put_date <= closing:
            issues.append(ReviewIssue("error", f"puts.{index}.date", "Scheduled holder put must occur after closing."))
        if put_date and maturity and put_date >= maturity:
            issues.append(ReviewIssue("error", f"puts.{index}.date", "Scheduled holder put must occur before maturity."))
        if put.get("yield_to_put") not in (None, "") and put_date is None:
            issues.append(
                ReviewIssue(
                    "error",
                    f"puts.{index}.date",
                    "Quoted yield to put requires a valid scheduled put date.",
                )
            )
        _validate_quoted_yield_pair(
            put.get("yield_to_put"),
            put.get("yield_to_put_frequency"),
            issues,
            yield_field=f"puts.{index}.yield_to_put",
            frequency_field=f"puts.{index}.yield_to_put_frequency",
        )
    _validate_issuance_yield_reconciliation(raw, issues)


def _validate_issuance_yield_reconciliation(
    raw: dict[str, Any],
    issues: list[ReviewIssue],
) -> None:
    """Flag material source-quote differences without replacing source terms."""

    try:
        checks = issuance_yield_checks(contract_from_dict(raw))
    except (TypeError, ValueError, OverflowError):
        # Missing/invalid scalar terms already receive field-specific issues.
        return

    coupon_rate = _as_float(_get(raw, "bond.coupon_rate")) or 0.0

    def append_mismatch(field: str, check: dict[str, Any]) -> None:
        if check.get("status") != "mismatch":
            return
        difference_bps = float(check.get("difference_bps") or 0.0)
        quoted = check.get("quoted_yield_percent")
        calculated = check.get("calculated_yield_percent")
        calculation = check.get("calculation") if isinstance(check.get("calculation"), dict) else {}
        assumption_limited = (
            coupon_rate > 0.0
            and calculation.get("status") == "calculated_with_assumptions"
        )
        warning_only = (
            assumption_limited
            and abs(difference_bps) <= ASSUMPTION_LIMITED_YIELD_WARNING_BPS
        )
        severity = "warning" if warning_only else "error"
        suffix = (
            " Confirm day count, clean/dirty price, coupon dates, and accrued-interest terms."
            if warning_only
            else (
                " The difference exceeds the assumption allowance; confirm both the source "
                "extraction and the exact coupon schedule."
                if assumption_limited
                else " Confirm the source extraction and issue-date cash flows."
            )
        )
        issues.append(
            ReviewIssue(
                severity,
                field,
                "Quoted yield "
                f"{float(quoted):.6g}% does not reconcile with calculated issuance yield "
                f"{float(calculated):.6g}% ({difference_bps:+.2f} bp; "
                f"tolerance {float(check.get('tolerance_bps') or 0.0):.2f} bp)."
                + suffix,
            )
        )

    append_mismatch(
        "redemption.yield_to_maturity",
        checks.get("yield_to_maturity") or {},
    )
    for put_check in checks.get("yield_to_puts") or []:
        if not isinstance(put_check, dict):
            continue
        index = put_check.get("put_index")
        if isinstance(index, int):
            append_mismatch(f"puts.{index}.yield_to_put", put_check)


def _validate_quoted_yield_pair(
    yield_raw: Any,
    frequency_raw: Any,
    issues: list[ReviewIssue],
    *,
    yield_field: str,
    frequency_field: str,
) -> None:
    if yield_raw in (None, "") and frequency_raw in (None, ""):
        return
    quoted_yield = _as_float(yield_raw)
    if quoted_yield is None or not math.isfinite(quoted_yield):
        issues.append(ReviewIssue("error", yield_field, "Quoted yield must be a finite percentage."))
    elif not -100.0 < quoted_yield <= 100.0:
        issues.append(ReviewIssue("error", yield_field, "Quoted yield must be greater than -100% and no more than 100%."))

    frequency = _as_float(frequency_raw)
    if frequency is None or not math.isfinite(frequency) or not frequency.is_integer() or not 1 <= frequency <= 365:
        issues.append(
            ReviewIssue(
                "error",
                frequency_field,
                "Quoted yield compounding frequency must be an integer between 1 and 365.",
            )
        )


def _as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


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
