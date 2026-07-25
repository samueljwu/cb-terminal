"""Yield-curve helpers for CB risk-free-rate selection.

Fetch a government yield curve by CB currency, parse the maturity/yield table,
and linearly interpolate the point matching maturity or the earliest put.
"""

from __future__ import annotations

import html
import json
import re
import urllib.request
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Mapping

from cb_terminal.domain import Contract


@dataclass(frozen=True)
class YieldPoint:
    years: float
    rate: float
    label: str


@dataclass(frozen=True)
class YieldCurve:
    currency: str
    source: str
    as_of: str
    points: list[YieldPoint]


@dataclass(frozen=True)
class MatchedYield:
    currency: str
    rate: float
    target_years: float
    target_date: date
    source: str
    as_of: str
    matched_label: str


SUPPORTED_YIELD_CURVE_CURRENCIES = (
    "USD",
    "HKD",
    "TWD",
    "CNY",
    "JPY",
    "KRW",
    "AUD",
)


COUNTRY_BY_CURRENCY = {
    "USD": "united-states",
    "TWD": "taiwan",
    "JPY": "japan",
    "EUR": "germany",
    "GBP": "united-kingdom",
    "HKD": "hong-kong",
    "SGD": "singapore",
    "CNY": "china",
    "CNH": "china",
    "RMB": "china",
    "KRW": "south-korea",
    "AUD": "australia",
}


def curve_currency_for_contract(contract: Contract) -> str:
    """Return the government-curve currency for risk-free-rate matching.

    The cash discount curve follows the economic principal/cash-flow currency,
    not the exchange currency of the underlying stock. This matters both for a
    conventional cross-currency CB (for example, a USD Lenovo bond on HKD
    shares) and for a currency-linked legal-USD bond whose economic principal
    is explicitly TWD, CNY, or another currency.
    """

    bond_currency = (contract.currency or "").strip().upper()
    settlement_currency = (contract.settlement_currency or "").strip().upper()
    term_extensions = contract.metadata.get("term_extensions", {}) if isinstance(contract.metadata, dict) else {}
    metadata_economic_currency = (
        str(term_extensions.get("economic_currency") or "").strip().upper()
        if isinstance(term_extensions, dict)
        else ""
    )
    return _select_curve_currency(
        economic_currency=(contract.economic_currency or metadata_economic_currency),
        bond_currency=bond_currency,
        settlement_currency=settlement_currency,
    )


def curve_currency_from_contract_dict(raw: Mapping[str, Any]) -> str:
    """Read the curve currency from draft terms that may not yet fully validate."""

    bond = raw.get("bond") if isinstance(raw.get("bond"), Mapping) else {}
    return _select_curve_currency(
        economic_currency=str(bond.get("economic_currency") or ""),
        bond_currency=str(bond.get("currency") or ""),
        settlement_currency=str(bond.get("settlement_currency") or ""),
    )


def _select_curve_currency(
    *,
    economic_currency: str,
    bond_currency: str,
    settlement_currency: str,
) -> str:
    return (
        economic_currency.strip().upper()
        or bond_currency.strip().upper()
        or settlement_currency.strip().upper()
    )


def effective_curve_date(contract: Contract, valuation_date: date) -> date:
    """Use earliest future scheduled put if present; otherwise maturity."""

    future_puts = [put.date for put in contract.puts if put.model_type == "scheduled_put" and put.date and put.date > valuation_date]
    if future_puts:
        return min(future_puts)
    return contract.maturity_date


def effective_curve_years(contract: Contract, valuation_date: date) -> float:
    return max((effective_curve_date(contract, valuation_date) - valuation_date).days / 365.25, 0.0)


def match_curve_for_contract(contract: Contract, valuation_date: date, curve: YieldCurve) -> MatchedYield:
    target_years = effective_curve_years(contract, valuation_date)
    rate, label = interpolate_curve_rate_with_label(curve.points, target_years)
    return MatchedYield(
        currency=curve.currency,
        rate=rate,
        target_years=target_years,
        target_date=effective_curve_date(contract, valuation_date),
        source=curve.source,
        as_of=curve.as_of,
        matched_label=label,
    )


def interpolate_curve_rate(points: Iterable[YieldPoint], target_years: float) -> float:
    return interpolate_curve_rate_with_label(points, target_years)[0]


