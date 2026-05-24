"""Page-level source evidence helpers for prospectus-derived contracts.

Evidence matching is project-local and deterministic. It should produce the
same review hints on any machine that can extract the PDF text.
"""

from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime
from typing import Any

from cb_terminal.prospectus.extraction import ExtractionResult, PageText

REQUIRED_EVIDENCE_FIELDS: tuple[str, ...] = (
    "instrument.canonical_id",
    "issuer.name",
    "bond.description",
    "bond.issue_size",
    "bond.denomination",
    "bond.issue_price",
    "bond.closing_date",
    "bond.maturity_date",
    "redemption.maturity_price",
    "conversion.underlying_ticker",
    "conversion.initial_conversion_price",
    "conversion.fixed_exchange_rate",
    "conversion.start_date",
    "conversion.end_date",
    "calls[0].trigger_ratio",
)

BASE_APPROVAL_EVIDENCE_FIELDS: tuple[str, ...] = (
    "issuer.name",
    "bond.issue_price",
    "bond.maturity_date",
    "redemption.maturity_price",
    "conversion.underlying_ticker",
    "conversion.initial_conversion_price",
)

# Always look for these CB contract concepts even when the current scalar parser
# cannot yet normalize them into the contract JSON.  These candidates make the
# review workflow reproducible: a fresh checkout knows what to hunt for without
# relying on agent memory or issuer-specific prompts.
TERM_TARGETS: tuple[dict[str, Any], ...] = (
    {"key": "instrument.canonical_id", "label": "ISIN / Common Code", "patterns": [r"\bISINs?\b[\s\S]{0,500}?\bXS[0-9A-Z]{10}\b", r"\bXS[0-9A-Z]{10}\b", r"Common Codes?[\s\S]{0,500}?\b\d{6,12}\b"]},
    {"key": "issuer.name", "label": "issuer", "patterns": [r"\bIssuer\b[\s.]{0,80}.{0,180}", r"OFFERING CIRCULAR[\s\S]{0,700}?Convertible Bonds due"]},
    {"key": "issuer.ticker", "label": "underlying ticker / stock code", "patterns": [r"\b(?:Stock Code|trading code|Security Code|Securities Identification Code)[\s\S]{0,220}?\b[0-9A-Za-z.]{3,8}\b", r"Shares[\s\S]{0,220}?(?:listed|traded)[\s\S]{0,220}?\b[0-9A-Za-z.]{3,8}\b"]},
    {"key": "bond.issue_size", "label": "issue size / principal amount", "patterns": [r"(?:aggregate principal amount|Issue Size|Deal Size|Offer Size|Securities Offered)[\s\S]{0,260}?(?:US\$|HK\$|NT\$|S\$|¥|JPY|RMB|CNH|EUR|USD|HKD|TWD|SGD)\s*[0-9][0-9,]*(?:\.\d+)?\s*(?:billion|million)?"]},
    {"key": "bond.denomination", "label": "denomination", "patterns": [r"\bDenominations?\b[\s\S]{0,240}?(?:US\$|HK\$|NT\$|S\$|¥|JPY|RMB|CNH|EUR|USD|HKD|TWD|SGD)\s*[0-9][0-9,]*(?:\.\d+)?"]},
    {"key": "bond.issue_price", "label": "issue price", "patterns": [r"\bIssue Prices?\b[\s\S]{0,500}?[0-9][0-9,]*(?:\.\d+)?\s*(?:%|per cent)", r"issue price of the Bonds[\s\S]{0,180}?[0-9][0-9,]*(?:\.\d+)?\s*(?:%|per cent)"]},
    {"key": "bond.coupon_rate", "label": "coupon", "patterns": [r"\bCoupon\b[\s\S]{0,220}?(?:Zero|[0-9][0-9,]*(?:\.\d+)?\s*(?:%|per cent))", r"\bZero Coupon Convertible Bonds\b"]},
    {"key": "bond.closing_date", "label": "closing / issue date", "patterns": [r"\b(?:Closing Date|Issue Date)\b[\s\S]{0,180}?(?:\d{1,2}\s+[A-Z][a-z]+,?\s+20\d{2}|[A-Z][a-z]+\s+\d{1,2},\s+20\d{2})"]},
    {"key": "bond.maturity_date", "label": "maturity date", "patterns": [r"\bMaturity Date\b[\s\S]{0,180}?(?:\d{1,2}\s+[A-Z][a-z]+,?\s+20\d{2}|[A-Z][a-z]+\s+\d{1,2},\s+20\d{2})", r"redeemed[\s\S]{0,160}?on\s+(?:\d{1,2}\s+[A-Z][a-z]+,?\s+20\d{2}|[A-Z][a-z]+\s+\d{1,2},\s+20\d{2})"]},
    {"key": "redemption.maturity_price", "label": "par / maturity redemption price / premium", "patterns": [r"\b(?:Redemption Price at Maturity|redeemed at|redemption at maturity)\b[\s\S]{0,240}?[0-9][0-9,]*(?:\.\d+)?\s*(?:%|per cent)", r"\b100\s*(?:%|per cent)\s+of\s+(?:their|the)\s+principal amount\b"]},
    {"key": "conversion.initial_conversion_price", "label": "initial conversion price", "patterns": [r"\bInitial Conversion Prices?\b[\s\S]{0,400}?(?:US\$|HK\$|NT\$|S\$|¥|JPY|RMB|CNH|EUR|USD|HKD|TWD|SGD)\s*[0-9][0-9,]*(?:\.\d+)?", r"initial conversion price[\s\S]{0,260}?(?:US\$|HK\$|NT\$|S\$|¥|JPY|RMB|CNH|EUR|USD|HKD|TWD|SGD)\s*[0-9][0-9,]*(?:\.\d+)?"]},
    {"key": "conversion.conversion_premium", "label": "conversion premium", "patterns": [r"\bConversion Premium\b[\s\S]{0,260}?[0-9][0-9,]*(?:\.\d+)?\s*(?:%|per cent)", r"premium of[\s\S]{0,180}?[0-9][0-9,]*(?:\.\d+)?\s*(?:%|per cent)[\s\S]{0,220}?conversion price"]},
    {"key": "conversion.period", "label": "conversion/exercise period", "patterns": [r"\b(?:Conversion Period|Exercise of Stock Acquisition Rights|Exercise Period)\b[\s\S]{0,700}?(?:\d{1,2}\s+[A-Z][a-z]+,?\s+20\d{2}|[A-Z][a-z]+\s+\d{1,2},\s+20\d{2})"]},
    {"key": "conversion.fixed_exchange_rate", "label": "fixed exchange rate", "patterns": [r"\b(?:Fixed Exchange Rate|fixed exchange rate|Initial [A-Z]{3}/[A-Z]{3} Exchange Rate)\b[\s\S]{0,260}?[0-9][0-9,]*(?:\.\d+)?\s*=\s*(?:US\$|HK\$|NT\$|S\$|¥|JPY|RMB|CNH|EUR|USD|HKD|TWD|SGD)?\s*1(?:\.00)?"]},
    {"key": "calls", "label": "issuer call / tax call / cleanup call", "patterns": [r"\b(?:Optional Redemption|Clean-up Call|Tax Redemption|issuer may.*redeem|Company may.*redeem|call option)\b[\s\S]{0,700}?(?:[0-9][0-9,]*(?:\.\d+)?\s*(?:%|per cent)|Closing Price|principal amount)"]},
    {"key": "puts", "label": "investor put / change-of-control / delisting put", "patterns": [r"\b(?:Put Option|Change of Control|Delisting|Relevant Event|Bondholders may require|holder.*require.*redeem)\b[\s\S]{0,700}?(?:[0-9][0-9,]*(?:\.\d+)?\s*(?:%|per cent)|principal amount)"]},
    {"key": "dividend_protection", "label": "dividend protection / adjustment", "patterns": [r"\b(?:Dividend|Extraordinary Dividend|adjustment to the Conversion Price|Conversion Price shall be adjusted)\b[\s\S]{0,700}?(?:Conversion Price|dividend|adjusted)"]},
    {"key": "quote_convention", "label": "quote / accrued interest convention", "patterns": [r"\b(?:clean price|dirty price|accrued interest|without accrued interest|together with accrued interest)\b[\s\S]{0,260}"]},
)


