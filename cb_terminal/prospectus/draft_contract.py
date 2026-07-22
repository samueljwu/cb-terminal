"""Draft normalized contract JSON from prospectus text.

Runtime drafting is issuer-neutral. It recognizes common CB term patterns and
leaves unsupported terms blank or needs_review until a reviewer checks the
source snippets and pages. Curated samples are fixture/backfill inputs only.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from cb_terminal.io.instrument_registry import cb_display_name
from cb_terminal.prospectus.schema import required_review_items, required_term_keys


MONTH_TOKEN = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
DATE_RE = re.compile(
    rf"(\b\d{{1,2}}(?:st|nd|rd|th)?\s+{MONTH_TOKEN},?\s+20\d{{2}}\b|\b{MONTH_TOKEN}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+20\d{{2}}\b)",
    flags=re.IGNORECASE,
)


def draft_contract_from_text(text: str, *, source_file: str = "") -> dict[str, Any]:
    drafts = draft_contracts_from_text(text, source_file=source_file)
    if not drafts:
        raise ValueError("unsupported prospectus text; no conservative draft template matched")
    return drafts[0]


def draft_contracts_from_text(text: str, *, source_file: str = "") -> list[dict[str, Any]]:
    scoped_text, scope_pages = _select_term_scope(text)
    generics = _draft_generic_contracts(
        scoped_text,
        full_text=text,
        source_file=source_file,
        scope_pages=scope_pages,
    )
    if generics:
        return generics
    return []


def _first_float_after(text: str, prefix_regex: str) -> float | None:
    match = re.search(prefix_regex + r"([0-9][0-9,]*(?:\.[0-9]+)?)", text, flags=re.IGNORECASE)
    if not match:
        return None
    return float(match.group(1).replace(",", ""))


def _parse_long_date(value: str) -> str:
    clean = " ".join(value.replace(",", " ").split()).title()
    clean = re.sub(r"(?<=\d)(?:St|Nd|Rd|Th)\b", "", clean)
    for fmt in ("%d %B %Y", "%B %d %Y", "%d %b %Y", "%b %d %Y"):
        try:
            return datetime.strptime(clean, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"unsupported date format: {value}")


def _select_term_scope(text: str) -> tuple[str, list[int]]:
    """Keep summary/cover pages ahead of boilerplate and detailed conditions.

    Short term sheets normally place the complete economic summary in their
    first pages.  Long offering circulars repeat dates and prices hundreds of
    pages later, often in definitions or underwriting sections.  Ranking early
    summary pages prevents a valid but economically unrelated scalar from
    winning merely because it appears first after whitespace flattening.
    """

    parts = re.split(r"\n+\s*--- Page (\d+) ---\s*\n", text)
    if len(parts) <= 1:
        return text, []
    pages: list[tuple[int, str]] = []
    index = 1
    while index + 1 < len(parts):
        pages.append((int(parts[index]), parts[index + 1]))
        index += 2
    if not pages:
        return text, []

    page_count = max(number for number, _ in pages)
    if page_count <= 90:
        selected = [(number, body) for number, body in pages if number <= 8]
    else:
        early = [(number, body) for number, body in pages if number <= 30]
        scored: list[tuple[int, int, str]] = []
        for number, body in early:
            lower = body.lower()
            score = 0
            score += 40 if re.search(r"\bsummary\s+(?:terms|of the offering)\b", lower) else 0
            score += 35 if re.search(r"(?m)^\s*the offering\s*$", lower) else 0
            score += 18 if "convertible bonds due" in lower else 0
            score += 5 * len(re.findall(r"\b(?:maturity date|issue price|offer price|initial conversion price|initial conversion prices|denomination|closing date)\b", lower))
            if score:
                scored.append((score, number, body))
        best_numbers = {number for _, number, _ in sorted(scored, reverse=True)[:8]}
        selected = [(number, body) for number, body in early if number in best_numbers]
        if not selected:
            selected = early[:8]

    selected.sort(key=lambda item: item[0])
    scoped = "".join(f"\n\n--- Page {number} ---\n{body}" for number, body in selected)
    return scoped, [number for number, _ in selected]


def _draft_generic_contracts(
    raw_text: str,
    *,
    full_text: str | None = None,
    source_file: str = "",
    scope_pages: list[int] | None = None,
) -> list[dict[str, Any]]:
    text = " ".join(raw_text.split())
    full_normalized = " ".join((full_text or raw_text).split())
    lower = text.lower()
    is_exchangeable = "exchangeable bond" in lower or "exchangeable bonds" in lower
    if "convertible bond" not in lower and "convertible bonds" not in lower and not is_exchangeable:
        return []
    table_rows = _extract_terms_table_rows(raw_text)
    issuer = _extract_issuer_name(text, table_rows=table_rows)
    guarantor = _extract_guarantor_name(text, table_rows=table_rows)
    default_conversion_price, default_stock_currency = _extract_conversion_price(text, table_rows=table_rows)
    settlement_currency_hint = _extract_settlement_currency(text, None, table_rows=table_rows)
    fixed_fx = fx_units = fx_convention = None
    underlying = _extract_underlying_ticker(text)
    if underlying == "needs_review":
        underlying = _extract_underlying_ticker(full_normalized)
    series = _extract_series_terms(text, table_rows=table_rows)
    if not series:
        maturity_date = _extract_maturity_date(text, table_rows=table_rows)
        issue_size, bond_currency = _extract_issue_size(text, table_rows=table_rows)
        if maturity_date and issue_size and bond_currency:
            series = [{"maturity_date": maturity_date, "issue_size": issue_size, "bond_currency": bond_currency, "series_label": maturity_date[:4]}]
    if not (issuer and series):
        return []
    drafts: list[dict[str, Any]] = []
    for item in series:
        maturity_date = str(item["maturity_date"])
        issue_size = float(item["issue_size"])
        bond_currency = str(item["bond_currency"])
        series_label = str(item.get("series_label") or maturity_date[:4])
        term_series_label = series_label if len(series) > 1 else None
        conversion_price, stock_currency = _extract_conversion_price(
            text,
            series_label=term_series_label,
            table_rows=table_rows,
        )
        if conversion_price is None:
            conversion_price = default_conversion_price
        if stock_currency is None:
            stock_currency = default_stock_currency
        settlement_currency = settlement_currency_hint or _extract_settlement_currency(text, bond_currency, table_rows=table_rows)
        fixed_fx, fx_units, fx_convention = _extract_fixed_fx(text, bond_currency, stock_currency)
        settlement_fx, settlement_fx_units = _extract_initial_settlement_fx(text, table_rows=table_rows)
        reference_share_price = _extract_reference_share_price(text, table_rows=table_rows)
        conversion_premium = _extract_conversion_premium(text, series_label=term_series_label, table_rows=table_rows)
        premium_derived = False
        if conversion_premium is None and conversion_price and reference_share_price:
            derived = (float(conversion_price) / float(reference_share_price) - 1.0) * 100.0
            if -50.0 < derived < 300.0:
                conversion_premium = round(derived, 6)
                premium_derived = True
        economic_currency = _extract_economic_currency(
            text,
            series_label=term_series_label,
            bond_currency=bond_currency,
            stock_currency=stock_currency,
            table_rows=table_rows,
        )
        maturity_year = maturity_date[:4]
        issuer_short_name = _issuer_short_name(issuer)
        pricing_date = _extract_pricing_date(text, table_rows=table_rows)
        closing_date = _extract_closing_date(text, table_rows=table_rows) or _infer_issue_date_from_maturity_span(text, maturity_date)
        conversion_windows = _extract_disjoint_conversion_windows(text, closing_date, maturity_date)
        if conversion_windows:
            conversion_start = conversion_windows[0]["start_date"]
            conversion_end = conversion_windows[-1]["end_date"]
        else:
            conversion_start = (
                _extract_conversion_start_date(text, series_label=term_series_label)
                or _infer_controlling_conversion_start_date(full_normalized, closing_date)
                or _infer_relative_conversion_start_date(text, closing_date)
            )
            conversion_end = _extract_conversion_end_date(text, series_label=term_series_label) or _infer_relative_conversion_end_date(text, maturity_date)
        conversion_start_rule = "multiple_disjoint_conversion_windows" if conversion_windows else _extract_conversion_start_rule(full_normalized)
        conversion_end_rule = _extract_conversion_end_rule(text)
        conditional_early_conversion = _extract_conditional_early_conversion(text, closing_date)
        conversion_calendar_status = ""
        if conversion_end_rule and "conditional" in conversion_end_rule:
            conversion_calendar_status = "conditional_cutoff_and_weekday_approximation_require_contract_calendar"
        elif conversion_end_rule and any(token in conversion_end_rule for token in ("working_days_before_maturity", "trading_days_before_maturity")):
            conversion_calendar_status = "weekday_only_approximation_requires_contract_calendar"
        series_suffix = "" if series_label == maturity_year else f" {series_label}"
        contract_id = _slug(f"{issuer}{series_suffix} {maturity_year} cb")
        isin = _extract_series_isin(full_normalized, series_label=series_label)
        if not isin and len(series) == 1:
            isin = _extract_single_security_isin(full_normalized)
        if isin:
            contract_id = f"{isin}_contract"
        coupon_row = _table_value(table_rows, "Coupon")
        zero_coupon = bool(
            "zero coupon" in lower
            or re.search(r"\bCoupon\s*:?\s*(?:Zero|0+(?:\.0+)?\s*%)", text, flags=re.IGNORECASE)
            or re.search(r"^(?:Zero|0+(?:\.0+)?\s*%)\b", coupon_row, flags=re.IGNORECASE)
        )
        draft = {
        "id": contract_id,
        "instrument": {
            "canonical_id_type": "ISIN" if isin else "PENDING_ISIN",
            "canonical_id": isin or "",
            "display_name": cb_display_name(issuer_short_name, maturity_date, 0.0 if zero_coupon else 0.0) + (f" {series_label}" if series_label != maturity_year else ""),
            "issuer_legal_name": issuer,
            "issuer_short_name": issuer_short_name,
            "maturity_year": int(maturity_year),
            "series_label": series_label if series_label != maturity_year else "",
            "deal_names": _extract_deal_names(source_file),
            "aliases": [f"{issuer_short_name} {series_label} CB" if series_label != maturity_year else f"{issuer_short_name} {maturity_year} CB"],
            "structure_type": "exchangeable_bond" if is_exchangeable else "convertible_bond",
        },
        "source_file": source_file,
        "source_type": "automated_prospectus_draft",
        "status": "needs_review",
        "issuer": {"name": issuer, "ticker": underlying},
        "guarantor": {"name": guarantor} if guarantor else None,
        "exchangeable_terms": _extract_exchangeable_terms(text, table_rows=table_rows) if is_exchangeable else None,
        "bond": {
            "description": f"{_display_currency_token(bond_currency)}{issue_size:,.0f} {series_label + ' ' if series_label != maturity_year else ''}{'Zero Coupon ' if zero_coupon else ''}{'Exchangeable' if is_exchangeable else 'Convertible'} Bonds due {maturity_year}",
            "currency": bond_currency,
            "economic_currency": economic_currency or bond_currency,
            "settlement_currency": settlement_currency or bond_currency,
            "stock_currency": stock_currency or bond_currency,
            "denomination": _extract_denomination(text, table_rows=table_rows),
            "denomination_increment": _extract_denomination_increment(text, table_rows=table_rows),
            "pricing_face": 100.0,
            "issue_size": issue_size,
            "issue_price": _extract_issue_price(text, series_label=term_series_label, table_rows=table_rows),
            "investor_offer_price": _extract_investor_offer_price(text, series_label=term_series_label, table_rows=table_rows),
            "coupon_rate": 0.0 if zero_coupon else None,
            "coupon_frequency": 0 if zero_coupon else None,
            "pricing_date": pricing_date,
            "closing_date": closing_date,
            "maturity_date": maturity_date,
            "day_count": "needs_review",
        },
        "redemption": {"maturity_price": _extract_maturity_price(text, series_label=term_series_label, table_rows=table_rows)},
        "conversion": {
            "underlying_ticker": underlying,
            "initial_conversion_price": conversion_price,
            "reference_share_price": reference_share_price,
            "conversion_premium": conversion_premium,
            "conversion_premium_source": "derived_from_reference_share_price" if premium_derived else "stated",
            "fixed_exchange_rate": fixed_fx,
            "fixed_exchange_rate_units": fx_units,
            "initial_settlement_exchange_rate": settlement_fx,
            "initial_settlement_exchange_rate_units": settlement_fx_units,
            "start_date": conversion_start,
            "start_date_rule": conversion_start_rule,
            "end_date": conversion_end,
            "end_date_rule": conversion_end_rule,
            "calendar_status": conversion_calendar_status,
            "windows": conversion_windows,
            "conditional_early_start_date": conditional_early_conversion.get("start_date"),
            "conditional_early_start_rule": conditional_early_conversion.get("rule"),
            "conditional_early_conditions": conditional_early_conversion.get("conditions", []),
            "restricted_periods": "needs_review",
        },
        "calls": _extract_soft_calls(text, series_label=term_series_label, closing_date=closing_date),
        "puts": (
            _extract_scheduled_puts(text, series_label=term_series_label, table_rows=table_rows)
            + _extract_event_puts(text)
        ),
        "source_review": {
            "created_from": "automated_text_extraction_generic_template",
            "review_status": "needs_human_review",
            "required_term_keys": required_term_keys(),
            "review_items": required_review_items(),
            "term_scope_pages": list(scope_pages or []),
            "term_scope_policy": "summary/cover pages ranked before detailed conditions",
        },
        "model_notes": [
            "Automated generic draft; every extracted scalar must be checked against source_review.term_evidence before use.",
            "Call/put schedules are inferred only when the parser finds explicit dates and triggers; otherwise review them manually.",
        ],
        }
        _attach_draft_evidence(draft, text, series_label=str(item.get("series_label") or maturity_year) if len(series) > 1 else None)
        drafts.append(draft)
    return drafts


TABLE_TERM_LABELS: tuple[str, ...] = (
    "Issuer",
    "Guarantor",
    "Guarantee",
    "Offering",
    "Securities Offered",
    "Currency",
    "Denomination",
    "Maturity Date",
    "Maturity Date and Final Redemption",
    "Pricing Date",
    "Trade Date",
    "Pricing / Trade Date",
    "Closing Date",
    "Closing / Issue Date",
    "Issue / Closing Date",
    "Closing / Settlement Date",
    "Issue / Closing / Settlement Date",
    "Settlement Date",
    "Issue Price",
    "Offer Price",
    "Deal Size",
    "Issue Size",
    "Offer Size",
    "Coupon",
    "Yield to Put / Maturity",
    "Issue / Put / Maturity Price",
    "Redemption Price at Maturity",
    "Redemption Price",
    "Initial Conversion Premium",
    "Conversion Premium",
    "Initial Exchange Premium",
    "Exchange Premium",
    "Reference Share Price",
    "Reference H Share Price",
    "Initial Conversion Price",
    "Initial Conversion Prices",
    "Initial Exchange Price",
    "Initial Exchange Ratio",
    "Bondholder Put Date",
    "Bondholder's Put Option Date",
    "Bondholders' Put Option Date",
    "Investor Put Date",
    "Investor Put Option",
    "Put Price",
    "Investor Put Price",
    "Conversion Period",
    "Exchange Period",
    "Exercise of Stock Acquisition Rights",
    "Security Codes",
    "Initial USD/CNH Exchange Rate",
    "Fixed Exchange Rate",
)


def _extract_terms_table_rows(text: str) -> dict[str, str]:
    """Return label/value pairs from summary term tables preserved in text.

    PDF text layers often flatten tables into alternating label/value runs rather
    than clean CSV rows.  This helper is intentionally broad and issuer-neutral:
    it searches for a catalog of standard convertible-bond term labels, then
    takes the bounded text up to the next standard label.  Scalar parsers can use
    these row values first, but still require normal evidence snippets before a
    draft can be approved.
    """

    labels = sorted(TABLE_TERM_LABELS, key=len, reverse=True)

    def flexible_label(label: str) -> str:
        return r"\s+".join(re.escape(token) for token in label.split())

    label_pattern = r"(?:" + "|".join(flexible_label(label) for label in labels) + r")"
    # Anchoring to line starts avoids matching labels inside values such as
    # "Currency-Linked Zero Coupon Convertible Bonds". PDF table labels may
    # wrap across lines, so whitespace between label words remains flexible.
    matches = list(
        re.finditer(
            rf"(?mi)^[ \t]*({label_pattern})[ \t]*(?::[ \t]*|\.{{2,}}[ \t]*|(?=\r?$))",
            text,
        )
    )
    if not matches:
        # Copied text and fixtures sometimes place several labels on one line.
        # A mandatory colon keeps that fallback bounded and conservative.
        matches = list(re.finditer(rf"(?i)(?:^|\s)({label_pattern})\s*:\s*", text))
    rows: dict[str, str] = {}
    for index, match in enumerate(matches):
        raw_label = " ".join(match.group(1).split())
        canonical = next((label for label in TABLE_TERM_LABELS if label.lower() == raw_label.lower()), raw_label)
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else min(len(text), start + 700)
        value = " ".join(text[start:end].split()).strip(" :-–—\t")
        if value and canonical not in rows:
            rows[canonical] = value
    return rows


def _table_value(table_rows: dict[str, str] | None, *labels: str) -> str:
    if not table_rows:
        return ""
    for label in labels:
        value = table_rows.get(label)
        if value:
            return value
    return ""


def _series_column_index(series_label: str | None) -> int | None:
    match = re.fullmatch(r"Series\s+([A-Z])", str(series_label or "").strip(), flags=re.IGNORECASE)
    return ord(match.group(1).upper()) - ord("A") if match else None


def _select_series_item(items: list[Any], series_label: str | None) -> Any:
    if not items:
        return None
    index = _series_column_index(series_label)
    if index is not None and index < len(items):
        return items[index]
    return items[0]


def _extract_series_terms(text: str, *, table_rows: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Return per-series issue size/maturity rows explicitly visible in text."""
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, float]] = set()
    pattern = (
        _money_pattern()
        + r"\s+(?:Currency-Linked\s+)?(?:Zero Coupon\s+)?(?:U\.S\. Dollar Settled\s+)?Convertible Bonds due\s+(20\d{2})"
        + r"(?:\s*\((?:the\s+)?[“\"]?([^”\")]+? Bonds)[”\"]?\))?"
    )
    for match in re.finditer(pattern, text, flags=re.IGNORECASE):
        token, number, scale, year, inline_label = match.groups()
        tail = text[match.end() : match.end() + 140]
        label_match = re.match(
            r"\s*\([^)]*?[\"“”']?((?:Series\s+[A-Z0-9]+)|(?:20\d{2}))\s+Bonds\b",
            tail,
            flags=re.IGNORECASE,
        )
        label = inline_label or (label_match.group(1) if label_match else None)
        issue_size = _scaled_money_parts(number, scale)
        bond_currency = _currency_from_token(token)
        maturity_date = _maturity_date_for_series(text, year)
        if not maturity_date:
            fallback_maturity = _extract_maturity_date(text, table_rows=table_rows)
            if fallback_maturity and fallback_maturity.startswith(year):
                maturity_date = fallback_maturity
        if not maturity_date:
            continue
        series_label = _normalize_series_label(label) if label else year
        key = (series_label, bond_currency, issue_size)
        if key in seen:
            continue
        rows.append({"series_label": series_label, "maturity_date": maturity_date, "issue_size": issue_size, "bond_currency": bond_currency})
        seen.add(key)
    if len(rows) == 1 and str(rows[0]["maturity_date"]).endswith("-12-31"):
        exact_maturity = _extract_maturity_date(text)
        if exact_maturity:
            rows[0]["maturity_date"] = exact_maturity
            rows[0]["series_label"] = exact_maturity[:4]
    return rows