def interpolate_curve_rate_with_label(points: Iterable[YieldPoint], target_years: float) -> tuple[float, str]:
    ordered = sorted(points, key=lambda point: point.years)
    if not ordered:
        raise ValueError("yield curve has no points")
    if target_years <= ordered[0].years:
        return ordered[0].rate, ordered[0].label
    if target_years >= ordered[-1].years:
        return ordered[-1].rate, ordered[-1].label
    for left, right in zip(ordered, ordered[1:]):
        if left.years <= target_years <= right.years:
            if abs(target_years - left.years) < 1e-12:
                return left.rate, left.label
            if abs(target_years - right.years) < 1e-12:
                return right.rate, right.label
            weight = (target_years - left.years) / (right.years - left.years)
            rate = left.rate + (right.rate - left.rate) * weight
            return rate, f"interpolated {left.label}-{right.label}"
    return ordered[-1].rate, ordered[-1].label


def fetch_worldgovernmentbonds_curve(currency: str, *, timeout: int = 30) -> YieldCurve:
    """Fetch and parse the current WorldGovernmentBonds curve for a currency."""

    normalized = currency.strip().upper()
    country_slug = COUNTRY_BY_CURRENCY.get(normalized)
    if not country_slug:
        raise ValueError(f"unsupported yield-curve currency: {currency}")

    page_url = f"https://www.worldgovernmentbonds.com/country/{country_slug}/"
    page_html = _urlopen_text(page_url, timeout=timeout)
    global_vars = _extract_global_vars(page_html)
    post_body = json.dumps({"GLOBALVAR": global_vars}).encode("utf-8")
    endpoint = "https://www.worldgovernmentbonds.com/wp-json/country/v1/main"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://www.worldgovernmentbonds.com",
        "Referer": page_url,
    }
    response_text = _urlopen_text(endpoint, data=post_body, headers=headers, timeout=timeout)
    payload = json.loads(response_text)
    if not payload.get("success"):
        raise ValueError("WorldGovernmentBonds response did not report success")
    points = parse_worldgovernmentbonds_main_table(str(payload.get("mainTable", "")))
    if not points:
        raise ValueError("WorldGovernmentBonds response contained no parseable curve points")
    return YieldCurve(
        currency=normalized,
        source=page_url,
        as_of=str(payload.get("lastDataValDesc") or payload.get("lastTimeValDesc") or ""),
        points=points,
    )


def parse_worldgovernmentbonds_main_table(table_html: str) -> list[YieldPoint]:
    points: list[YieldPoint] = []
    for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, flags=re.IGNORECASE | re.DOTALL):
        cells = [_clean_html(cell) for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, flags=re.IGNORECASE | re.DOTALL)]
        point = _point_from_cells(cells)
        if point is not None:
            points.append(point)
    return points


def _point_from_cells(cells: list[str]) -> YieldPoint | None:
    maturity_index = None
    years = None
    for index, cell in enumerate(cells):
        years = _parse_maturity_years(cell)
        if years is not None:
            maturity_index = index
            break
    if maturity_index is None or years is None:
        return None
    for cell in cells[maturity_index + 1 :]:
        match = re.search(r"(-?\d+(?:\.\d+)?)\s*%", cell)
        if match:
            return YieldPoint(years=years, rate=float(match.group(1)) / 100.0, label=cells[maturity_index].lower())
    return None


def _parse_maturity_years(value: str) -> float | None:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(month|months|year|years)", value.strip().lower())
    if not match:
        return None
    amount = float(match.group(1))
    unit = match.group(2)
    return amount / 12.0 if unit.startswith("month") else amount


def _clean_html(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    return html.unescape(re.sub(r"\s+", " ", text)).strip()


def _extract_global_vars(page_html: str) -> dict[str, object]:
    match = re.search(r"var\s+jsGlobalVars\s*=\s*(\{.*?\});", page_html, flags=re.DOTALL)
    if not match:
        raise ValueError("could not locate WorldGovernmentBonds jsGlobalVars")
    return json.loads(match.group(1))


def _urlopen_text(url: str, *, data: bytes | None = None, headers: dict[str, str] | None = None, timeout: int = 30) -> str:
    request_headers = {"User-Agent": "Mozilla/5.0"}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, data=data, headers=request_headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")
