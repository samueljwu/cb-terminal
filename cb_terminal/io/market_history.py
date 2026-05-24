"""CSV market-history ingestion for Phase 2 batch valuation.

Only stdlib CSV is supported here.  XLSX exports should be saved as CSV before
use, or handled by an optional adapter outside the clean core.
"""

from __future__ import annotations

import csv
from datetime import date, datetime
from io import StringIO
from pathlib import Path
from typing import Iterable, Optional

from cb_terminal.domain import FXConvention, MarketRow


ALIASES: dict[str, tuple[str, ...]] = {
    "date": ("date", "as_of_date", "as of date", "dt", "pricing date"),
    "stock_price": (
        "stock_price",
        "stock price",
        "underlying price",
        "underlying px",
        "underlying_px_last",
        "px_last",
        "last price",
        "equity px_last",
        "equity last",
    ),
    "bond_price": ("bond_price", "bond price", "cb price", "convertible price", "cb_px_last", "bond px_last"),
    "market_fx_rate": ("market_fx_rate", "fx", "fx rate", "market fx", "exchange rate", "spot fx"),
    "stock_currency": ("stock_currency", "stock ccy", "equity ccy", "underlying ccy"),
    "bond_price_currency": ("bond_price_currency", "bond ccy", "cb ccy", "price ccy", "currency"),
    "fx_convention": ("fx_convention", "fx convention", "fx direction"),
    "volatility": ("volatility", "vol", "input vol", "sigma"),
    "risk_free_rate": ("risk_free_rate", "risk free rate", "risk-free rate", "rates", "rf"),
    "credit_spread": ("credit_spread", "credit spread", "spread", "oas"),
    "credit_spread_bps": ("credit_spread_bps", "credit spread bps", "credit spread (bp)", "credit spread (bps)", "oas (bp)", "oas (bps)"),
    "borrow_rate": ("borrow_rate", "borrow rate", "borrow cost", "stock borrow"),
    "dividend_yield": ("dividend_yield", "dividend yield", "div yield", "dividend"),
}

ASSUMPTION_FIELDS = ("volatility", "risk_free_rate", "credit_spread", "borrow_rate", "dividend_yield")


def load_market_history_csv(path: str | Path) -> list[MarketRow]:
    path = Path(path)
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        return parse_market_history_csv(handle, source=str(path))


def parse_market_history_csv_text(text: str, *, source: str = "<text>") -> list[MarketRow]:
    return parse_market_history_csv(StringIO(text), source=source)


def parse_market_history_csv(handle: Iterable[str], *, source: str = "") -> list[MarketRow]:
    reader = csv.DictReader(handle)
    if not reader.fieldnames:
        raise ValueError("market history CSV must include a header row")
    header_map = _header_map(reader.fieldnames)
    _require_columns(header_map, ("date", "stock_price"))

    rows: list[MarketRow] = []
    for source_row, raw in enumerate(reader, start=2):
        if _is_blank_row(raw.values()):
            continue
        normalized = _normalize_row(raw, header_map)
        rows.append(
            MarketRow(
                as_of_date=_parse_date(_required_value(normalized, "date", source_row)),
                stock_price=_parse_float(_required_value(normalized, "stock_price", source_row), "stock_price", source_row),
                bond_price=_parse_optional_float(normalized.get("bond_price"), "bond_price", source_row),
                market_fx_rate=_parse_market_fx_rate(normalized.get("market_fx_rate"), source_row),
                stock_currency=_parse_currency(normalized.get("stock_currency")),
                bond_price_currency=_parse_currency(normalized.get("bond_price_currency")),
                fx_convention=_parse_fx_convention(normalized.get("fx_convention")),
                assumption_overrides=_parse_assumption_overrides(normalized, source_row),
                source=source,
                source_row=source_row,
            )
        )
    return rows


def _header_map(fieldnames: list[str]) -> dict[str, str]:
    canonical_by_alias = {}
    for canonical, aliases in ALIASES.items():
        for alias in aliases:
            canonical_by_alias[_normalize_header(alias)] = canonical
    result: dict[str, str] = {}
    for field in fieldnames:
        canonical = canonical_by_alias.get(_normalize_header(field), _normalize_header(field))
        result[field] = canonical
    return result