def _normalize_series_label(label: str | None) -> str:
    if not label:
        return ""
    clean = " ".join(label.replace("“", "").replace("”", "").replace('"', "").split())
    clean = re.sub(r"\s+Bonds?$", "", clean, flags=re.IGNORECASE).strip()
    return clean.title() if re.match(r"series\s+[a-z]$", clean, flags=re.IGNORECASE) else clean


def _maturity_date_for_series(text: str, year: str) -> str | None:
    patterns = [
        r"(?:on|and)\s+(" + DATE_RE.pattern.strip("()") + rf")\s+in the case\s+of\s+the\s+{re.escape(year)}\s+Bonds",
        rf"{re.escape(year)}\s+Bonds[^.]{{0,200}}?redeemed[^.]{{0,200}}?(?:on|and)\s+(" + DATE_RE.pattern.strip("()") + r")",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return _parse_long_date(match.group(1))
    return None


def _attach_draft_evidence(contract: dict[str, Any], text: str, *, series_label: str | None = None) -> None:
    source_review = contract.setdefault("source_review", {})
    evidence: dict[str, list[dict[str, Any]]] = {}
    field_patterns: dict[str, list[tuple[str, str]]] = {
        "issuer.name": [(re.escape(str(contract.get("issuer", {}).get("name") or "")), "issuer-name")],
        "bond.issue_size": [(_money_value_pattern(contract.get("bond", {}).get("issue_size"), contract.get("bond", {}).get("currency")), "issue-size")],
        "bond.currency": [(_currency_value_pattern(contract.get("bond", {}).get("currency")), "bond-currency")],
        "bond.maturity_date": [(_date_value_pattern(contract.get("bond", {}).get("maturity_date"), series_label), "maturity-date")],
        "conversion.initial_conversion_price": [(_money_value_pattern(contract.get("conversion", {}).get("initial_conversion_price"), contract.get("bond", {}).get("stock_currency")), "conversion-price")],
    }
    premium = contract.get("conversion", {}).get("conversion_premium")
    if premium not in (None, ""):
        field_patterns["conversion.conversion_premium"] = [(_numeric_value_pattern(premium), "conversion-premium")]
    fx_value = contract.get("conversion", {}).get("fixed_exchange_rate")
    if fx_value not in (None, ""):
        field_patterns["conversion.fixed_exchange_rate"] = [(_numeric_value_pattern(fx_value), "fixed-fx")]
    denom = contract.get("bond", {}).get("denomination")
    if denom not in (None, ""):
        field_patterns["bond.denomination"] = [(_money_value_pattern(denom, contract.get("bond", {}).get("currency")), "denomination")]
    issue_price = contract.get("bond", {}).get("issue_price")
    if issue_price not in (None, ""):
        field_patterns["bond.issue_price"] = [(_numeric_value_pattern(issue_price), "issue-price")]
    maturity_price = contract.get("redemption", {}).get("maturity_price")
    if maturity_price not in (None, ""):
        field_patterns["redemption.maturity_price"] = [(_numeric_value_pattern(maturity_price), "maturity-redemption")]
    ticker = contract.get("conversion", {}).get("underlying_ticker")
    if ticker and ticker != "needs_review":
        field_patterns["conversion.underlying_ticker"] = [(re.escape(str(ticker).split()[0]), "underlying-ticker")]
    isin = contract.get("instrument", {}).get("canonical_id")
    if isin:
        field_patterns["instrument.canonical_id"] = [(re.escape(str(isin)), "isin")]
    start_date = contract.get("conversion", {}).get("start_date")
    if start_date not in (None, ""):
        field_patterns["conversion.start_date"] = [(_date_value_pattern(start_date), "conversion-start-date"), (r"(?:Conversion|Exchange) Period[\s\S]{0,700}?(?:day after the Issue Date|\d+(?:st|nd|rd|th)\s+day\s+(?:from|following)\s+the\s+(?:Closing|Issue) Date)", "conversion-start-relative")]
    end_date = contract.get("conversion", {}).get("end_date")
    if end_date not in (None, ""):
        field_patterns["conversion.end_date"] = [(_date_value_pattern(end_date), "conversion-end-date"), (r"(?:Conversion|Exchange) Period[\s\S]{0,900}?(?:\d+(?:st|nd|rd|th)?\s+(?:Trading\s+)?day\s+(?:prior\s+to|immediately preceding)\s+(?:the\s+)?Maturity Date|\d+\s+working\s+days?\s+prior\s+to\s+(?:the\s+)?Maturity Date|\d+\s+days?\s+(?:prior\s+to|immediately preceding)\s+(?:the\s+)?Maturity Date)", "conversion-end-relative")]
    for field, patterns in field_patterns.items():
        for pattern, label in patterns:
            if not pattern:
                continue
            match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
            if match:
                evidence[field] = [{
                    "page": 1,
                    "field": field,
                    "confidence": 0.9,
                    "match_type": label,
                    "snippet": _snippet(text, match.start(), match.end()),
                }]
                break
    source_review["term_evidence"] = evidence
    source_review["evidence_policy"] = "extracted scalar fields require raw text snippet evidence; missing fields remain needs_review, not invented"


def _currency_value_pattern(currency: Any) -> str:
    ccy = str(currency or "").upper()
    token_patterns = {
        "USD": r"(?:US\$|USD|U\.S\.\s+Dollar|United States Dollar)",
        "HKD": r"(?:HK\$|HKD|Hong Kong Dollars?)",
        "TWD": r"(?:NT\$|NTD|TWD|New Taiwan Dollars?)",
        "CNH": r"(?:RMB|CNH|Renminbi|RMB denominated|CNH linked)",
        "JPY": r"(?:¥|JPY|Japanese Yen|Yen)",
        "SGD": r"(?:S\$|SGD|Singapore Dollars?)",
        "EUR": r"(?:EUR|Euro)",
    }
    return token_patterns.get(ccy, re.escape(ccy))


def _money_value_pattern(value: Any, currency: Any) -> str:
    if value in (None, ""):
        return ""
    ccy = str(currency or "")
    token_patterns = {"USD": r"(?:US\$|USD)", "HKD": r"(?:HK\$|HKD)", "TWD": r"(?:NT\$|NTD|TWD)", "CNH": r"(?:RMB|CNH)", "JPY": r"(?:¥|JPY)"}
    prefix = token_patterns.get(ccy, re.escape(ccy))
    try:
        number = float(value)
    except Exception:
        return prefix + r"\s*" + _numeric_value_pattern(value)
    scaled_variants: list[str] = []
    if abs(number) >= 1_000_000_000 and number % 1_000_000_000 == 0:
        scaled_variants.append(rf"{number / 1_000_000_000:g}\s*billion")
        scaled_variants.append(rf"{number / 1_000_000_000:,.0f}\s*billion")
    if abs(number) >= 1_000_000 and number % 1_000_000 == 0:
        scaled_variants.append(rf"{number / 1_000_000:g}\s*million")
        scaled_variants.append(rf"{number / 1_000_000:,.0f}\s*million")
    numeric = _numeric_value_pattern(value)
    if scaled_variants:
        numeric = r"(?:" + numeric + r"|" + r"|".join(scaled_variants) + r")"
    return prefix + r"\s*" + numeric


def _numeric_value_pattern(value: Any) -> str:
    try:
        number = float(value)
    except Exception:
        return re.escape(str(value))
    variants = {f"{number:g}", f"{number:,.0f}", f"{number:,.1f}", f"{number:,.2f}", f"{number:,.3f}", str(value)}
    return r"(?:" + "|".join(re.escape(v).replace(",", r",?") for v in sorted(variants, key=len, reverse=True) if v) + r")"


def _date_value_pattern(value: Any, series_label: str | None = None) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.strptime(str(value), "%Y-%m-%d")
    except ValueError:
        return re.escape(str(value))
    day_first = f"{parsed.day} {parsed.strftime('%B')} {parsed.year}"
    month_first = f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"
    base = rf"(?:{re.escape(day_first)}|{re.escape(month_first)})"
    if series_label:
        return base
    return base


def _snippet(text: str, start: int, end: int, *, radius: int = 120) -> str:
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    out = " ".join(text[left:right].split())
    if left > 0:
        out = "…" + out
    if right < len(text):
        out += "…"
    return out


def _extract_issuer_name(text: str, *, table_rows: dict[str, str] | None = None) -> str | None:
    clean_text = re.sub(r"---\s*Page\s+\d+\s*---", " ", text).strip()

    issuer_row = _table_value(table_rows, "Issuer")
    if issuer_row:
        candidate = re.split(
            r"\s+\((?:the\s+[\"“']?Issuer|the\s+[\"“']?Company|Stock\s+Code:|Bloomberg\s+ticker:)",
            issuer_row,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        candidate = _clean_issuer_candidate(candidate)
        if _issuer_name_is_plausible(candidate):
            return candidate

    terms_issuer = re.search(
        r"SUMMARY TERMS.{0,2200}?\bIssuer\s+(.{1,180}?)(?:\s+\(the|\s+\(Stock Code:|\s+Securities Offered|\s+Currency\b)",
        clean_text[:30000],
        flags=re.IGNORECASE,
    )
    if terms_issuer:
        candidate = _clean_issuer_candidate(terms_issuer.group(1))
        if _issuer_name_is_plausible(candidate):
            return candidate

    first_cb = re.search(r"Convertible Bonds due", clean_text, flags=re.IGNORECASE)
    if first_cb:
        window = clean_text[max(0, first_cb.start() - 800) : first_cb.start()]
        issuer_candidates = re.findall(
            r"([A-Z][A-Za-z0-9 .,&'’()-]+?(?:Corporation|Co\., Ltd\.|Company Limited|Group Limited|Limited|Ltd\.?))\s*(?:\(|$)",
            window,
        )
        if issuer_candidates:
            candidate = issuer_candidates[-1]
            parts = re.split(r"OFFERING\s+CIRCULAR|STRICTLY\s+CONFIDENTIAL", candidate, flags=re.IGNORECASE)
            candidate = parts[-1].strip() if len(parts) > 1 else candidate.strip()
            candidate = _clean_issuer_candidate(candidate)
            if _issuer_name_is_plausible(candidate):
                return candidate

    after_page = re.search(
        r"(?:STRICTLY CONFIDENTIAL\s+)?([A-Z][A-Za-z0-9 .,&'’()-]+?(?:Corporation|Co\., Ltd\.|Company Limited|Group Limited|Limited|Ltd\.?))\s*(?:\(|(?:US\$|HK\$|NT\$|S\$|¥|JPY|RMB|CNH|EUR|USD|HKD|TWD|SGD)\s*[0-9])",
        clean_text[:12000],
    )
    if after_page:
        candidate = after_page.group(1)
        parts = re.split(r"OFFERING\s+CIRCULAR|STRICTLY\s+CONFIDENTIAL", candidate, flags=re.IGNORECASE)
        candidate = parts[-1].strip() if len(parts) > 1 else candidate.strip()
        candidate = _clean_issuer_candidate(candidate)
        if _issuer_name_is_plausible(candidate):
            return candidate
    issuer_label = re.search(
        r"\bIssuer\s+(.{1,220}?)(?:\s+\(the [^)]*\))?\s+(?:\(Stock Code:|Securities Offered|Currency\b|Form\b|Ranking\b)",
        clean_text[:20000],
        flags=re.IGNORECASE,
    )
    if issuer_label:
        candidate = _clean_issuer_candidate(issuer_label.group(1))
        if _issuer_name_is_plausible(candidate):
            return candidate
    before = re.split(r"(?:US\$|HK\$|NT\$|S\$|¥|JPY|RMB|CNH|EUR|USD|HKD|TWD|SGD)\s*[0-9]", clean_text, maxsplit=1)[0]
    lines = [line.strip() for line in before.splitlines() if line.strip() and not line.strip().startswith("--- Page")]
    if lines:
        candidate = _clean_issuer_candidate(lines[0])
        if _issuer_name_is_plausible(candidate):
            return candidate
    compact = " ".join(before.split()).strip()
    if compact:
        candidate = _clean_issuer_candidate(compact)
        if _issuer_name_is_plausible(candidate):
            return candidate
    match = re.match(r"\s*([A-Z][A-Za-z0-9 .,&'-]+?)\s+(?:US\$|HK\$|NT\$|S\$|¥|JPY|RMB|CNH|EUR|USD|HKD|TWD|SGD)", clean_text)
    candidate = _clean_issuer_candidate(match.group(1)) if match else ""
    return candidate if _issuer_name_is_plausible(candidate) else None


def _extract_guarantor_name(text: str, *, table_rows: dict[str, str] | None = None) -> str | None:
    row_value = _table_value(table_rows, "Guarantor")
    if row_value:
        candidate = re.split(
            r"\s+\((?:Class|Stock Code:|Ticker:|the\s+[\"“']?Guarantor)",
            row_value,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        candidate = _clean_issuer_candidate(candidate)
        if _issuer_name_is_plausible(candidate):
            return candidate
    match = re.search(
        r"\bGuarantor\s*:?[ \t]*(.{1,160}?)(?=\s+(?:Guarantee|Offering|Status|Currency|Securities Offered)\b)",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        candidate = _clean_issuer_candidate(match.group(1))
        if _issuer_name_is_plausible(candidate):
            return candidate
    return None


def _clean_issuer_candidate(value: str) -> str:
    candidate = " ".join((value or "").replace("\u00a0", " ").split()).strip(" :-–—")
    candidate = re.sub(r"^\.+\s*", "", candidate)
    candidate = re.sub(r"\s*,?\s+(?:listed|incorporated)\s+(?:under|on|in|as)\b.*$", "", candidate, flags=re.IGNORECASE)
    candidate = candidate.strip(" ,:;")
    candidate = re.sub(r"\b(Corporation|Limited|Company)\.$", r"\1", candidate, flags=re.IGNORECASE)
    return candidate


def _issuer_name_is_plausible(value: str | None) -> bool:
    candidate = " ".join((value or "").split())
    if not (3 <= len(candidate) <= 140):
        return False
    blocked = (
        "denomination",
        "use of proceeds",
        "securities offered",
        "important notice",
        "offering circular",
        "not to be forwarded",
    )
    lower = candidate.lower()
    if any(token in lower for token in blocked):
        return False
    return bool(re.search(r"[A-Za-z]", candidate))


def _money_pattern() -> str:
    return r"(US\$|HK\$|NT\$|S\$|¥|JPY|RMB|CNH|CNY|EUR|USD|HKD|TWD|SGD)\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(billion|million|bn|mm|mn|b|m)?"


def _display_currency_token(currency: str | None) -> str:
    return {
        "USD": "US$",
        "HKD": "HK$",
        "TWD": "NT$",
        "JPY": "JPY ",
        "CNH": "CNH ",
        "CNY": "CNY ",
        "SGD": "S$",
        "EUR": "EUR ",
    }.get(str(currency or ""), str(currency or ""))


def _extract_issue_size(text: str, *, table_rows: dict[str, str] | None = None) -> tuple[float, str] | tuple[None, None]:
    row_value = _table_value(table_rows, "Deal Size", "Issue Size", "Offer Size", "Securities Offered")
    if row_value:
        row_match = re.search(_money_pattern(), row_value, flags=re.IGNORECASE)
        if row_match:
            token, number, scale = row_match.groups()[-3], row_match.groups()[-2], row_match.groups()[-1]
            return _scaled_money_parts(number, scale), _currency_from_token(token)
    patterns = [
        r"(?:Offer Size|Deal Size|Issue Size)\s+" + _money_pattern(),
        _money_pattern() + r"\s+(?:Currency-Linked\s+)?(?:Zero Coupon\s+)?(?:U\.S\. Dollar Settled\s+)?Convertible Bonds due",
        r"offering\s+" + _money_pattern() + r"\s+aggregate principal amount",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            groups = match.groups()
            token, number, scale = groups[-3], groups[-2], groups[-1]
            value = float(number.replace(",", ""))
            if scale and scale.lower().startswith("b"):
                value *= 1_000_000_000
            elif scale and scale.lower().startswith("m"):
                value *= 1_000_000
            return value, _currency_from_token(token)
    return None, None


def _extract_conversion_price(
    text: str,
    series_label: str | None = None,
    *,
    table_rows: dict[str, str] | None = None,
) -> tuple[float, str] | tuple[None, None]:
    money = _money_pattern()
    row_value = _table_value(
        table_rows,
        "Initial Conversion Price",
        "Initial Conversion Prices",
        "Initial Exchange Price",
    )
    if row_value:
        if series_label:
            label = re.escape(series_label)
            explicit_patterns = [
                rf"{money}[^.;]{{0,100}}?(?:for|in the case of)\s+(?:the\s+)?{label}\s+Bonds?",
                rf"{label}\s+Bonds?\s*:?\s*{money}",
            ]
            for pattern in explicit_patterns:
                match = re.search(pattern, row_value, flags=re.IGNORECASE)
                if match:
                    groups = match.groups()
                    token, number = groups[-3], groups[-2]
                    return float(number.replace(",", "")), _currency_from_token(token)
        row_matches = list(re.finditer(money, row_value, flags=re.IGNORECASE))
        selected = _select_series_item(row_matches, series_label)
        if selected is not None:
            token, number, _ = selected.groups()[-3:]
            return float(number.replace(",", "")), _currency_from_token(token)
    if series_label:
        label = re.escape(series_label)
        series_patterns = [
            rf"{label}\s+Bonds[^.\n]{{0,900}}?at\s+an?\s+initial conversion price[^.\n]{{0,180}}?(?:of\s*)?{money}\s+per\s+Share",
            rf"{label}\s+Bonds[^.\n]{{0,900}}?at\s+a\s+conversion price[^.\n]{{0,180}}?(?:of\s*)?{money}\s+per\s+Share",
            rf"at\s+an?\s+initial conversion price[^.\n]{{0,180}}?(?:of\s*)?{money}\s+per\s+Share[^.\n]{{0,420}}?{label}\s+Bonds",
            rf"at\s+a\s+conversion price[^.\n]{{0,180}}?(?:of\s*)?{money}\s+per\s+Share[^.\n]{{0,420}}?{label}\s+Bonds",
            rf"(?:initial\s+)?conversion price[^.\n]{{0,260}}?{money}\s+per\s+Share[^.\n]{{0,420}}?in\s+the\s+case\s+of\s+the\s+{label}\s+Bonds",
            rf"{label}\s+Bonds[^.\n]{{0,900}}?(?:initial\s+)?Conversion Price(?:\s+is|\s+which will initially be)?\s*{money}",
        ]
        for pattern in series_patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                groups = match.groups()
                token, number = groups[-3], groups[-2]
                return float(number.replace(",", "")), _currency_from_token(token)
    patterns = [
        r"(?:initial\s+)?Conversion Price(?:\s+is|\s+which will initially be)?\s*" + money,
        r"initial conversion price[^.;]{0,220}?of\s*" + money,
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            groups = match.groups()
            token, number = groups[-3], groups[-2]
            return float(number.replace(",", "")), _currency_from_token(token)
    return None, None


def _extract_conversion_premium(
    text: str,
    series_label: str | None = None,
    *,
    table_rows: dict[str, str] | None = None,
) -> float | None:
    row_value = _table_value(
        table_rows,
        "Initial Conversion Premium",
        "Conversion Premium",
        "Initial Exchange Premium",
        "Exchange Premium",
    )
    if row_value:
        percentages = [
            float(match.group(1).replace(",", ""))
            for match in re.finditer(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:%|per cent)", row_value, flags=re.IGNORECASE)
        ]
        selected = _select_series_item(percentages, series_label)
        if selected is not None:
            return float(selected)
    patterns: list[str] = []
    if series_label:
        label = re.escape(series_label)
        patterns.extend([
            rf"conversion premium[\s\S]{{0,240}}?([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:%|per cent)[\s\S]{{0,320}}?{label}\s+Bonds",
            rf"{label}\s+Bonds[\s\S]{{0,420}}?conversion premium[\s\S]{{0,240}}?([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:%|per cent)",
        ])
    patterns.extend([
        r"(?:conversion|exchange) premium[^.]{0,240}?([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:%|per cent)",
        r"premium of[^.]{0,120}?([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:%|per cent)[^.]{0,160}?(?:conversion|exchange) price",
    ])
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return float(match.group(1).replace(",", ""))
    return None


def _extract_reference_share_price(text: str, *, table_rows: dict[str, str] | None = None) -> float | None:
    row_value = _table_value(table_rows, "Reference Share Price", "Reference H Share Price")
    search_text = row_value or text
    patterns = [
        r"(?:Reference(?:\s+H)?\s+Share\s+Price)\s*:?\s*" + _money_pattern(),
        r"(?:placement price|closing price of the Shares)[^.;]{0,100}?" + _money_pattern(),
        r"closing sale price[^.;]{0,180}?\bwas\s+" + _money_pattern(),
    ]
    for pattern in patterns:
        match = re.search(pattern, search_text, flags=re.IGNORECASE)
        if match:
            number = match.groups()[-2]
            return float(number.replace(",", ""))
    if row_value:
        match = re.search(_money_pattern(), row_value, flags=re.IGNORECASE)
        if match:
            return float(match.groups()[-2].replace(",", ""))
    return None


def _extract_fixed_fx(text: str, bond_currency: str | None, stock_currency: str | None) -> tuple[float | None, str | None, str | None]:
    match = re.search(
        r"(?:fixed exchange rate(?: is)?(?: of)?|Fixed Exchange Rate)\s*:?\s*(US\$|HK\$|NT\$|S\$|¥|JPY|RMB|CNH|EUR|USD|HKD|TWD|SGD)\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:=|/)\s*(US\$|HK\$|NT\$|S\$|¥|JPY|RMB|CNH|EUR|USD|HKD|TWD|SGD)\s*1(?:\.0+)?",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None, None, None
    left_ccy = _currency_from_token(match.group(1))
    right_ccy = _currency_from_token(match.group(3))
    rate = float(match.group(2).replace(",", ""))
    units = f"{left_ccy} per {right_ccy}"
    convention = None
    if stock_currency and bond_currency:
        if left_ccy == stock_currency and right_ccy == bond_currency:
            convention = "STOCK_PER_CB"
        elif left_ccy == bond_currency and right_ccy == stock_currency:
            if rate > 0:
                return 1.0 / rate, f"{stock_currency} per {bond_currency}", "STOCK_PER_CB"
            convention = "CB_PER_STOCK"
    return rate, units, convention


def _extract_initial_settlement_fx(
    text: str,
    *,
    table_rows: dict[str, str] | None = None,
) -> tuple[float | None, str | None]:
    row_value = _table_value(table_rows, "Initial USD/CNH Exchange Rate")
    if not row_value:
        return None, None
    search_text = row_value
    token = r"(US\$|HK\$|NT\$|S\$|¥|JPY|RMB|CNH|CNY|EUR|USD|HKD|TWD|SGD)"
    match = re.search(
        token + r"\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*=\s*" + token + r"\s*1(?:\.0+)?",
        search_text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None, None
    left = _currency_from_token(match.group(1))
    right = _currency_from_token(match.group(3))
    return float(match.group(2).replace(",", "")), f"{left} per {right}"


def _extract_maturity_date(text: str, *, table_rows: dict[str, str] | None = None) -> str | None:
    row_value = _table_value(table_rows, "Maturity Date", "Maturity Date and Final Redemption")
    if row_value:
        row_patterns = [
            r"\bon(?:\s+or\s+about)?\s+(" + DATE_RE.pattern.strip("()") + r")",
            r"^(?:On\s+or\s+about\s+)?(" + DATE_RE.pattern.strip("()") + r")",
            r"mature[^.]{0,180}?(" + DATE_RE.pattern.strip("()") + r")",
            r"redeem[^.]{0,180}?(" + DATE_RE.pattern.strip("()") + r")",
        ]
        for pattern in row_patterns:
            row_match = re.search(pattern, row_value, flags=re.IGNORECASE)
            if row_match:
                return _parse_long_date(row_match.group(1))
    patterns = [
        r"mature on(?: or about)?\s+(" + DATE_RE.pattern.strip("()") + r")",
        r"Maturity Date(?:\s+and\s+Final\s+Redemption)?\s*:?\s+(?:On or about\s+)?(" + DATE_RE.pattern.strip("()") + r")",
        r"redeemed at [0-9.]+%?[^.]{0,120}? on (" + DATE_RE.pattern.strip("()") + r")",
        r"Convertible Bonds due\s+(20\d{2})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            value = match.group(1)
            if re.fullmatch(r"20\d{2}", value):
                return None
            return _parse_long_date(value)
    return None


def _extract_conversion_start_date(text: str, series_label: str | None = None) -> str | None:
    if series_label:
        label = re.escape(series_label)
        patterns = [
            r"exercisable\s+from,?\s+and\s+including,?\s+(" + DATE_RE.pattern.strip("()") + rf")[^.\n]{{0,500}}?in\s+the\s+case\s+of\s+the\s+{label}\s+Bonds",
            rf"{label}\s+Bonds[^.\n]{{0,500}}?(?:from and including|from,?\s+and\s+including,?|commence on)\s+(" + DATE_RE.pattern.strip("()") + r")",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match and match.groups():
                return _parse_long_date(match.group(1))
    semantic_match = re.search(
        r"(?:Bonds?\s+(?:will\s+be|are)\s+convertible|Conversion Rights?\s+may\s+be\s+exercised)[\s\S]{0,260}?"
        r"(?:during\s+the\s+period\s+)?from,?\s+and\s+including\s+(" + DATE_RE.pattern.strip("()") + r")",
        text,
        flags=re.IGNORECASE,
    )
    if semantic_match:
        return _parse_long_date(semantic_match.group(1))
    patterns = [
        r"(?:from and including|from,?\s+and\s+including,?|commence on)\s+(" + DATE_RE.pattern.strip("()") + r")",
        r"(?:Conversion|Exchange) Period\s+(?:Convertible|Exchangeable) at (?:the option of the Bondholders thereof, )?at any time on or after(?: the day after)? the Issue Date",
    ]
    conversion_window = _conversion_period_window(text)
    for pattern in patterns:
        match = re.search(pattern, conversion_window, flags=re.IGNORECASE)
        if match and match.groups():
            return _parse_long_date(match.group(1))
    return None


def _extract_conversion_end_date(text: str, series_label: str | None = None) -> str | None:
    if series_label:
        label = re.escape(series_label)
        date_pat = DATE_RE.pattern.strip("()")
        patterns = [
            # Prefer explicit table/prose pairs: "May 21, 2029, in the case of the 2029 Bonds".
            rf"(?:to,?\s+and\s+including,?\s+)?({date_pat})\s*,?\s+in\s+the\s+case\s+of\s+the\s+{label}\s+Bonds",
            rf"{label}\s+Bonds[^.\n]{{0,500}}?(?:to and including|to,?\s+and\s+including,?|end on)\s+({date_pat})",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match and match.groups():
                return _parse_long_date(match.group(1))
    semantic_match = re.search(
        r"(?:Bonds?\s+(?:will\s+be|are)\s+convertible|Conversion Rights?\s+may\s+be\s+exercised)[\s\S]{0,500}?"
        r"(?:during\s+the\s+period\s+)?from,?\s+and\s+including\s+"
        + DATE_RE.pattern
        + r"[\s\S]{0,180}?to,?\s+and\s+including\s+("
        + DATE_RE.pattern.strip("()")
        + r")",
        text,
        flags=re.IGNORECASE,
    )
    if semantic_match:
        return _parse_long_date(semantic_match.group(2))
    patterns = [
        r"(?:to and including|to,?\s+and\s+including,?|end on)\s+(" + DATE_RE.pattern.strip("()") + r")",
        r"until\s+10\s+working\s+days\s+prior\s+to\s+Maturity Date",
    ]
    conversion_window = _conversion_period_window(text)
    for pattern in patterns:
        match = re.search(pattern, conversion_window, flags=re.IGNORECASE)
        if match and match.groups():
            return _parse_long_date(match.group(1))
    return None


def _extract_conversion_start_rule(text: str) -> str:
    """Keep the contractual date rule alongside the resolved calendar date.

    A resolved date is convenient for the lattice, but investors also need to
    know whether the source was inclusive, exclusive, or relative to closing.
    """

    formal_clause = r"Conversion Right attaching to any Bond may be exercised[\s\S]{0,900}?at any time\s+"
    if re.search(formal_clause + r"on or after the Issue Date", text, flags=re.IGNORECASE):
        return "on_or_after_issue_date"
    if re.search(formal_clause + r"after the Issue Date", text, flags=re.IGNORECASE):
        return "after_issue_date"
    window = _conversion_period_window(text)
    if re.search(
        r"next day immediately after the end of a\s+(\d+|one|two|three|six)[- ]month period following the Closing Date",
        window,
        flags=re.IGNORECASE,
    ):
        match = re.search(
            r"next day immediately after the end of a\s+(\d+|one|two|three|six)[- ]month period following the Closing Date",
            window,
            flags=re.IGNORECASE,
        )
        token = match.group(1).lower() if match else ""
        count = {"one": 1, "two": 2, "three": 3, "six": 6}.get(token, int(token) if token.isdigit() else 0)
        return f"day_after_{count}_calendar_months_following_closing"
    direct_month_match = re.search(
        r"next day immediately after\s+(\d+|one|two|three|six)\s+months?\s+from the Closing Date",
        window,
        flags=re.IGNORECASE,
    )
    if direct_month_match:
        token = direct_month_match.group(1).lower()
        count = {"one": 1, "two": 2, "three": 3, "six": 6}.get(token, int(token) if token.isdigit() else 0)
        return f"day_after_{count}_calendar_months_following_closing"
    match = re.search(
        r"on or after the\s+(\d+)(?:st|nd|rd|th)\s+day\s+(?:from|after|following)\s+the\s+(?:Closing|Issue) Date",
        window,
        flags=re.IGNORECASE,
    )
    if match:
        return f"day_{int(match.group(1))}_from_closing_or_issue"
    anniversary_match = re.search(
        r"at any time after the\s+(\d+)(?:st|nd|rd|th)\s+anniversary of the\s+(?:Settlement|Closing|Issue) Date",
        window,
        flags=re.IGNORECASE,
    )
    if anniversary_match:
        return f"after_{int(anniversary_match.group(1))}_year_anniversary_of_settlement"
    if re.search(r"day after the Issue Date|at any time after (?:the )?(?:Closing|Issue) Date", window, flags=re.IGNORECASE):
        return "day_after_closing_or_issue"
    if _extract_conversion_start_date(text):
        return "explicit_calendar_date"
    return ""


def _extract_conversion_end_rule(text: str) -> str:
    """Return a normalized contractual conversion-end rule when available."""

    window = _conversion_period_window(text)
    trading_match = re.search(
        r"(\d+)(?:st|nd|rd|th)?\s+Trading Days?\s+immediately preceding\s+the\s+Maturity Date",
        window,
        flags=re.IGNORECASE,
    )
    if trading_match:
        calendar_alternative = re.search(
            r"where\s+the\s+Share Redemption Option is exercised[\s\S]{0,160}?(\d+)\s+days?\s+immediately preceding\s+the\s+Maturity Date",
            window,
            flags=re.IGNORECASE,
        )
        if calendar_alternative:
            return (
                f"conditional_{int(trading_match.group(1))}_trading_days_before_maturity_"
                f"or_{int(calendar_alternative.group(1))}_calendar_days_before_maturity_on_share_redemption"
            )
        return f"{int(trading_match.group(1))}_trading_days_before_maturity"
    match = re.search(r"(\d+)\s+working\s+days?(?:\s*\([^)]*\))?\s+prior\s+to\s+(?:the\s+)?Maturity Date", window, flags=re.IGNORECASE)
    if match:
        return f"{int(match.group(1))}_working_days_before_maturity"
    match = re.search(r"(\d+)(?:st|nd|rd|th)?\s+day\s+prior\s+to\s+the\s+Maturity Date", window, flags=re.IGNORECASE)
    if not match:
        match = re.search(r"(\d+)\s+days?\s+prior\s+to\s+the\s+Maturity Date", window, flags=re.IGNORECASE)
    if match:
        return f"{int(match.group(1))}_calendar_days_before_maturity"
    if _extract_conversion_end_date(text):
        return "explicit_calendar_date"
    return ""


def _extract_disjoint_conversion_windows(
    text: str,
    closing_date: str | None,
    maturity_date: str | None,
) -> list[dict[str, str]]:
    """Extract explicitly numbered, non-contiguous conversion periods."""

    if not closing_date or not maturity_date:
        return []
    window = _conversion_period_window(text)
    if not re.search(r"First Conversion Period", window, flags=re.IGNORECASE) or not re.search(
        r"Second Conversion Period", window, flags=re.IGNORECASE
    ):
        return []
    first = re.search(
        r"First Conversion Period\s*:?\s*after\s+(?:the\s+)?(?:Closing|Issue|Settlement) Date[\s\S]{0,180}?until\s+(" + DATE_RE.pattern.strip("()") + r")",
        window,
        flags=re.IGNORECASE,
    )
    second = re.search(
        r"Second Conversion Period\s*:?\s*after\s+(" + DATE_RE.pattern.strip("()") + r")[\s\S]{0,260}?(\d+)\s+days?\s+prior\s+to\s+(?:the\s+)?Maturity Date",
        window,
        flags=re.IGNORECASE,
    )
    if not first or not second:
        return []
    try:
        closing = date.fromisoformat(closing_date)
        maturity = date.fromisoformat(maturity_date)
        first_end = date.fromisoformat(_parse_long_date(first.group(1)))
        second_start = date.fromisoformat(_parse_long_date(second.group(1))) + timedelta(days=1)
        second_end = maturity - timedelta(days=int(second.group(2)))
    except (ValueError, IndexError):
        return []
    first_start = closing + timedelta(days=1)
    if not (first_start <= first_end < second_start <= second_end):
        return []
    return [
        {
            "start_date": first_start.isoformat(),
            "end_date": first_end.isoformat(),
            "start_date_rule": "strictly_after_closing_date",
            "end_date_rule": "explicit_calendar_date",
        },
        {
            "start_date": second_start.isoformat(),
            "end_date": second_end.isoformat(),
            "start_date_rule": "strictly_after_explicit_calendar_date",
            "end_date_rule": f"{int(second.group(2))}_calendar_days_before_maturity",
        },
    ]


def _infer_issue_date_from_maturity_span(text: str, maturity_date: str | None) -> str | None:
    if not maturity_date:
        return None
    match = re.search(r"Maturity Date\s+On or about\s+" + DATE_RE.pattern.strip("()") + r"\s*\((\d+)\s+days?\s+from\s+the\s+Closing\s*/\s*Issue Date\)", text, flags=re.IGNORECASE)
    if not match:
        return None
    try:
        maturity = date.fromisoformat(maturity_date)
        return (maturity - timedelta(days=int(match.group(1)))).isoformat()
    except (ValueError, IndexError):
        return None


def _infer_relative_conversion_start_date(text: str, anchor_date: str | None) -> str | None:
    if not anchor_date:
        return None
    try:
        anchor = date.fromisoformat(anchor_date)
    except ValueError:
        return None
    window = _conversion_period_window(text)
    if not window:
        return None
    if re.search(r"(?:day after the Issue Date|at any time after (?:the )?(?:Closing|Issue) Date)", window, flags=re.IGNORECASE):
        return (anchor + timedelta(days=1)).isoformat()
    month_match = re.search(
        r"next day immediately after the end of a\s+(\d+|one|two|three|six)[- ]month period following the Closing Date",
        window,
        flags=re.IGNORECASE,
    )
    if month_match:
        month_token = month_match.group(1).lower()
        month_count = {"one": 1, "two": 2, "three": 3, "six": 6}.get(month_token, int(month_token) if month_token.isdigit() else 0)
        return (_add_calendar_months(anchor, month_count) + timedelta(days=1)).isoformat()
    direct_month_match = re.search(
        r"next day immediately after\s+(\d+|one|two|three|six)\s+months?\s+from the Closing Date",
        window,
        flags=re.IGNORECASE,
    )
    if direct_month_match:
        month_token = direct_month_match.group(1).lower()
        month_count = {"one": 1, "two": 2, "three": 3, "six": 6}.get(month_token, int(month_token) if month_token.isdigit() else 0)
        return (_add_calendar_months(anchor, month_count) + timedelta(days=1)).isoformat()
    anniversary_match = re.search(
        r"at any time after the\s+(\d+)(?:st|nd|rd|th)\s+anniversary of the\s+(?:Settlement|Closing|Issue) Date",
        window,
        flags=re.IGNORECASE,
    )
    if anniversary_match:
        return (
            _add_calendar_months(anchor, int(anniversary_match.group(1)) * 12)
            + timedelta(days=1)
        ).isoformat()
    match = re.search(
        r"on or after the\s+(\d+)(?:st|nd|rd|th)\s+day\s+(?:from|after|following)\s+the\s+(?:Closing|Issue) Date",
        window,
        flags=re.IGNORECASE,
    )
    if match:
        return (anchor + timedelta(days=int(match.group(1)))).isoformat()
    return None


def _add_calendar_months(anchor: date, months: int) -> date:
    total = anchor.year * 12 + anchor.month - 1 + months
    year, month_index = divmod(total, 12)
    month = month_index + 1
    day = min(anchor.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _extract_conditional_early_conversion(text: str, anchor_date: str | None) -> dict[str, Any]:
    if not anchor_date:
        return {}
    window = _conversion_period_window(text)
    match = re.search(
        r"at any time after\s+(\d+)\s+days?\s+after the\s+(?:Settlement|Closing|Issue) Date[\s\S]{0,700}?\bif\b",
        window,
        flags=re.IGNORECASE,
    )
    if not match:
        return {}
    try:
        anchor = date.fromisoformat(anchor_date)
    except ValueError:
        return {}
    conditions: list[str] = []
    conditional_window = window[match.start() : match.start() + 900]
    if re.search(r"tax call", conditional_window, flags=re.IGNORECASE):
        conditions.append("tax_call_notice")
    if re.search(r"clean[ -]?up call", conditional_window, flags=re.IGNORECASE):
        conditions.append("cleanup_call_notice")
    if re.search(r"Relevant Event", conditional_window, flags=re.IGNORECASE):
        conditions.append("relevant_event")
    days = int(match.group(1))
    return {
        "start_date": (anchor + timedelta(days=days + 1)).isoformat(),
        "rule": f"strictly_after_{days}_days_after_settlement_if_event_occurs",
        "conditions": conditions,
    }


def _infer_controlling_conversion_start_date(text: str, issue_date: str | None) -> str | None:
    if not issue_date:
        return None
    try:
        anchor = date.fromisoformat(issue_date)
    except ValueError:
        return None
    formal_clause = r"Conversion Right attaching to any Bond may be exercised[\s\S]{0,900}?at any time\s+"
    if re.search(formal_clause + r"on or after the Issue Date", text, flags=re.IGNORECASE):
        return anchor.isoformat()
    if re.search(formal_clause + r"after the Issue Date", text, flags=re.IGNORECASE):
        return (anchor + timedelta(days=1)).isoformat()
    condition_windows = re.findall(
        r"(?:Exercise of Conversion Rights|Conversion Rights may be exercised)([\s\S]{0,2400}?)(?:Conversion Price|Fractions of Shares|Procedure for Conversion|$)",
        text,
        flags=re.IGNORECASE,
    )
    for window in condition_windows:
        if re.search(r"\bat any time on or after the Issue Date\b", window, flags=re.IGNORECASE):
            return anchor.isoformat()
        if re.search(r"\bat any time after the Issue Date\b", window, flags=re.IGNORECASE):
            return (anchor + timedelta(days=1)).isoformat()
    return None


def _infer_relative_conversion_end_date(text: str, maturity_date: str | None) -> str | None:
    if not maturity_date:
        return None
    try:
        maturity = date.fromisoformat(maturity_date)
    except ValueError:
        return None
    window = _conversion_period_window(text)
    if not window:
        return None
    match = re.search(
        r"(\d+)(?:st|nd|rd|th)?\s+Trading Days?\s+immediately preceding\s+the\s+Maturity Date",
        window,
        flags=re.IGNORECASE,
    )
    business_days = bool(match)
    if not match:
        match = re.search(r"(\d+)(?:st|nd|rd|th)?\s+day\s+prior\s+to\s+the\s+Maturity Date", window, flags=re.IGNORECASE)
    if not match:
        match = re.search(r"(\d+)\s+working\s+days?(?:\s*\([^)]*\))?\s+prior\s+to\s+(?:the\s+)?Maturity Date", window, flags=re.IGNORECASE)
        business_days = bool(match)
    if not match:
        match = re.search(r"(\d+)\s+days?\s+prior\s+to\s+the\s+Maturity Date", window, flags=re.IGNORECASE)
    if match:
        days = int(match.group(1))
        if business_days:
            return _subtract_weekdays(maturity, days).isoformat()
        return (maturity - timedelta(days=days)).isoformat()
    return None


def _subtract_weekdays(anchor: date, days: int) -> date:
    current = anchor
    remaining = days
    while remaining > 0:
        current -= timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current


def _add_weekdays(anchor: date, days: int) -> date:
    current = anchor
    remaining = days
    while remaining > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current


def _conversion_period_window(text: str) -> str:
    starts = list(re.finditer(r"(?:Conversion|Exchange) Period\s*:?\s+", text, flags=re.IGNORECASE))
    if not starts:
        return ""
    windows: list[str] = []
    terminator = re.compile(
        r"(?:Conversion Right|Exchange Rights|Redemption at the Option|Issuer Call|Early Redemption Amount|Adjustments to (?:Conversion|Exchange)|Cash Election|Pricing\s*/\s*Trade Date|Security Codes?|Accrued Interest|--- Page)",
        flags=re.IGNORECASE,
    )
    for start in starts:
        body = text[start.end() : start.end() + 1400]
        end = terminator.search(body)
        if end:
            body = body[: end.start()]
        windows.append(" ".join(body.split()))
    for window in windows:
        if re.search(r"Maturity Date|Closing Date|Issue Date|working days? prior", window, flags=re.IGNORECASE):
            return window
    return windows[0]


def _extract_closing_date(text: str, *, table_rows: dict[str, str] | None = None) -> str | None:
    row_value = _table_value(
        table_rows,
        "Closing Date",
        "Closing / Issue Date",
        "Issue / Closing Date",
        "Closing / Settlement Date",
        "Issue / Closing / Settlement Date",
        "Settlement Date",
    )
    if row_value:
        match = re.search(DATE_RE.pattern, row_value, flags=re.IGNORECASE)
        if match:
            return _parse_long_date(match.group(1))
    patterns = [
        r"Closing / Settlement Date\s*:?\s*(?:Expected\s+)?(?:on or about\s+)?(" + DATE_RE.pattern.strip("()") + r")",
        r"Issue / Closing / Settlement Date\s*:?\s*(?:On or about\s+)?(" + DATE_RE.pattern.strip("()") + r")",
        r"Closing / Issue Date\s*:?\s*(?:On or about\s+)?(" + DATE_RE.pattern.strip("()") + r")",
        r"Issue / Closing Date\s*:?\s*(?:On or about\s+)?(" + DATE_RE.pattern.strip("()") + r")",
        r"Settlement Date\s*:?\s*(?:Expected\s+)?(?:On or about\s+)?(" + DATE_RE.pattern.strip("()") + r")",
        r"Closing Date\s*:?\s*(?:Expected\s+)?(?:on or about\s+)?(" + DATE_RE.pattern.strip("()") + r")",
        r"closing date (?:is |for [^,]+ is expected to take place by |is expected to be on |[^.]{0,60}?on or about )(" + DATE_RE.pattern.strip("()") + r")",
        r"Issue Date\s+(" + DATE_RE.pattern.strip("()") + r")",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return _parse_long_date(match.group(1))
    return None


def _extract_soft_calls(
    text: str,
    series_label: str | None = None,
    *,
    closing_date: str | None = None,
) -> list[dict[str, Any]]:
    """Extract generic issuer soft-call clauses with explicit date/trigger terms."""

    call_window = None
    if series_label and _series_column_index(series_label) is not None:
        label = re.escape(series_label)
        call_window = re.search(
            rf"Redemption at the Option of the Issuer for the {label} Bonds[\s\S]{{0,160}}?Issuer Call(?P<body>[\s\S]{{0,1800}}?)(?:Clean Up Call|Tax Call|Change of Control|Put Option|$)",
            text,
            flags=re.IGNORECASE,
        )
    if not call_window:
        call_candidates = list(re.finditer(
            r"Issuer Call(?P<body>[\s\S]{0,2200}?)(?:Clean[ -]?Up Call|Tax Call|Change of Control|Put Option|Adjustment upon|$)",
            text,
            flags=re.IGNORECASE,
        ))
        if call_candidates:
            call_window = max(
                call_candidates,
                key=lambda candidate: (
                    5 * bool(re.search(r"\bcallable\b", candidate.group("body"), flags=re.IGNORECASE))
                    + 4 * bool(re.search(r"Closing Price", candidate.group("body"), flags=re.IGNORECASE))
                    + 3 * bool(re.search(r"(?:Conversion|Exchange) Price|conversion ratio", candidate.group("body"), flags=re.IGNORECASE))
                    + len(candidate.group("body")) / 10000.0
                ),
            )
    if not call_window:
        call_window = re.search(
            r"(?:Optional Redemption|early redeemed at the Issuer[’']s option|(?:Company|Issuer)\s+may(?:,?\s+at\s+its\s+option,?)?\s+redeem)(?P<body>[\s\S]{0,1800})",
            text,
            flags=re.IGNORECASE,
        )
    if not call_window:
        return []
    body = " ".join(call_window.group("body").split())
    start_date = None
    start_match = None
    if series_label:
        label = re.escape(series_label)
        start_match = re.search(
            r"(?P<boundary>at any time after|at any time on or after|on or after|after)\s+("
            + DATE_RE.pattern.strip("()")
            + rf")[^.\n]{{0,180}}?in\s+the\s+case\s+of\s+the\s+{label}\s+Bonds",
            body,
            flags=re.IGNORECASE,
        )
    if not start_match:
        start_match = re.search(
            r"(?P<boundary>at any time after|at any time on or after|on or after|after)\s+(" + DATE_RE.pattern.strip("()") + r")",
            body,
            flags=re.IGNORECASE,
        )
    if start_match:
        start_date = _parse_long_date(start_match.group(2))
        boundary = str(start_match.groupdict().get("boundary") or "").lower()
        if "on or after" not in boundary and "after" in boundary:
            start_date = (date.fromisoformat(start_date) + timedelta(days=1)).isoformat()
    if start_date is None:
        preposed_patterns: list[str] = []
        if series_label:
            preposed_patterns.append(
                r"(?P<boundary>at any time after|at any time on or after|on or after|after)\s+("
                + DATE_RE.pattern.strip("()")
                + rf")\s*\(?\s*in the case of the\s+{re.escape(series_label)}\s+Bonds\)?[\s\S]{{0,320}}?(?:Company|Issuer)\s+may"
            )
        preposed_patterns.append(
            r"(?P<boundary>at any time after|at any time on or after|on or after|after)\s+("
            + DATE_RE.pattern.strip("()")
            + r")[\s\S]{0,320}?(?:Company|Issuer)\s+may"
        )
        for pattern in preposed_patterns:
            preposed = re.search(pattern, text, flags=re.IGNORECASE)
            if not preposed:
                continue
            start_date = _parse_long_date(preposed.group(2))
            boundary = str(preposed.groupdict().get("boundary") or "").lower()
            if "on or after" not in boundary and "after" in boundary:
                start_date = (date.fromisoformat(start_date) + timedelta(days=1)).isoformat()
            break
    relative_rule = None
    if start_date is None and closing_date:
        years_match = re.search(r"callable after\s+(\d+)\s+years?\s+from\s+(?:the\s+)?Closing Date", body, flags=re.IGNORECASE)
        if years_match:
            start_date = (_add_calendar_months(date.fromisoformat(closing_date), int(years_match.group(1)) * 12) + timedelta(days=1)).isoformat()
            relative_rule = years_match.group(0)
        else:
            months_match = re.search(
                r"callable(?:\s+at\s+any\s+time)?\s+after\s+(\d+)\s+months?\s+from\s+(?:the\s+)?(?:Closing|Settlement) Date",
                body,
                flags=re.IGNORECASE,
            )
            if months_match:
                start_date = (
                    _add_calendar_months(date.fromisoformat(closing_date), int(months_match.group(1)))
                    + timedelta(days=1)
                ).isoformat()
                relative_rule = months_match.group(0)
        if start_date is None:
            trading_days_match = re.search(
                r"callable(?:\s+at\s+any\s+time)?\s+after\s+(\d+)\s+Trading Days?(?:\s*\([^)]*\))?\s+(?:following|after)\s+(?:the\s+)?(?:Closing|Issue|Settlement) Date",
                body,
                flags=re.IGNORECASE,
            )
            if trading_days_match:
                # "After N Trading Days" means the next trading day, not the
                # next calendar day (which could otherwise resolve to Saturday).
                start_date = _add_weekdays(
                    date.fromisoformat(closing_date), int(trading_days_match.group(1)) + 1
                ).isoformat()
                relative_rule = trading_days_match.group(0)
    if start_date is None:
        relative_match = re.search(
            r"callable after the\s+\d+(?:st|nd|rd|th)\s+[^.]{0,120}?Business Day[^.]{0,120}?after the Issue Date",
            body,
            flags=re.IGNORECASE,
        )
        if relative_match:
            relative_rule = relative_match.group(0)
    trigger = None
    trigger_match = re.search(
        r"(?:at least|not less than)\s+([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:%|per\s+cent\.?)[\s\S]{0,180}?(?:Conversion|Exchange) Price",
        body,
        flags=re.IGNORECASE,
    )
    if not trigger_match:
        trigger_match = re.search(
            r"closing\s+price[\s\S]{0,220}?([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:%|per\s+cent\.?)\s+of\s+the\s+(?:Conversion|Exchange) Price",
            body,
            flags=re.IGNORECASE,
        )
    if not trigger_match:
        trigger_match = re.search(
            r"Closing Price[\s\S]{0,800}?at least\s+([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:%|per\s+cent\.?)",
            body,
            flags=re.IGNORECASE,
        )
    if trigger_match:
        trigger = float(trigger_match.group(1).replace(",", "")) / 100.0
    if trigger is None:
        return []
    call = {
        "model_type": "soft_call" if start_date else "soft_call_unresolved",
        "start_date": start_date,
        "price": 100.0,
        "trigger_ratio": trigger,
        "description": "Issuer soft call extracted from prospectus text; confirm notice period and observation window before final use.",
    }
    if relative_rule:
        call["start_date_rule"] = relative_rule
        if re.search(r"Trading Days?", relative_rule, flags=re.IGNORECASE):
            call["start_date_calendar_status"] = "weekday_only_approximation_requires_contract_calendar"
    if re.search(r"Early Redemption Amount", body, flags=re.IGNORECASE):
        call["price_rule"] = "early_redemption_amount"
        call["description"] = "Issuer soft call pays the accreting Early Redemption Amount; the current lattice does not model that dynamic call price."
    if re.search(r"Early Redemption Amount[\s\S]{0,180}?divided by (?:the )?(?:Conversion Ratio|conversion ratio)", body, flags=re.IGNORECASE):
        call["trigger_basis"] = "early_redemption_amount_divided_by_conversion_ratio"
    window_match = re.search(
        r"for any\s+(\d+)\s+[^.]{0,120}?within a period of\s+(\d+)\s+consecutive",
        body,
        flags=re.IGNORECASE,
    )
    if window_match:
        call["trigger_days"] = int(window_match.group(1))
        call["trigger_window_days"] = int(window_match.group(2))
        call["observation_rule"] = f"{call['trigger_days']}_of_{call['trigger_window_days']}_consecutive_trading_days"
    else:
        out_of_match = re.search(r"(?:for\s+(?:any\s+)?|period of\s+)(\d+)\s+out of\s+(\d+)\s+consecutive", body, flags=re.IGNORECASE)
        if out_of_match:
            call["trigger_days"] = int(out_of_match.group(1))
            call["trigger_window_days"] = int(out_of_match.group(2))
            call["observation_rule"] = f"{call['trigger_days']}_of_{call['trigger_window_days']}_consecutive_trading_days"
        else:
            consecutive_match = re.search(
                r"(?:for\s+each\s+of\s+|for\s+)?(?:the\s+)?(\d+)\s+consecutive\s+(?:Trading Days|Stock Exchange Business Days)",
                body,
                flags=re.IGNORECASE,
            )
            if consecutive_match:
                consecutive_days = int(consecutive_match.group(1))
                call["trigger_days"] = consecutive_days
                call["trigger_window_days"] = consecutive_days
                call["observation_rule"] = f"{consecutive_days}_consecutive_trading_days"
    last_observation_match = re.search(
        r"last\s+of\s+(?:which|such\s+[^.]{0,80}?days?)\s+(?:occurs?|shall\s+occur)\s+not\s+more\s+than\s+(\d+)\s+days?\s+prior\s+to[\s\S]{0,100}?notice",
        body,
        flags=re.IGNORECASE,
    )
    if last_observation_match:
        call["last_observation_max_days_before_notice"] = int(last_observation_match.group(1))
    elif re.search(r"immediately prior to the date upon which notice", body, flags=re.IGNORECASE):
        call["observation_rule"] = (str(call.get("observation_rule") or "") + "_immediately_before_notice").strip("_")
    return [call]


def _extract_scheduled_puts(
    text: str,
    series_label: str | None = None,
    *,
    table_rows: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    date_row = _table_value(
        table_rows,
        "Bondholder Put Date",
        "Bondholder's Put Option Date",
        "Bondholders' Put Option Date",
        "Investor Put Date",
        "Investor Put Option",
    )
    if date_row and not re.search(r"\bNone\b", date_row, flags=re.IGNORECASE):
        dates = [_parse_long_date(match.group(1)) for match in re.finditer(DATE_RE.pattern, date_row, flags=re.IGNORECASE)]
        if dates:
            price_row = _table_value(table_rows, "Put Price", "Investor Put Price", "Issue / Put / Maturity Price")
            prices = [
                float(match.group(1).replace(",", ""))
                for match in re.finditer(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:%|per cent)", price_row, flags=re.IGNORECASE)
            ]
            if not prices and re.search(r"\b(?:principal amount|par)\b", price_row, flags=re.IGNORECASE):
                prices = [100.0]
            if series_label:
                selected_date = _select_series_item(dates, series_label)
                selected_price = _select_series_item(prices, series_label)
                pairs = [(selected_date, selected_price)]
            else:
                pairs = [
                    (put_date, prices[index] if index < len(prices) else (prices[0] if len(prices) == 1 else None))
                    for index, put_date in enumerate(dates)
                ]
            scheduled = [{
                    "type": "investor_put",
                    "model_type": "scheduled_put",
                    "date": str(put_date),
                    "price": float(put_price),
                    "description": "Scheduled holder put extracted from the summary terms; confirm settlement-equivalent wording and notice periods.",
                } for put_date, put_price in pairs if put_date and put_price is not None]
            if scheduled:
                return scheduled

    # Offering-circular cover prose can state a dated par put without a table.
    price_first = re.search(
        r"require\s+(?:the\s+)?(?:Company|Issuer)\s+to\s+(?:repurchase|redeem)[\s\S]{0,650}?"
        r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:%|per cent)\s+of\s+(?:the|their)\s+principal amount"
        r"[\s\S]{0,260}?\bon\s+(?P<dates>[\s\S]{0,140})",
        text,
        flags=re.IGNORECASE,
    )
    if price_first:
        dates = [_parse_long_date(match.group(1)) for match in re.finditer(DATE_RE.pattern, price_first.group("dates"), flags=re.IGNORECASE)]
        if dates:
            return [
                {
                    "type": "investor_put",
                    "model_type": "scheduled_put",
                    "date": put_date,
                    "price": float(price_first.group(1).replace(",", "")),
                    "description": "Scheduled holder put extracted from offering-circular summary prose; confirm settlement-equivalent mechanics.",
                }
                for put_date in dates
            ]

    prose_pattern = (
        r"(?:require\s+(?:the\s+)?(?:Company|Issuer)\s+to\s+(?:repurchase|redeem)|redeemed\s+at\s+the\s+option\s+of\s+the\s+holders?)"
        r"[\s\S]{0,220}?\bon\s+(" + DATE_RE.pattern.strip("()") + r")"
        r"[\s\S]{0,260}?([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:%|per cent)\s+of\s+(?:the|their)\s+principal amount"
    )
    matches = list(re.finditer(prose_pattern, text, flags=re.IGNORECASE))
    selected = _select_series_item(matches, series_label)
    if selected is not None:
        return [{
            "type": "investor_put",
            "model_type": "scheduled_put",
            "date": _parse_long_date(selected.group(1)),
            "price": float(selected.group(2).replace(",", "")),
            "description": "Scheduled holder put extracted from offering-circular summary prose; confirm any event conditions.",
        }]
    return []


def _extract_event_puts(text: str) -> list[dict[str, Any]]:
    targets = (
        ("change_of_control", r"(?:Change of Control Put|Redemption for Change of Control|upon a Change of Control)"),
        ("delisting_or_suspension", r"(?:De-?listing\s*(?:/|or)\s*Suspension of Trading Put|De-?listing Put|de-?list(?:ed|ing)[\s\S]{0,140}?suspension)"),
        ("no_registration_event", r"(?:No Registration Event Put|Registration Failure Put)"),
        ("corporate_event", r"(?:Corporate Event Put|Squeezeout Event)"),
        ("relevant_event", r"Redemption at the Option of the Bondholders[\s\S]{0,700}?Relevant Event"),
    )
    results: list[dict[str, Any]] = []
    for put_type, pattern in targets:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        window = text[match.start() : match.start() + 800]
        price_rule = "principal_amount"
        if re.search(r"Early Redemption Amount", window, flags=re.IGNORECASE):
            price_rule = "early_redemption_amount"
        results.append({
            "type": put_type,
            "model_type": "event_put",
            "date": None,
            "price": 100.0,
            "price_rule": price_rule,
            "description": "Event-driven holder put identified in the summary terms; occurrence probability and notice mechanics are not modeled in the lattice.",
        })
    return results


def _extract_pricing_date(text: str, *, table_rows: dict[str, str] | None = None) -> str | None:
    row_value = _table_value(table_rows, "Pricing Date", "Trade Date", "Pricing / Trade Date")
    if row_value:
        match = re.search(DATE_RE.pattern, row_value, flags=re.IGNORECASE)
        if match:
            return _parse_long_date(match.group(1))
    patterns = [
        r"\bPricing Date\s*(?:is|:)?\s*(" + DATE_RE.pattern.strip("()") + r")",
        r"SUMMARY(?:\s*:)?\s*(?:TERMS(?:\s*(?:&|AND)\s*CONDITIONS)?|INDICATIVE TERMS AND CONDITIONS)[^\n.]{0,180}?(" + DATE_RE.pattern.strip("()") + r")",
        r"(" + DATE_RE.pattern.strip("()") + r")[^\n.]{0,120}?SUMMARY(?:\s*:)?\s*(?:TERMS|INDICATIVE TERMS)",
        r"Offering circular dated\s*(" + DATE_RE.pattern.strip("()") + r")",
        r"(?:The\s+)?date of this offering circular is\s*(" + DATE_RE.pattern.strip("()") + r")",
        r"(?:Reference(?:\s+H)?\s+Share\s+Price)[\s\S]{0,220}?(?:on|as at)\s+(" + DATE_RE.pattern.strip("()") + r")",
        r"(?:Initial\s+(?:USD/CNH\s+)?Exchange Rate|Fixed Exchange Rate)[^.;]{0,280}?\bon\s+(" + DATE_RE.pattern.strip("()") + r")",
    ]
    for pattern in patterns:
        dated = re.search(pattern, text, flags=re.IGNORECASE)
        if dated:
            return _parse_long_date(dated.group(1))
    # Pricing and closing are economically distinct.  Returning None is safer
    # than silently shortening maturity by substituting the settlement date.
    return None


def _extract_denomination(text: str, *, table_rows: dict[str, str] | None = None) -> float | None:
    row_value = _table_value(table_rows, "Denomination")
    if row_value:
        row_match = re.search(_money_pattern(), row_value, flags=re.IGNORECASE)
        if row_match:
            return _scaled_money_match_value(row_match)
    match = re.search(r"Denomination(?:s? of)?\s*" + _money_pattern(), text, flags=re.IGNORECASE)
    if not match:
        match = re.search(r"denominations? of\s*" + _money_pattern(), text, flags=re.IGNORECASE)
    return _scaled_money_match_value(match) if match else None


def _extract_denomination_increment(text: str, *, table_rows: dict[str, str] | None = None) -> float | None:
    row_value = _table_value(table_rows, "Denomination")
    search_text = row_value or text
    match = re.search(
        r"integral multiples? of\s*" + _money_pattern() + r"\s+in excess thereof",
        search_text,
        flags=re.IGNORECASE,
    )
    return _scaled_money_match_value(match) if match else None


def _extract_issue_price(text: str, series_label: str | None = None, *, table_rows: dict[str, str] | None = None) -> float | None:
    row_value = _table_value(table_rows, "Issue Price", "Issue / Put / Maturity Price")
    if row_value:
        if series_label:
            explicit = re.search(
                rf"{re.escape(series_label)}\s+Bonds?\s*:?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:%|per cent)",
                row_value,
                flags=re.IGNORECASE,
            )
            if explicit:
                return float(explicit.group(1).replace(",", ""))
        percentages = [
            float(match.group(1).replace(",", ""))
            for match in re.finditer(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:%|per cent)", row_value, flags=re.IGNORECASE)
        ]
        selected = _select_series_item(percentages, series_label)
        if selected is not None:
            return float(selected)
    if series_label:
        label = re.escape(series_label)
        patterns = [
            rf"Issue Prices?[\s\S]{{0,800}}?{label}\s+Bonds\s*:?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*%",
            rf"{label}\s+Bonds[\s\S]{{0,400}}?issue price[\s\S]{{0,160}}?([0-9][0-9,]*(?:\.[0-9]+)?)\s*%",
            rf"issue price[\s\S]{{0,200}}?([0-9][0-9,]*(?:\.[0-9]+)?)\s*%[\s\S]{{0,320}}?{label}\s+Bonds",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return float(match.group(1).replace(",", ""))
    match = re.search(r"Issue Price\s*:?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*%", text, flags=re.IGNORECASE)
    if not match:
        match = re.search(r"Issue\s*/\s*Put\s*/\s*Maturity Price\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:%|of the principal amount)", text, flags=re.IGNORECASE)
    if not match:
        match = re.search(r"issue price is\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*per cent", text, flags=re.IGNORECASE)
    return float(match.group(1).replace(",", "")) if match else None


def _extract_investor_offer_price(
    text: str,
    series_label: str | None = None,
    *,
    table_rows: dict[str, str] | None = None,
) -> float | None:
    if series_label:
        label = re.escape(series_label)
        patterns = [
            rf"Convertible Bonds due\s+{label}[\s\S]{{0,180}}?OFFER PRICE\s*:?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*%",
            rf"{label}\s+Bonds[\s\S]{{0,220}}?Offer Price\s*:?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*%",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return float(match.group(1).replace(",", ""))
    row_value = _table_value(table_rows, "Offer Price")
    if row_value:
        match = re.search(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:%|per cent)", row_value, flags=re.IGNORECASE)
        if match:
            return float(match.group(1).replace(",", ""))
    return None


def _extract_maturity_price(
    text: str,
    series_label: str | None = None,
    *,
    table_rows: dict[str, str] | None = None,
) -> float | None:
    row_value = _table_value(table_rows, "Redemption Price at Maturity", "Redemption Price", "Issue / Put / Maturity Price")
    if row_value:
        percentages = [
            float(match.group(1).replace(",", ""))
            for match in re.finditer(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:%|per cent)", row_value, flags=re.IGNORECASE)
        ]
        selected = _select_series_item(percentages, series_label)
        if selected is not None:
            return float(selected)
        if re.search(r"\b(?:principal amount|par)\b", row_value, flags=re.IGNORECASE):
            return 100.0
    patterns = [
        # Final/prospectus table rows often read: "Redemption Price at Maturity
        # The U.S. Dollar Equivalent of 100.00% of the principal amount".  Use
        # a bounded semantic window instead of punctuation-delimited text because
        # currency abbreviations such as U.S. contain periods.
        r"Redemption Price at\s+Maturity[\s\S]{0,180}?([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:%|per cent)",
        r"redemption at maturity[\s\S]{0,180}?([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:%|per cent)",
        r"redeemed at\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:per cent|%)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return float(match.group(1).replace(",", ""))
    principal_patterns = [
        r"Redemption Price at\s+Maturity[\s\S]{0,180}?\b(?:principal amount|par)\b",
        r"redeem(?:ed)?\s+(?:each\s+Bond|the\s+Bonds)[\s\S]{0,180}?\b(?:principal amount|par)\b",
    ]
    for pattern in principal_patterns:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return 100.0
    return None


def _extract_underlying_ticker(text: str) -> str:
    patterns = [
        r"trading code\s+[“\"]?([0-9A-Za-z.]+)[”\"]?",
        r"Bloomberg ticker\s+([0-9A-Za-z.]+)\s+([A-Z]{2})",
        r"\(([0-9]{4})\s+(HK|CH|TT|JP|US|KS)\)",
        r"Stock Code:\s*([0-9A-Za-z.]+)\s*(HK|CH|TT|JP|US|KS)?",
        r'stock code[\s:]+(?:“|")?([0-9A-Za-z.]+)(?:\s*(HK|CH|TT|JP|US|KS))?',
        r"Securities Identification Code for the Shares[^.]{0,120}?\bis\s+([0-9A-Za-z.]+)",
        r"Shares[^.]{0,120}?listed on[^.]{0,120}?Tokyo Stock Exchange[^.]{0,220}?\bcode\s+([0-9A-Za-z.]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            code = match.group(1).upper().rstrip('.,;”"')
            suffix = (match.group(2).upper() if len(match.groups()) > 1 and match.group(2) else "")
            if not suffix:
                suffix = _infer_equity_suffix_from_context(text, match.start(), match.end())
            if suffix:
                return f"{code} {suffix}"
            continue
    return "needs_review"


def _infer_equity_suffix_from_context(text: str, start: int, end: int) -> str:
    """Infer Bloomberg equity suffix from the listing venue near the stock-code evidence."""
    window = text[max(0, start - 700) : min(len(text), end + 700)].lower()
    ordered_hints = [
        ("TT", ["taipei exchange", "tpex", "taiwan stock exchange", "twse", "listed in taiwan", "traded on the tpex"]),
        ("HK", ["hong kong stock exchange", "stock exchange of hong kong", "sehk", "hkex", "h shares", "hong kong"]),
        ("JP", ["tokyo stock exchange", "tse", "japan exchange", "listed in japan", "securities identification code committee of japan"]),
        ("CH", ["shanghai stock exchange", "shenzhen stock exchange", "a-share", "a share", "a-shares", "a shares"]),
        ("US", ["new york stock exchange", "nyse", "nasdaq", "listed in the united states"]),
        ("KS", ["korea exchange", "krx", "kospi", "kosdaq"]),
    ]
    for suffix, hints in ordered_hints:
        if any(hint in window for hint in hints):
            return suffix
    return ""


def _extract_settlement_currency(text: str, bond_currency: str | None, *, table_rows: dict[str, str] | None = None) -> str | None:
    row_value = _table_value(table_rows, "Currency", "Securities Offered")
    search_text = " ".join(part for part in (row_value, text[:20000]) if part)
    if re.search(r"U\.S\. Dollar Settled|USD settled|United States Dollar settled|United States Dollar settled", search_text, flags=re.IGNORECASE):
        return "USD"
    return bond_currency


def _extract_economic_currency(
    text: str,
    *,
    series_label: str | None,
    bond_currency: str | None,
    stock_currency: str | None,
    table_rows: dict[str, str] | None = None,
) -> str | None:
    currency_row = _table_value(table_rows, "Currency", "Securities Offered")
    linked = bool(re.search(r"(?:currency[- ]linked|linked to|reference currency)", " ".join((currency_row, text[:8000])), flags=re.IGNORECASE))
    if not linked:
        return bond_currency
    # Parallel A/B tables commonly put the linked series in the first column
    # and the plain USD series in the second.  Retain the legal denomination in
    # bond.currency and expose the risk currency separately.
    column = _series_column_index(series_label)
    if column is not None and column > 0:
        return bond_currency
    if stock_currency and stock_currency != bond_currency:
        return stock_currency
    return bond_currency


def _extract_exchangeable_terms(
    text: str,
    *,
    table_rows: dict[str, str] | None = None,
) -> dict[str, Any]:
    reference_company = None
    company_match = re.search(
        r"ordinary shares of\s+(.{2,100}?)(?:\s*\([“\"]|\s+are listed|\s+listed on)",
        text,
        flags=re.IGNORECASE,
    )
    if not company_match:
        company_match = re.search(
            r"Exchangeable into\s+(?:Ordinary\s+)?Shares of\s+(.{2,100}?)(?:\s*\(|\s+Guaranteed by|\s+due\b)",
            text,
            flags=re.IGNORECASE,
        )
    if company_match:
        candidate = _clean_issuer_candidate(company_match.group(1))
        if _issuer_name_is_plausible(candidate):
            reference_company = candidate

    property_match = re.search(
        r"Exchange Property[\s\S]{0,220}?initially comprise(?:s)?\s+(?:around|approximately)?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s+Shares",
        text,
        flags=re.IGNORECASE,
    )
    ratio_row = _table_value(table_rows, "Initial Exchange Ratio")
    ratio_match = re.search(
        r"([0-9][0-9,]*(?:\.[0-9]+)?)\s+Shares\s+per\s+" + _money_pattern(),
        ratio_row or text,
        flags=re.IGNORECASE,
    )
    initial_ratio = float(ratio_match.group(1).replace(",", "")) if ratio_match else None
    ratio_basis = _scaled_money_match_value(ratio_match) if ratio_match else None
    cash_election = bool(re.search(r"\bCash Election\b", text, flags=re.IGNORECASE))
    averaging_match = re.search(
        r"Cash Averaging Period[\s\S]{0,180}?period of\s+(\d+)\s+consecutive Trading Days",
        text,
        flags=re.IGNORECASE,
    )
    return {
        "reference_company_name": reference_company,
        "initial_exchange_property_shares": float(property_match.group(1).replace(",", "")) if property_match else None,
        "initial_exchange_ratio": initial_ratio,
        "initial_exchange_ratio_principal_basis": ratio_basis,
        "issuer_cash_election": cash_election,
        "cash_averaging_period_trading_days": int(averaging_match.group(1)) if averaging_match else None,
        "share_redemption_option": bool(re.search(r"Share Redemption Option", text, flags=re.IGNORECASE)),
    }


def _extract_series_isin(text: str, series_label: str | None) -> str | None:
    if not series_label:
        return None
    label = re.escape(series_label)
    patterns = [
        rf"{label}\s*:?[\s\S]{{0,80}}?\bISIN\s*:?\s*\b(XS[0-9A-Z]{{10}})\b",
        rf"{label}\s+Bonds(?:(?!Series\s+[A-Z]\s+Bonds).){{0,800}}?\b(XS[0-9A-Z]{{10}})\b",
        rf"\b(XS[0-9A-Z]{{10}})\b(?:(?!Series\s+[A-Z]\s+Bonds).){{0,800}}?{label}\s+Bonds",
        rf"(?:ISINs?|International Securities Identification Numbers?)[\s\S]{{0,800}}?{label}\s+Bonds\s*[:.\s]*\b(XS[0-9A-Z]{{10}})\b",
        rf"{label}\s+Bonds\s*[.\s]*\b(XS[0-9A-Z]{{10}})\b\s+\d{{6,12}}",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if match:
            # In all patterns exactly one capturing group is the ISIN.
            for group in match.groups():
                if group and re.fullmatch(r"XS[0-9A-Z]{10}", group.upper()):
                    return group.upper()
    return None


def _extract_single_security_isin(text: str) -> str | None:
    """Return an ISIN only when the document presents a unique security code.

    Some short-form pricing term sheets put the identifier in a header block such
    as ``Security Codes ISIN: XS... Common: ...`` rather than near the maturity
    or ``due YYYY Bonds`` label.  This fallback is intentionally limited to
    single-series drafts and requires either a unique XS identifier in the whole
    text or a unique XS identifier inside a bounded security-code/ISIN block.
    Multi-series documents must still use the series-aware matcher above.
    """

    explicit_security_codes = {
        value.upper()
        for value in re.findall(
            r"Security Codes?[\s\S]{0,180}?\bISIN\s*:?\s*\b(XS[0-9A-Z]{10})\b",
            text,
            flags=re.IGNORECASE,
        )
    }
    if len(explicit_security_codes) == 1:
        return next(iter(explicit_security_codes))

    security_code_blocks = re.findall(
        r"(?:ISINs?|International Securities Identification Numbers?)[\s\S]{0,300}?\b(XS[0-9A-Z]{10})\b",
        text,
        flags=re.IGNORECASE,
    )
    block_isins = {value.upper() for value in security_code_blocks}
    if len(block_isins) == 1:
        return next(iter(block_isins))

    all_isins = {match.group(0).upper() for match in re.finditer(r"\bXS[0-9A-Z]{10}\b", text, flags=re.IGNORECASE)}
    if len(all_isins) == 1:
        return next(iter(all_isins))
    return None


def _scaled_money_match_value(match: re.Match[str]) -> float:
    groups = match.groups()
    return _scaled_money_parts(groups[-2], groups[-1])


def _scaled_money_parts(number: str, scale: str | None) -> float:
    value = float(number.replace(",", ""))
    if scale and scale.lower().startswith("b"):
        value *= 1_000_000_000
    elif scale and scale.lower().startswith("m"):
        value *= 1_000_000
    return value


def _currency_from_token(token: str) -> str:
    token = token.upper().replace("$", "")
    return {"US": "USD", "HK": "HKD", "NT": "TWD", "S": "SGD", "RMB": "CNH", "¥": "JPY"}.get(token, token)


def _slug(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "_", value.lower())
    return re.sub(r"_+", "_", value).strip("_")


def _issuer_short_name(issuer: str) -> str:
    """Deterministically shorten common legal issuer suffixes for display names."""

    compact = " ".join((issuer or "").split()).strip()
    suffix_pattern = re.compile(
        r"\s+(Corporation|Corp\.?|Co\.?\s*,?\s*Ltd\.?|Company\s+Limited|Group\s+Limited|Limited|Ltd\.?)$",
        flags=re.IGNORECASE,
    )
    shortened = suffix_pattern.sub("", compact).strip()
    return shortened or compact


def _extract_deal_names(source_file: str) -> list[str]:
    """Return project/deal names from source filenames as aliases only."""

    stem = Path(source_file).stem if source_file else ""
    match = re.search(r"\b(Project\s+[A-Za-z0-9][A-Za-z0-9 -]*?)(?:\s+-|$)", stem)
    return [" ".join(match.group(1).split())] if match else []
