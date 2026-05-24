"""Build valuation-ready market-history CSV rows from imported quote/equity/FX data."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from cb_terminal.storage.price_history_store import PriceHistoryStore

DEFAULT_DB = Path("data/price_history/price_history.sqlite")
FIELDNAMES = [
    "date",
    "stock_price",
    "bond_price",
    "market_fx_rate",
    "stock_currency",
    "bond_price_currency",
    "fx_convention",
    "cb_instrument_id",
    "cb_reference_security",
    "cb_contract_id",
    "cb_quote_time",
    "cb_quote_dealer",
    "cb_bid_price",
    "cb_ask_price",
    "cb_selection_reason",
    "equity_instrument_id",
    "fx_instrument_id",
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Join CB quote history with equity/FX history into valuation-ready CSV rows.")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite DB path")
    parser.add_argument("--cb-instrument-id", required=True, help="CB quote instrument id in cb_price_quotes")
    parser.add_argument("--equity-instrument-id", required=True, help="Equity instrument id in market_data_points")
    parser.add_argument("--fx-instrument-id", default="", help="FX instrument id in market_data_points")
    parser.add_argument("--stock-currency", default="", help="Stock/equity currency for valuation CSV")
    parser.add_argument("--bond-price-currency", default="", help="CB quote currency for valuation CSV")
    parser.add_argument("--fx-convention", default="STOCK_PER_CB", help="FX convention; project standard is STOCK_PER_CB (e.g. TWD per USD)")
    parser.add_argument("--output", required=True, help="Output market-history CSV path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = PriceHistoryStore(args.db)
    rows = store.build_valuation_market_rows(
        cb_instrument_id=args.cb_instrument_id,
        equity_instrument_id=args.equity_instrument_id,
        fx_instrument_id=args.fx_instrument_id,
        stock_currency=args.stock_currency,
        bond_price_currency=args.bond_price_currency,
        fx_convention=args.fx_convention,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote_market_history={output}")
    print(f"joined_rows={len(rows)}")
    if not rows:
        print("warning=no overlapping CB quote dates and equity/FX market-data dates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
