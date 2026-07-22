"""CLI for selecting clean daily CB prices from detailed quote history."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from cb_terminal.io.price_history_preprocess import load_and_select_daily_quotes, write_selected_daily_quotes_csv


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Preprocess detailed CB quote history into one clean daily quote per ISIN")
    parser.add_argument("input", help="Detailed CB quote-history CSV/XLSX")
    parser.add_argument("--isin", required=True, help="Target CB ISIN / canonical instrument id")
    parser.add_argument("--contract-id", default="", help="Contract id to stamp into output provenance")
    parser.add_argument("--output", required=True, help="Output selected daily quote CSV")
    parser.add_argument(
        "--stock-close",
        action="append",
        default=[],
        metavar="YYYY-MM-DD=PRICE",
        help=(
            "Trusted stock close for selection; repeatable. When present, compare robust two-sided CB consensus quotes "
            "observed at nearby same-row stock levels."
        ),
    )
    args = parser.parse_args(argv)
    selected = load_and_select_daily_quotes(
        args.input,
        isin=args.isin,
        contract_id=args.contract_id,
        stock_closes=_parse_stock_closes(args.stock_close),
    )
    write_selected_daily_quotes_csv(args.output, selected)
    print(f"selected_quotes={len(selected)} output={Path(args.output)}")
    return 0


def _parse_stock_closes(values: list[str]) -> dict[date, float]:
    result: dict[date, float] = {}
    for item in values:
        if "=" not in item:
            raise ValueError(f"--stock-close must be YYYY-MM-DD=PRICE, got {item!r}")
        raw_date, raw_price = item.split("=", 1)
        result[date.fromisoformat(raw_date.strip())] = float(raw_price.strip().replace(",", ""))
    return result


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