def build_term_evidence(contract: dict[str, Any], extraction: ExtractionResult) -> dict[str, list[dict[str, Any]]]:
    """Return best page/snippet matches for key modeled terms."""

    evidence: dict[str, list[dict[str, Any]]] = {}
    pages = extraction.pages or _pages_from_marked_text(extraction.text)
    for field in REQUIRED_EVIDENCE_FIELDS:
        value = _get(contract, field)
        candidates = _field_patterns(field, value, contract)
        matches: list[dict[str, Any]] = []
        for page in pages:
            for pattern, confidence, label in candidates:
                if not pattern:
                    continue
                match = re.search(pattern, page.text, flags=re.IGNORECASE | re.DOTALL)
                if match:
                    matches.append(
                        {
                            "page": page.page_number,
                            "field": field,
                            "confidence": confidence,
                            "match_type": label,
                            "snippet": _snippet(page.text, match.start(), match.end()),
                        }
                    )
                    break
        if matches:
            evidence[field] = sorted(matches, key=lambda item: (-float(item["confidence"]), int(item["page"])))[:3]
    return evidence


def build_targeted_term_candidates(contract: dict[str, Any], extraction: ExtractionResult) -> dict[str, list[dict[str, Any]]]:
    """Search every standard CB term target and return candidate snippets.

    This is separate from normalized scalar extraction.  It is allowed to find
    ambiguous candidate snippets for human review, but it must not convert those
    snippets into model inputs unless the scalar parser has explicit evidence.
    """

    pages = extraction.pages or _pages_from_marked_text(extraction.text)
    series_label = _series_label_for_matching(contract)
    results: dict[str, list[dict[str, Any]]] = {}
    for target in TERM_TARGETS:
        key = str(target["key"])
        hits: list[dict[str, Any]] = []
        for page in pages:
            for pattern in target["patterns"]:
                for match in re.finditer(pattern, page.text, flags=re.IGNORECASE | re.DOTALL):
                    snippet = _snippet(page.text, match.start(), match.end(), radius=160)
                    confidence = 0.65
                    if series_label and re.search(re.escape(series_label) + r"\s+Bonds", snippet, flags=re.IGNORECASE):
                        confidence += 0.2
                    if _value_for_target_is_present(contract, key, snippet):
                        confidence += 0.1
                    hits.append(
                        {
                            "page": page.page_number,
                            "term_key": key,
                            "label": target["label"],
                            "confidence": round(min(confidence, 0.95), 2),
                            "match_type": "targeted-term-search",
                            "snippet": snippet,
                        }
                    )
                    if len(hits) >= 8:
                        break
                if len(hits) >= 8:
                    break
            if len(hits) >= 8:
                break
        if hits:
            deduped = _dedupe_hits(hits)
            results[key] = sorted(deduped, key=lambda item: (-float(item["confidence"]), int(item["page"])))[:4]
    return results


