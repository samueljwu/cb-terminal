"""Import raw CB quote history into the dedicated price-history SQLite database."""

from __future__ import annotations

import argparse
from pathlib import Path

from cb_terminal.storage.price_history_store import PriceHistoryStore

DEFAULT_DB = Path("data/price_history/price_history.sqlite")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import raw CB quote/price-history CSV or XLSX into SQLite.")
    parser.add_argument("input", help="CSV/XLSX price-history export")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite DB path for quote history")
    parser.add_argument("--instrument-id", default="", help="Stable instrument id; defaults to Reference Security per row")
    parser.add_argument("--contract-id", default="", help="Optional normalized contract id once mapped/reviewed")
    parser.add_argument("--notes", default="", help="Free-form import notes")
    parser.add_argument("--show-latest", type=int, default=5, help="Print latest imported quotes after import")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = PriceHistoryStore(args.db)
    batch = store.import_file(
        args.input,
        instrument_id=args.instrument_id,
        contract_id=args.contract_id,
        notes=args.notes,
    )
    print(f"imported_price_history_batch_id={batch.id}")
    print(f"source_file={batch.source_file}")
    print(f"source_sha256={batch.source_sha256}")
    print(f"parsed_quote_rows={batch.row_count}")
    print(f"database={Path(args.db)}")
    total = store.quote_count(instrument_id=args.instrument_id, contract_id=args.contract_id)
    print(f"matching_database_quote_rows={total}")
    if args.show_latest:
        for quote in store.latest_quotes(instrument_id=args.instrument_id, contract_id=args.contract_id, limit=args.show_latest):
            print(
                "latest_quote "
                f"date={quote['as_of_date']} time={quote['as_of_time']} "
                f"instrument={quote['instrument_id']} dealer={quote['dealer']} "
                f"bid={quote['bid_price']} ask={quote['ask_price']} mid={quote['mid_price']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
