"""Preprocess detailed CB quote-history exports into model-ready daily rows.

Future price-history inputs are expected to look like Bloomberg/dealer quote
history: multiple intraday CB quotes per ISIN, optional same-row stock price,
source/dealer metadata, and occasional bad rows.  This module filters raw quote
rows and selects one auditable daily bond price before the pricing model sees the
row.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path
from typing import Iterable

from cb_terminal.io.daily_quote_selection import (
    DailyQuoteCandidate,
    is_clean_quote_values,
    select_daily_quote_candidate,
)
from cb_terminal.io.price_history import PriceQuoteRow, load_price_history_file


@dataclass(frozen=True)
class QuoteFilterPolicy:
    """Conservative bad-data filters for raw CB quote rows."""

    min_price: float = 1.0
    max_price: float = 1000.0
    max_bid_ask_spread: float = 10.0
    max_bid_ask_spread_pct: float = 0.10
    require_positive_stock_if_present: bool = True


@dataclass(frozen=True)
class SelectedDailyQuote:
    as_of_date: date
    bond_price: float
    stock_price: float | None
    price_currency: str
    cb_instrument_id: str
    cb_reference_security: str
    cb_contract_id: str
    cb_quote_time: time | None
    cb_quote_dealer: str
    cb_bid_price: float | None
    cb_ask_price: float | None
    selection_reason: str
    source_file: str
    source_sheet: str
    source_row: int


def load_and_select_daily_quotes(
    path: str | Path,
    *,
    isin: str,
    contract_id: str = "",
    stock_closes: dict[date, float] | None = None,
    policy: QuoteFilterPolicy = QuoteFilterPolicy(),
) -> list[SelectedDailyQuote]:
    """Load a detailed quote-history file and select one clean quote per day.

    Selection policy:
    - filter to the requested ISIN first;
    - reject non-positive/out-of-range prices, crossed markets, excessive spreads,
      and non-positive same-row stock prices;
    - ignore high-confidence stock-price currency/unit artifacts, while leaving
      ambiguous one- and two-contributor cases untouched;
    - prefer two-sided quotes, reject isolated daily price outliers with a robust
      median/MAD screen, and use a trusted stock close as additional context;
    - select a real observed quote nearest the resulting daily consensus and
      retain the decision-quality flags in ``selection_reason``.
    """

    rows = load_price_history_file(path, contract_id=contract_id)
    return select_daily_quotes(rows, isin=isin, contract_id=contract_id, stock_closes=stock_closes or {}, policy=policy)


def select_daily_quotes(
    rows: Iterable[PriceQuoteRow],
    *,
    isin: str,
    contract_id: str = "",
    stock_closes: dict[date, float] | None = None,
    policy: QuoteFilterPolicy = QuoteFilterPolicy(),
) -> list[SelectedDailyQuote]:
    isin = isin.strip().upper()
    stock_closes = stock_closes or {}
    clean: list[PriceQuoteRow] = []
    for row in rows:
        if (row.instrument_id or "").strip().upper() != isin:
            continue
        if not _is_clean_quote(row, policy):
            continue
        clean.append(row)
    by_date: dict[date, list[PriceQuoteRow]] = {}
    for row in clean:
        by_date.setdefault(row.as_of_date, []).append(row)
    selected: list[SelectedDailyQuote] = []
    for as_of_date in sorted(by_date):
        chosen, reason = _select_quote_for_date(by_date[as_of_date], stock_closes.get(as_of_date))
        selected.append(
            SelectedDailyQuote(
                as_of_date=as_of_date,
                bond_price=float(chosen.mid_price),
                stock_price=chosen.stock_price,
                price_currency=chosen.price_currency,
                cb_instrument_id=isin,
                cb_reference_security=chosen.reference_security,
                cb_contract_id=contract_id or chosen.contract_id,
                cb_quote_time=chosen.as_of_time,
                cb_quote_dealer=chosen.dealer,
                cb_bid_price=chosen.bid_price,
                cb_ask_price=chosen.ask_price,
                selection_reason=reason,
                source_file=chosen.source_file,
                source_sheet=chosen.source_sheet,
                source_row=chosen.source_row,
            )
        )
    return selected


def write_selected_daily_quotes_csv(path: str | Path, rows: Iterable[SelectedDailyQuote]) -> None:
    """Write selected quote rows with provenance for later stock/FX joining."""

    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "date",
        "bond_price",
        "quote_stock_price",
        "bond_price_currency",
        "cb_instrument_id",
        "cb_reference_security",
        "cb_contract_id",
        "cb_quote_time",
        "cb_quote_dealer",
        "cb_bid_price",
        "cb_ask_price",
        "selection_reason",
        "source_file",
        "source_sheet",
        "source_row",
    ]
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "date": row.as_of_date.isoformat(),
                    "bond_price": row.bond_price,
                    "quote_stock_price": "" if row.stock_price is None else row.stock_price,
                    "bond_price_currency": row.price_currency,
                    "cb_instrument_id": row.cb_instrument_id,
                    "cb_reference_security": row.cb_reference_security,
                    "cb_contract_id": row.cb_contract_id,
                    "cb_quote_time": row.cb_quote_time.isoformat(timespec="minutes") if row.cb_quote_time else "",
                    "cb_quote_dealer": row.cb_quote_dealer,
                    "cb_bid_price": "" if row.cb_bid_price is None else row.cb_bid_price,
                    "cb_ask_price": "" if row.cb_ask_price is None else row.cb_ask_price,
                    "selection_reason": row.selection_reason,
                    "source_file": row.source_file,
                    "source_sheet": row.source_sheet,
                    "source_row": row.source_row,
                }
            )


def _is_clean_quote(row: PriceQuoteRow, policy: QuoteFilterPolicy) -> bool:
    return is_clean_quote_values(
        mid_price=row.mid_price,
        bid_price=row.bid_price,
        ask_price=row.ask_price,
        stock_price=row.stock_price,
        min_price=policy.min_price,
        max_price=policy.max_price,
        max_bid_ask_spread=policy.max_bid_ask_spread,
        max_bid_ask_spread_pct=policy.max_bid_ask_spread_pct,
        require_positive_stock_if_present=policy.require_positive_stock_if_present,
    )


def _select_quote_for_date(rows: list[PriceQuoteRow], stock_close: float | None) -> tuple[PriceQuoteRow, str]:
    candidates = [
        DailyQuoteCandidate(
            payload=row,
            mid_price=float(row.mid_price),
            bid_price=row.bid_price,
            ask_price=row.ask_price,
            stock_price=row.stock_price,
            as_of_time=row.as_of_time,
            stable_key=(row.source_file, row.source_sheet, row.source_row, row.dealer, row.reference_security),
        )
        for row in rows
        if row.mid_price is not None
    ]
    chosen, reason = select_daily_quote_candidate(candidates, stock_close)
    return chosen.payload, reason