def attach_source_evidence(contract: dict[str, Any], extraction: ExtractionResult) -> dict[str, Any]:
    """Return a contract copy enriched with term evidence and gap metadata."""

    enriched = deepcopy(contract)
    source_review = enriched.setdefault("source_review", {})
    existing_evidence = dict(source_review.get("term_evidence") or {})
    term_evidence = build_term_evidence(enriched, extraction)
    term_evidence = {**existing_evidence, **term_evidence}
    missing = missing_required_evidence_fields(term_evidence)
    approval_missing = missing_approval_evidence_fields(term_evidence, enriched)
    source_review["term_evidence"] = term_evidence
    source_review["targeted_term_candidates"] = build_targeted_term_candidates(enriched, extraction)
    source_review["term_target_catalog"] = [{"key": item["key"], "label": item["label"]} for item in TERM_TARGETS]
    source_review["required_evidence_fields"] = list(REQUIRED_EVIDENCE_FIELDS)
    source_review["approval_required_evidence_fields"] = approval_required_evidence_fields(enriched)
    source_review["missing_required_evidence"] = missing
    source_review["missing_approval_evidence"] = approval_missing
    source_review["evidence_status"] = "complete" if not missing else "incomplete"
    source_review["approval_evidence_status"] = "complete" if not approval_missing else "incomplete"
    source_review["review_status"] = (
        "evidence_collected_needs_human_review" if not approval_missing else "needs_human_review"
    )
    # Evidence collection is not human approval.  Keep the model-facing status conservative.
    if enriched.get("status") == "reviewed" and missing:
        enriched["status"] = "needs_review"
    elif enriched.get("status") != "reviewed":
        enriched["status"] = "needs_review"
    return enriched


