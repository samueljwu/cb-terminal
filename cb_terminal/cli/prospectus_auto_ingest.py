"""CLI for automated prospectus ingestion and reviewed-raw cleanup."""

from __future__ import annotations

import argparse
from pathlib import Path

from cb_terminal.prospectus.auto_ingest import approve_reviewed_contract, auto_ingest_prospectuses


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Automatically ingest new raw CB prospectuses into reviewable contract drafts.")
    parser.add_argument("--project-root", default=".", help="Project root containing data/ directories")
    parser.add_argument("--prospectus-dir", default=None, help="Raw prospectus directory; defaults under project root")
    parser.add_argument("--contracts-dir", default=None, help="Contracts directory; defaults under project root")
    parser.add_argument("--reviews-dir", default=None, help="Review markdown directory; defaults under project root")
    parser.add_argument("--coverage-dir", default=None, help="Coverage metadata directory; defaults under project root")
    parser.add_argument("--fixture-dir", default=None, help="Optional page-text fixture directory; defaults under project root")
    parser.add_argument("--delete-duplicate-raw", action="store_true", help="Delete raw PDF files that are exact instrument duplicates of existing contracts")
    parser.add_argument("--approve-contract", default=None, help="Reviewed contract JSON path whose raw prospectus may be archived/deleted")
    parser.add_argument("--delete-reviewed-raw", action="store_true", help="Delete raw source for --approve-contract; requires status=reviewed and checksum match")
    parser.add_argument("--archive-reviewed-raw", default=None, help="Archive raw source for --approve-contract instead of deleting")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.project_root)
    if args.approve_contract:
        result = approve_reviewed_contract(
            args.approve_contract,
            delete_raw=args.delete_reviewed_raw,
            archive_dir=args.archive_reviewed_raw,
        )
        print(f"reviewed_cleanup contract={result['contract_path']} source={result['source_path']}")
        print(f"raw_deleted={result['raw_deleted']} raw_archived={result['raw_archived']}")
        if result.get("archive_path"):
            print(f"archive_path={result['archive_path']}")
        return 0

    report = auto_ingest_prospectuses(
        prospectus_dir=Path(args.prospectus_dir) if args.prospectus_dir else root / "data/raw/prospectuses",
        contracts_dir=Path(args.contracts_dir) if args.contracts_dir else root / "data/contracts",
        reviews_dir=Path(args.reviews_dir) if args.reviews_dir else root / "data/prospectus_reviews",
        coverage_dir=Path(args.coverage_dir) if args.coverage_dir else root / "data/coverage",
        fixture_dir=Path(args.fixture_dir) if args.fixture_dir else root / "data/prospectus_text",
        delete_duplicate_raw=args.delete_duplicate_raw,
    )
    print(f"wrote review queue: {report.queue_path}")
    print(
        f"scanned={report.scanned} created_contracts={report.created_contracts} duplicates={report.duplicates} "
        f"needs_extraction={report.needs_extraction} failed={report.failed}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
