"""Draft normalized contract JSON from prospectus text.

Runtime drafting is issuer-neutral. It recognizes common CB term patterns and
leaves unsupported terms blank or needs_review until a reviewer checks the
source snippets and pages. Curated samples are fixture/backfill inputs only.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from cb_terminal.io.instrument_registry import cb_display_name
from cb_terminal.prospectus.schema import required_review_items, required_term_keys


DATE_RE = re.compile(r"(\b\d{1,2}\s+[A-Z][a-z]+,?\s+20\d{2}\b|\b[A-Z][a-z]+\s+\d{1,2},\s+20\d{2}\b)")


def draft_contract_from_text(text: str, *, source_file: str = "") -> dict[str, Any]:
    drafts = draft_contracts_from_text(text, source_file=source_file)
    if not drafts:
        raise ValueError("unsupported prospectus text; no conservative draft template matched")
    return drafts[0]


def draft_contracts_from_text(text: str, *, source_file: str = "") -> list[dict[str, Any]]:
    normalized = " ".join(text.split())
    generics = _draft_generic_contracts(normalized, source_file=source_file)
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
    for fmt in ("%d %B %Y", "%B %d %Y"):
        try:
            return datetime.strptime(clean, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"unsupported date format: {value}")


def _draft_generic_contracts(text: str, *, source_file: str = "") -> list[dict[str, Any]]:
    lower = text.lower()
    if "convertible bond" not in lower and "convertible bonds" not in lower:
        return []
    issuer = _extract_issuer_name(text)
    default_conversion_price, default_stock_currency = _extract_conversion_price(text)
    table_rows = _extract_terms_table_rows(text)
    settlement_currency_hint = _extract_settlement_currency(text, None, table_rows=table_rows)
    fixed_fx = fx_units = fx_convention = None
    underlying = _extract_underlying_ticker(text)
    series = _extract_series_terms(text)
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
        conversion_price, stock_currency = _extract_conversion_price(text, series_label=term_series_label)
        if conversion_price is None:
            conversion_price = default_conversion_price
        if stock_currency is None:
            stock_currency = default_stock_currency
        settlement_currency = settlement_currency_hint or _extract_settlement_currency(text, bond_currency, table_rows=table_rows)
        fixed_fx, fx_units, fx_convention = _extract_fixed_fx(text, bond_currency, stock_currency)
        maturity_year = maturity_date[:4]
        issuer_short_name = _issuer_short_name(issuer)
        pricing_date = _extract_pricing_or_closing_date(text)
        closing_date = _extract_closing_date(text) or _infer_issue_date_from_maturity_span(text, maturity_date)
        conversion_start = _extract_conversion_start_date(text, series_label=term_series_label) or _infer_relative_conversion_start_date(text, closing_date or pricing_date)
        conversion_end = _extract_conversion_end_date(text, series_label=term_series_label) or _infer_relative_conversion_end_date(text, maturity_date)
        series_suffix = "" if series_label == maturity_year else f" {series_label}"
        contract_id = _slug(f"{issuer}{series_suffix} {maturity_year} cb")
        isin = _extract_series_isin(text, series_label=series_label)
        if not isin and len(series) == 1:
            isin = _extract_single_security_isin(text)
        if isin:
            contract_id = f"{isin}_contract"
        zero_coupon = "zero coupon" in lower
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
        },
        "source_file": source_file,
        "source_type": "automated_prospectus_draft",
        "status": "needs_review",
        "issuer": {"name": issuer, "ticker": underlying},
        "bond": {
            "description": f"{_display_currency_token(bond_currency)}{issue_size:,.0f} {series_label + ' ' if series_label != maturity_year else ''}{'Zero Coupon ' if zero_coupon else ''}Convertible Bonds due {maturity_year}",
            "currency": bond_currency,
            "settlement_currency": settlement_currency or bond_currency,
            "stock_currency": stock_currency or bond_currency,
            "denomination": _extract_denomination(text, table_rows=table_rows),
            "pricing_face": 100.0,
            "issue_size": issue_size,
            "issue_price": _extract_issue_price(text, series_label=term_series_label, table_rows=table_rows),
            "coupon_rate": 0.0 if zero_coupon else None,
            "coupon_frequency": 0 if zero_coupon else None,
            "pricing_date": pricing_date,
            "closing_date": closing_date,
            "maturity_date": maturity_date,
            "day_count": "needs_review",
        },
        "redemption": {"maturity_price": _extract_maturity_price(text, table_rows=table_rows)},
        "conversion": {
            "underlying_ticker": underlying,
            "initial_conversion_price": conversion_price,
            "conversion_premium": _extract_conversion_premium(text, series_label=term_series_label),
            "fixed_exchange_rate": fixed_fx,
            "fixed_exchange_rate_units": fx_units,
            "start_date": conversion_start,
            "end_date": conversion_end,
            "restricted_periods": "needs_review",
        },
        "calls": _extract_soft_calls(text),
        "puts": [],
        "source_review": {
            "created_from": "automated_text_extraction_generic_template",
            "review_status": "needs_human_review",
            "required_term_keys": required_term_keys(),
            "review_items": required_review_items(),
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
    "Securities Offered",
    "Currency",
    "Denomination",
    "Maturity Date",
    "Issue Price",
    "Deal Size",
    "Issue Size",
    "Offer Size",
    "Coupon",
    "Yield to Put / Maturity",
    "Issue / Put / Maturity Price",
    "Redemption Price at Maturity",
    "Initial Conversion Price",
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
    label_pattern = r"(?:" + "|".join(re.escape(label) for label in labels) + r")"
    matches = list(re.finditer(rf"\b({label_pattern})\b", text, flags=re.IGNORECASE))
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


def _extract_series_terms(text: str) -> list[dict[str, Any]]:
    """Return per-series issue size/maturity rows explicitly visible in text."""
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, float]] = set()
    pattern = (
        _money_pattern()
        + r"\s+(?:Currency-Linked\s+)?(?:Zero Coupon\s+)?(?:U\.S\. Dollar Settled\s+)?Convertible Bonds due\s+(20\d{2})"
        + r"(?:\s*\((?:the\s+)?[“\"]?([^”\")]+? Bonds)[”\"]?\))?"
    )
    for match in re.finditer(pattern, text, flags=re.IGNORECASE):
        token, number, scale, year, label = match.groups()
        issue_size = _scaled_money_parts(number, scale)
        bond_currency = _currency_from_token(token)
        maturity_date = _maturity_date_for_series(text, year)
        if not maturity_date:
            fallback_maturity = _extract_maturity_date(text)
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
        field_patterns["conversion.start_date"] = [(_date_value_pattern(start_date), "conversion-start-date"), (r"Conversion Period[\s\S]{0,500}?(?:day after the Issue Date|\d+(?:st|nd|rd|th)\s+day\s+from\s+the\s+Closing Date)", "conversion-start-relative")]
    end_date = contract.get("conversion", {}).get("end_date")
    if end_date not in (None, ""):
        field_patterns["conversion.end_date"] = [(_date_value_pattern(end_date), "conversion-end-date"), (r"Conversion Period[\s\S]{0,500}?(?:\d+(?:st|nd|rd|th)?\s+day\s+prior\s+to\s+(?:the\s+)?Maturity Date|\d+\s+working\s+days?\s+prior\s+to\s+(?:the\s+)?Maturity Date|\d+\s+days?\s+prior\s+to\s+(?:the\s+)?Maturity Date)", "conversion-end-relative")]
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


def _extract_issuer_name(text: str) -> str | None:
    clean_text = re.sub(r"---\s*Page\s+\d+\s*---", " ", text).strip()

    terms_issuer = re.search(
        r"SUMMARY TERMS.{0,2200}?\bIssuer\s+(.{1,180}?)(?:\s+\(the|\s+\(Stock Code:|\s+Securities Offered|\s+Currency\b)",
        clean_text[:30000],
        flags=re.IGNORECASE,
    )
    if terms_issuer:
        return " ".join(terms_issuer.group(1).split()).strip()

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
            return " ".join(candidate.split()).strip()

    after_page = re.search(
        r"(?:STRICTLY CONFIDENTIAL\s+)?([A-Z][A-Za-z0-9 .,&'’()-]+?(?:Corporation|Co\., Ltd\.|Company Limited|Group Limited|Limited|Ltd\.?))\s*(?:\(|(?:US\$|HK\$|NT\$|S\$|¥|JPY|RMB|CNH|EUR|USD|HKD|TWD|SGD)\s*[0-9])",
        clean_text[:12000],
    )
    if after_page:
        candidate = after_page.group(1)
        parts = re.split(r"OFFERING\s+CIRCULAR|STRICTLY\s+CONFIDENTIAL", candidate, flags=re.IGNORECASE)
        candidate = parts[-1].strip() if len(parts) > 1 else candidate.strip()
        return " ".join(candidate.split()).strip()
    issuer_label = re.search(
        r"\bIssuer\s+(.{1,220}?)(?:\s+\(the [^)]*\))?\s+(?:\(Stock Code:|Securities Offered|Currency\b|Form\b|Ranking\b)",
        clean_text[:20000],
        flags=re.IGNORECASE,
    )
    if issuer_label:
        return " ".join(issuer_label.group(1).split()).strip()
    before = re.split(r"(?:US\$|HK\$|NT\$|S\$|¥|JPY|RMB|CNH|EUR|USD|HKD|TWD|SGD)\s*[0-9]", clean_text, maxsplit=1)[0]
    lines = [line.strip() for line in before.splitlines() if line.strip() and not line.strip().startswith("--- Page")]
    if lines:
        return lines[0]
    compact = " ".join(before.split()).strip()
    if compact:
        return compact
    match = re.match(r"\s*([A-Z][A-Za-z0-9 .,&'-]+?)\s+(?:US\$|HK\$|NT\$|S\$|¥|JPY|RMB|CNH|EUR|USD|HKD|TWD|SGD)", clean_text)
    return match.group(1).strip() if match else None


def _money_pattern() -> str:
    return r"(US\$|HK\$|NT\$|S\$|¥|JPY|RMB|CNH|EUR|USD|HKD|TWD|SGD)\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(billion|million)?"


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


def _extract_conversion_price(text: str, series_label: str | None = None) -> tuple[float, str] | tuple[None, None]:
    money = _money_pattern()
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


def _extract_conversion_premium(text: str, series_label: str | None = None) -> float | None:
    patterns: list[str] = []
    if series_label:
        label = re.escape(series_label)
        patterns.extend([
            rf"conversion premium[\s\S]{{0,240}}?([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:%|per cent)[\s\S]{{0,320}}?{label}\s+Bonds",
            rf"{label}\s+Bonds[\s\S]{{0,420}}?conversion premium[\s\S]{{0,240}}?([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:%|per cent)",
        ])
    patterns.extend([
        r"conversion premium[^.]{0,240}?([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:%|per cent)",
        r"premium of[^.]{0,120}?([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:%|per cent)[^.]{0,160}?conversion price",
    ])
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return float(match.group(1).replace(",", ""))
    return None


def _extract_fixed_fx(text: str, bond_currency: str | None, stock_currency: str | None) -> tuple[float | None, str | None, str | None]:
    match = re.search(
        r"(?:fixed exchange rate(?: is)?(?: of)?|Fixed Exchange Rate)\s*(US\$|HK\$|NT\$|S\$|¥|JPY|RMB|CNH|EUR|USD|HKD|TWD|SGD)\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*=\s*(US\$|HK\$|NT\$|S\$|¥|JPY|RMB|CNH|EUR|USD|HKD|TWD|SGD)\s*1(?:\.00)?",
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


def _extract_maturity_date(text: str, *, table_rows: dict[str, str] | None = None) -> str | None:
    row_value = _table_value(table_rows, "Maturity Date")
    if row_value:
        row_patterns = [
            r"\bon\s+(" + DATE_RE.pattern.strip("()") + r")",
            r"mature[^.]{0,180}?(" + DATE_RE.pattern.strip("()") + r")",
            r"redeem[^.]{0,180}?(" + DATE_RE.pattern.strip("()") + r")",
        ]
        for pattern in row_patterns:
            row_match = re.search(pattern, row_value, flags=re.IGNORECASE)
            if row_match:
                return _parse_long_date(row_match.group(1))
    patterns = [
        r"mature on(?: or about)?\s+(" + DATE_RE.pattern.strip("()") + r")",
        r"Maturity Date\s+(?:On or about\s+)?(" + DATE_RE.pattern.strip("()") + r")",
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
    patterns = [
        r"(?:from and including|from,?\s+and\s+including,?|commence on)\s+(" + DATE_RE.pattern.strip("()") + r")",
        r"Conversion Period\s+Convertible at (?:the option of the Bondholders thereof, )?at any time on or after(?: the day after)? the Issue Date",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
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
    patterns = [
        r"(?:to and including|to,?\s+and\s+including,?|end on)\s+(" + DATE_RE.pattern.strip("()") + r")",
        r"until\s+10\s+working\s+days\s+prior\s+to\s+Maturity Date",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match and match.groups():
            return _parse_long_date(match.group(1))
    return None


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
    if re.search(r"day after the Issue Date", window, flags=re.IGNORECASE):
        return (anchor + timedelta(days=1)).isoformat()
    match = re.search(r"on or after the\s+(\d+)(?:st|nd|rd|th)\s+day\s+from\s+the\s+Closing Date", window, flags=re.IGNORECASE)
    if match:
        return (anchor + timedelta(days=int(match.group(1)) - 1)).isoformat()
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
    match = re.search(r"(\d+)(?:st|nd|rd|th)?\s+day\s+prior\s+to\s+the\s+Maturity Date", window, flags=re.IGNORECASE)
    business_days = False
    if not match:
        match = re.search(r"(\d+)\s+working\s+days?\s+prior\s+to\s+(?:the\s+)?Maturity Date", window, flags=re.IGNORECASE)
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


def _conversion_period_window(text: str) -> str:
    matches = list(re.finditer(r"Conversion Period\s+([\s\S]{0,900}?)(?:Conversion Right|Redemption at the Option|Issuer Call|Adjustments to Conversion|$)", text, flags=re.IGNORECASE))
    if not matches:
        return ""
    windows = [" ".join(match.group(1).split()) for match in matches]
    for window in windows:
        if re.search(r"Maturity Date|Closing Date|Issue Date|working days? prior", window, flags=re.IGNORECASE):
            return window
    return windows[0]


def _extract_closing_date(text: str) -> str | None:
    patterns = [
        r"Closing / Settlement Date\s+(?:Expected\s+)?(?:on or about\s+)?(" + DATE_RE.pattern.strip("()") + r")",
        r"Issue / Closing / Settlement Date\s+(?:On or about\s+)?(" + DATE_RE.pattern.strip("()") + r")",
        r"closing date (?:is |for [^,]+ is expected to take place by |is expected to be on |[^.]{0,60}?on or about )(" + DATE_RE.pattern.strip("()") + r")",
        r"Issue Date\s+(" + DATE_RE.pattern.strip("()") + r")",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return _parse_long_date(match.group(1))
    return None


def _extract_soft_calls(text: str) -> list[dict[str, Any]]:
    """Extract generic issuer soft-call clauses with explicit date/trigger terms."""

    call_window = re.search(
        r"Issuer Call(?P<body>[\s\S]{0,1500}?)(?:Clean Up Call|Tax Call|Change of Control|Put Option|Adjustment upon|$)",
        text,
        flags=re.IGNORECASE,
    )
    if not call_window:
        call_window = re.search(
            r"(?:Optional Redemption|early redeemed at the Issuer[’']s option|(?:Company|Issuer)\s+may\s+redeem)(?P<body>[\s\S]{0,1500}?)(?:Tax Redemption|Change of Control|Put Option|$)",
            text,
            flags=re.IGNORECASE,
        )
    if not call_window:
        return []
    body = " ".join(call_window.group("body").split())
    start_date = None
    start_match = re.search(
        r"(?:at any time after|on or after|after)\s+(" + DATE_RE.pattern.strip("()") + r")",
        body,
        flags=re.IGNORECASE,
    )
    if start_match:
        start_date = _parse_long_date(start_match.group(1))
    trigger = None
    trigger_match = re.search(
        r"(?:at least|not less than)\s+([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:%|per\s+cent\.?)[\s\S]{0,180}?Conversion Price",
        body,
        flags=re.IGNORECASE,
    )
    if not trigger_match:
        trigger_match = re.search(
            r"closing\s+price[\s\S]{0,220}?([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:%|per\s+cent\.?)\s+of\s+the\s+Conversion Price",
            body,
            flags=re.IGNORECASE,
        )
    if trigger_match:
        trigger = float(trigger_match.group(1).replace(",", "")) / 100.0
    if not (start_date and trigger):
        return []
    call = {
        "model_type": "soft_call",
        "start_date": start_date,
        "price": 100.0,
        "trigger_ratio": trigger,
        "description": "Issuer soft call extracted from prospectus text; confirm notice period and observation window before final use.",
    }
    window_match = re.search(
        r"for any\s+(\d+)\s+[^.]{0,120}?within a period of\s+(\d+)\s+consecutive",
        body,
        flags=re.IGNORECASE,
    )
    if window_match:
        call["trigger_days"] = int(window_match.group(1))
        call["trigger_window_days"] = int(window_match.group(2))
    return [call]


def _extract_pricing_or_closing_date(text: str) -> str | None:
    dated = re.search(r"Offering circular dated\s*(" + DATE_RE.pattern.strip("()") + r")", text, flags=re.IGNORECASE)
    if not dated:
        dated = re.search(r"SUMMARY TERMS.{0,120}?(" + DATE_RE.pattern.strip("()") + r")", text[:500], flags=re.IGNORECASE)
    if dated:
        return _parse_long_date(dated.group(1))
    return _extract_closing_date(text)


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


def _extract_issue_price(text: str, series_label: str | None = None, *, table_rows: dict[str, str] | None = None) -> float | None:
    row_value = _table_value(table_rows, "Issue Price", "Issue / Put / Maturity Price")
    if row_value and not series_label:
        row_match = re.search(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:%|per cent)", row_value, flags=re.IGNORECASE)
        if row_match:
            return float(row_match.group(1).replace(",", ""))
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
    match = re.search(r"Issue Price\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*%", text, flags=re.IGNORECASE)
    if not match:
        match = re.search(r"Issue\s*/\s*Put\s*/\s*Maturity Price\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:%|of the principal amount)", text, flags=re.IGNORECASE)
    if not match:
        match = re.search(r"issue price is\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*per cent", text, flags=re.IGNORECASE)
    return float(match.group(1).replace(",", "")) if match else None


def _extract_maturity_price(text: str, *, table_rows: dict[str, str] | None = None) -> float | None:
    row_value = _table_value(table_rows, "Redemption Price at Maturity", "Issue / Put / Maturity Price")
    if row_value:
        row_match = re.search(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:%|per cent)", row_value, flags=re.IGNORECASE)
        if row_match:
            return float(row_match.group(1).replace(",", ""))
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


def _extract_series_isin(text: str, series_label: str | None) -> str | None:
    if not series_label:
        return None
    label = re.escape(series_label)
    patterns = [
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

    security_code_blocks = re.findall(
        r"(?:Security Codes?|ISINs?|International Securities Identification Numbers?)[\s\S]{0,300}?\b(XS[0-9A-Z]{10})\b",
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