def evidence_summary(contract: dict[str, Any]) -> dict[str, Any]:
    source_review = contract.get("source_review") or {}
    term_evidence = source_review.get("term_evidence") or {}
    missing = missing_required_evidence_fields(term_evidence)
    approval_fields = approval_required_evidence_fields(contract)
    approval_missing = missing_approval_evidence_fields(term_evidence, contract)
    return {
        "required_fields": len(REQUIRED_EVIDENCE_FIELDS),
        "covered_required_fields": len(REQUIRED_EVIDENCE_FIELDS) - len(missing),
        "missing_required_fields": missing,
        "evidence_status": "complete" if not missing else "incomplete",
        "approval_required_fields": len(approval_fields),
        "covered_approval_fields": len(approval_fields) - len(approval_missing),
        "approval_missing_fields": approval_missing,
        "approval_evidence_status": "complete" if not approval_missing else "incomplete",
    }


def approval_required_evidence_fields(contract: dict[str, Any] | None) -> list[str]:
    """Return evidence fields that block approval for pricing.

    REQUIRED_EVIDENCE_FIELDS is the broader audit checklist shown in review reports.
    The approval gate is narrower: it blocks fields required by the current
    valuation/market-join path and any optional clauses that the draft actually
    modeled. Metadata-only fields and missing relative-date prose stay visible as
    audit gaps but do not prevent a PM from approving a pricing-ready contract.
    """

    raw = contract if isinstance(contract, dict) else {}
    fields = list(BASE_APPROVAL_EVIDENCE_FIELDS)
    if _get(raw, "bond.currency") != _get(raw, "bond.stock_currency"):
        fields.append("conversion.fixed_exchange_rate")
    calls = raw.get("calls")
    if isinstance(calls, list) and calls:
        first = calls[0]
        if isinstance(first, dict) and first.get("trigger_ratio") not in (None, ""):
            fields.append("calls[0].trigger_ratio")
    return fields


def missing_approval_evidence_fields(term_evidence: Any, contract: dict[str, Any] | None) -> list[str]:
    if not isinstance(term_evidence, dict):
        term_evidence = {}
    return [field for field in approval_required_evidence_fields(contract) if not has_valid_page_evidence(term_evidence.get(field))]


def missing_required_evidence_fields(term_evidence: Any) -> list[str]:
    """Return required evidence fields without at least one usable page citation.

    Evidence must be more than a present key. Approval is allowed only when each
    required modeled term has a non-empty list containing page-level provenance
    with page number, snippet, and match type. This keeps malformed or empty
    evidence maps fail-closed while preserving warnings for normal draft review.
    """

    if not isinstance(term_evidence, dict):
        term_evidence = {}
    return [field for field in REQUIRED_EVIDENCE_FIELDS if not has_valid_page_evidence(term_evidence.get(field))]


def has_valid_page_evidence(entries: Any) -> bool:
    if not isinstance(entries, list) or not entries:
        return False
    return any(_is_valid_page_evidence_entry(entry) for entry in entries)


