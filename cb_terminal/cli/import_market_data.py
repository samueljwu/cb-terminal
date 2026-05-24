"""Import daily equity and FX market data into the price-history SQLite database."""

from __future__ import annotations

import argparse
from pathlib import Path

from cb_terminal.storage.price_history_store import PriceHistoryStore

DEFAULT_DB = Path("data/price_history/price_history.sqlite")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import daily equity/FX market-data CSV or XLSX into SQLite.")
    parser.add_argument("input", help="CSV/XLSX daily market-data export")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite DB path")
    parser.add_argument("--notes", default="", help="Free-form import notes")
    parser.add_argument("--show-instrument", default="", help="Print latest points for one instrument after import")
    parser.add_argument("--show-latest", type=int, default=5, help="Number of latest points to print")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = PriceHistoryStore(args.db)
    batch = store.import_market_data_file(args.input, notes=args.notes)
    print(f"imported_market_data_batch_id={batch.id}")
    print(f"source_file={batch.source_file}")
    print(f"source_sha256={batch.source_sha256}")
    print(f"parsed_market_data_points={batch.row_count}")
    print(f"database={Path(args.db)}")
    print(f"equity_points={store.market_data_count(instrument_type='equity')}")
    print(f"fx_points={store.market_data_count(instrument_type='fx')}")
    if args.show_instrument:
        for point in store.market_data_points(instrument_id=args.show_instrument, limit=args.show_latest):
            print(
                "latest_market_data "
                f"date={point['as_of_date']} instrument={point['instrument_id']} "
                f"field={point['field']} value={point['value']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