def _normalize_row(raw: dict[str, str], header_map: dict[str, str]) -> dict[str, str]:
    normalized = {}
    for header, value in raw.items():
        if header is None:
            continue
        canonical = header_map.get(header, _normalize_header(header))
        if canonical in normalized and not _is_blank(normalized[canonical]) and not _is_blank(value):
            if str(normalized[canonical]).strip() != str(value).strip():
                raise ValueError(f"conflicting values supplied for canonical column {canonical!r}")
        if canonical not in normalized or _is_blank(normalized[canonical]):
            normalized[canonical] = value
    return normalized


def _normalize_header(value: str) -> str:
    return " ".join((value or "").strip().lower().replace("_", " ").split())


def _require_columns(header_map: dict[str, str], required: tuple[str, ...]) -> None:
    present = set(header_map.values())
    missing = [name for name in required if name not in present]
    if missing:
        raise ValueError(f"required column(s) missing from market history CSV: {', '.join(missing)}")


def _required_value(row: dict[str, str], key: str, source_row: int) -> str:
    value = row.get(key)
    if _is_blank(value):
        raise ValueError(f"required column {key!r} is blank on source row {source_row}")
    return str(value)


def _is_blank(value: object) -> bool:
    return value is None or str(value).strip() == ""


def _is_blank_row(values: Iterable[object]) -> bool:
    return all(_is_blank(value) for value in values)


def _parse_date(value: str) -> date:
    value = value.strip()
    if "/" in value:
        parts = value.split("/")
        if len(parts) == 3 and all(part.isdigit() for part in parts):
            first = int(parts[0])
            second = int(parts[1])
            if first <= 12 and second <= 12:
                raise ValueError(f"ambiguous slash date {value!r}; use ISO YYYY-MM-DD")
            fmt = "%d/%m/%Y" if first > 12 else "%m/%d/%Y"
            return datetime.strptime(value, fmt).date()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%d-%b-%Y", "%d %b %Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    return date.fromisoformat(value)


def _parse_float(value: str, field: str, source_row: int) -> float:
    try:
        return float(str(value).strip().replace(",", ""))
    except ValueError as exc:
        raise ValueError(f"invalid numeric value for {field!r} on source row {source_row}: {value!r}") from exc


def _parse_optional_float(value: Optional[str], field: str, source_row: int) -> Optional[float]:
    if _is_blank(value):
        return None
    return _parse_float(str(value), field, source_row)


def _parse_market_fx_rate(value: Optional[str], source_row: int) -> float:
    parsed = _parse_optional_float(value, "market_fx_rate", source_row)
    return 1.0 if parsed is None else parsed


def _parse_currency(value: Optional[str]) -> Optional[str]:
    if _is_blank(value):
        return None
    return str(value).strip().upper()


def _parse_fx_convention(value: Optional[str]) -> Optional[FXConvention]:
    if _is_blank(value):
        return None
    text = str(value).strip().upper().replace(" ", "_").replace("-", "_")
    try:
        return FXConvention[text]
    except KeyError:
        return FXConvention(text)


def _parse_assumption_overrides(row: dict[str, str], source_row: int) -> dict[str, float]:
    overrides: dict[str, float] = {}
    for field in ASSUMPTION_FIELDS:
        if field in row and not _is_blank(row[field]):
            overrides[field] = _parse_rate(row[field], field, source_row)
    if "credit_spread_bps" in row and not _is_blank(row["credit_spread_bps"]):
        bps_value = _parse_float(row["credit_spread_bps"], "credit_spread_bps", source_row) / 10000.0
        if "credit_spread" in overrides and abs(overrides["credit_spread"] - bps_value) > 1e-12:
            raise ValueError("conflicting credit_spread and credit_spread_bps values")
        overrides["credit_spread"] = bps_value
    return overrides


def _parse_rate(value: str, field: str, source_row: int) -> float:
    text = str(value).strip()
    if text.endswith("%"):
        return _parse_float(text[:-1], field, source_row) / 100.0
    number = _parse_float(text, field, source_row)
    # Decimal rates are preferred.  Be tolerant of percent-like exported values.
    if abs(number) > 1.0:
        return number / 100.0
    return number