def _is_valid_page_evidence_entry(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    page = entry.get("page")
    try:
        if int(page) < 1:
            return False
    except Exception:
        return False
    snippet = str(entry.get("snippet") or "").strip()
    match_type = str(entry.get("match_type") or "").strip()
    return bool(snippet and match_type)


def _field_patterns(field: str, value: Any, contract: dict[str, Any]) -> list[tuple[str, float, str]]:
    if value in (None, "", "needs_review"):
        return []
    if field == "instrument.canonical_id":
        return [(re.escape(str(value)), 0.98, "isin-exact")]
    if field == "issuer.name":
        return [(re.escape(str(value)), 0.95, "issuer-name")]
    if field == "bond.description":
        return [(r"\s+".join(re.escape(part) for part in str(value).split()), 0.85, "description-exact")]
    if field in {"bond.issue_size", "bond.denomination"}:
        ccy = _get(contract, "bond.currency")
        return [(_money_value_pattern(value, ccy), 0.92, field.rsplit(".", 1)[-1])]
    if field == "bond.issue_price":
        return [(_percent_value_pattern(value), 0.92, "issue-price")]
    if field in {"bond.closing_date", "bond.maturity_date", "conversion.start_date", "conversion.end_date"}:
        return [(_date_value_pattern(value), 0.9, field.rsplit(".", 1)[-1])]
    if field == "redemption.maturity_price":
        return [(_percent_value_pattern(value), 0.88, "maturity-redemption")]
    if field == "conversion.underlying_ticker":
        code = str(value).split()[0]
        return [(rf"\b{re.escape(code)}\b", 0.82, "underlying-ticker")]
    if field == "conversion.initial_conversion_price":
        ccy = _get(contract, "bond.stock_currency")
        return [(_money_value_pattern(value, ccy), 0.92, "conversion-price")]
    if field == "conversion.conversion_premium":
        return [(_percent_value_pattern(value), 0.86, "conversion-premium")]
    if field == "conversion.fixed_exchange_rate":
        return [(_numeric_value_pattern(value), 0.85, "fixed-fx")]
    if field == "calls[0].trigger_ratio":
        try:
            pct = float(value) * 100.0
        except Exception:
            pct = None
        patterns = []
        if pct is not None:
            pct_pattern = _percent_value_pattern(pct)
            patterns.append(
                r"(?:at least|not less than)\s+"
                + pct_pattern
                + r"[\s\S]{0,220}?Conversion Price"
            )
            patterns.append(
                r"closing\s+price[\s\S]{0,220}?"
                + pct_pattern
                + r"\s+of\s+the\s+Conversion Price"
            )
        patterns.append(r"soft\s+call[\s\S]{0,300}?" + _numeric_value_pattern(value))
        return [(r"(?:" + "|".join(patterns) + r")", 0.75, "soft-call-trigger")]
    return [(re.escape(str(value)), 0.6, "exact-value")]


def _money_value_pattern(value: Any, currency: Any) -> str:
    number = _numeric_value_pattern(value)
    ccy = str(currency or "")
    token_patterns = {
        "USD": r"(?:US\$|USD|U\.S\.\s*Dollar[s]?)",
        "HKD": r"(?:HK\$|HKD|Hong\s*Kong\s*Dollar[s]?)",
        "TWD": r"(?:NT\$|NTD|TWD|NT\s*Dollar[s]?)",
        "CNH": r"(?:RMB|CNH|Renminbi)",
        "JPY": r"(?:¥|JPY|Yen)",
        "SGD": r"(?:S\$|SGD|Singapore\s*Dollar[s]?)",
        "EUR": r"(?:€|EUR|Euro[s]?)",
    }
    prefix = token_patterns.get(ccy, re.escape(ccy) if ccy else r"(?:US\$|HK\$|NT\$|S\$|¥|JPY|RMB|CNH|EUR|USD|HKD|TWD|SGD)?")
    return prefix + r"\s*" + number


def _percent_value_pattern(value: Any) -> str:
    return _numeric_value_pattern(value) + r"\s*(?:%|per\s+cent)?"


def _numeric_value_pattern(value: Any) -> str:
    try:
        number = float(str(value).replace(",", ""))
    except ValueError:
        return re.escape(str(value))
    variants = {f"{number:g}", f"{number:,.0f}", f"{number:,.1f}", f"{number:,.2f}", f"{number:,.3f}", str(value)}
    variants = {re.escape(item).replace(",", r",?") for item in variants if item and item != "0.00"}
    return "(?:" + "|".join(sorted(variants, key=len, reverse=True)) + ")"


def _date_value_pattern(value: Any) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.strptime(str(value), "%Y-%m-%d")
    except ValueError:
        return re.escape(str(value))
    variants = {
        f"{parsed.day} {parsed.strftime('%B')} {parsed.year}",
        f"{parsed.day} {parsed.strftime('%b')} {parsed.year}",
        f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}",
        f"{parsed.strftime('%b')} {parsed.day}, {parsed.year}",
    }
    return r"(?:" + "|".join(re.escape(item) for item in sorted(variants, key=len, reverse=True)) + r")"


