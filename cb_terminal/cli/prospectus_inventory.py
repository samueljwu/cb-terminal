"""CLI for building a conservative raw-prospectus inventory."""

from __future__ import annotations

import argparse
from pathlib import Path

from cb_terminal.domain import dumps_json
from cb_terminal.prospectus.inventory import build_prospectus_inventory


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inventory raw CB prospectus PDFs without inventing contract terms.")
    parser.add_argument("--prospectus-dir", default="data/raw/prospectuses", help="Directory containing raw prospectus PDFs")
    parser.add_argument("--contracts-dir", default="data/contracts", help="Directory containing normalized contract JSON files")
    parser.add_argument("--output", required=True, help="Output JSON inventory path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    inventory = build_prospectus_inventory(args.prospectus_dir, contracts_dir=args.contracts_dir)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(dumps_json(inventory, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    ready = sum(1 for item in inventory if item.get("contract_path"))
    print(f"wrote prospectus inventory: {output}")
    print(f"prospectuses={len(inventory)} contracts_linked={ready} prospectus_only={len(inventory) - ready}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