def _series_label_for_matching(contract: dict[str, Any]) -> str:
    explicit = _get(contract, "instrument.series_label")
    if explicit:
        return str(explicit)
    maturity_year = _get(contract, "instrument.maturity_year") or str(_get(contract, "bond.maturity_date") or "")[:4]
    return str(maturity_year or "")


def _value_for_target_is_present(contract: dict[str, Any], target_key: str, snippet: str) -> bool:
    mapped = {
        "instrument.canonical_id": "instrument.canonical_id",
        "issuer.name": "issuer.name",
        "issuer.ticker": "conversion.underlying_ticker",
        "bond.issue_size": "bond.issue_size",
        "bond.denomination": "bond.denomination",
        "bond.issue_price": "bond.issue_price",
        "bond.coupon_rate": "bond.coupon_rate",
        "bond.closing_date": "bond.closing_date",
        "bond.maturity_date": "bond.maturity_date",
        "redemption.maturity_price": "redemption.maturity_price",
        "conversion.initial_conversion_price": "conversion.initial_conversion_price",
        "conversion.conversion_premium": "conversion.conversion_premium",
        "conversion.fixed_exchange_rate": "conversion.fixed_exchange_rate",
    }.get(target_key)
    value = _get(contract, mapped) if mapped else None
    if value in (None, "", "needs_review"):
        return False
    if mapped and mapped.endswith("date"):
        pattern = _date_value_pattern(value)
    elif mapped and ("price" in mapped or "size" in mapped or "denomination" in mapped or "rate" in mapped):
        pattern = _numeric_value_pattern(value)
    else:
        pattern = re.escape(str(value).split()[0] if target_key == "issuer.ticker" else str(value))
    return bool(re.search(pattern, snippet, flags=re.IGNORECASE))


def _dedupe_hits(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for hit in hits:
        key = (str(hit.get("page")), str(hit.get("snippet"))[:220])
        if key in seen:
            continue
        seen.add(key)
        out.append(hit)
    return out


def _snippet(text: str, start: int, end: int, *, radius: int = 110) -> str:
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    snippet = " ".join(text[left:right].split())
    if left > 0:
        snippet = "…" + snippet
    if right < len(text):
        snippet = snippet + "…"
    return snippet


def _pages_from_marked_text(text: str) -> list[PageText]:
    if not text.strip():
        return []
    parts = re.split(r"\n\n--- Page (\d+) ---\n", text)
    if len(parts) <= 1:
        return [PageText(1, text)]
    pages: list[PageText] = []
    index = 1
    while index + 1 < len(parts):
        page_number = int(parts[index])
        page_text = parts[index + 1]
        pages.append(PageText(page_number, page_text))
        index += 2
    return pages


def _get(raw: dict[str, Any], dotted: str | None) -> Any:
    if not dotted:
        return None
    current: Any = raw
    tokens = re.findall(r"[^.\[\]]+|\[\d+\]", dotted)
    for token in tokens:
        if token.startswith("["):
            if not isinstance(current, list):
                return None
            index = int(token.strip("[]"))
            if index >= len(current):
                return None
            current = current[index]
        else:
            if not isinstance(current, dict):
                return None
            current = current.get(token)
    return current
