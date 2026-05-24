"""CLI for drafting a reviewed contract JSON from a prospectus PDF."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from cb_terminal.domain import dumps_json
from cb_terminal.prospectus.draft_contract import draft_contract_from_text
from cb_terminal.prospectus.evidence import attach_source_evidence, evidence_summary
from cb_terminal.prospectus.extraction import ExtractionResult, PageText, extract_text_from_pdf
from cb_terminal.prospectus.review import build_review_report, validate_contract_dict


def main() -> None:
    parser = argparse.ArgumentParser(description="Draft normalized CB contract JSON from a prospectus PDF")
    parser.add_argument("--pdf", required=True, help="Path to prospectus/final offering circular PDF")
    parser.add_argument("--output", required=True, help="Path to write draft contract JSON")
    parser.add_argument("--review-output", required=True, help="Path to write markdown review checklist")
    parser.add_argument("--max-pages", type=int, default=None, help="Optional page limit for PDF extraction")
    parser.add_argument(
        "--text-fixture",
        default=None,
        help="Optional JSON page-text fixture for deterministic/offline evidence tests: {pages:[{page,text}]}",
    )
    parser.add_argument(
        "--require-evidence",
        action="store_true",
        help="Exit non-zero if required modeled terms do not have page-level evidence",
    )
    args = parser.parse_args()

    extraction = _load_text_fixture(args.text_fixture, args.pdf) if args.text_fixture else extract_text_from_pdf(args.pdf, max_pages=args.max_pages)
    if not extraction.has_text:
        raise ValueError("No extractable text. Install PyMuPDF or provide --text-fixture for reviewed page text.")
    contract = draft_contract_from_text(extraction.text, source_file=str(args.pdf))

    contract.setdefault("source_review", {})["extraction"] = {
        "method": extraction.method,
        "page_count": extraction.page_count,
        "warnings": extraction.warnings,
        "extracted_characters": len(extraction.text),
    }
    if extraction.pages:
        contract = attach_source_evidence(contract, extraction)
    issues = validate_contract_dict(contract)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(dumps_json(contract, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    review = Path(args.review_output)
    review.parent.mkdir(parents=True, exist_ok=True)
    review.write_text(build_review_report(contract, extraction), encoding="utf-8")

    error_count = sum(1 for issue in issues if issue.severity == "error")
    evidence = evidence_summary(contract)
    evidence_gap_count = len(evidence["missing_required_fields"])
    print(f"wrote contract: {output}")
    print(f"wrote review: {review}")
    print(f"validation_errors={error_count} validation_issues={len(issues)}")
    print(
        "source_evidence_covered="
        f"{evidence['covered_required_fields']}/{evidence['required_fields']} missing={evidence_gap_count}"
    )
    if error_count:
        raise SystemExit(2)
    if args.require_evidence and evidence_gap_count:
        raise SystemExit(3)


def _load_text_fixture(path: str, pdf_path: str) -> ExtractionResult:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    pages = [PageText(int(item["page"]), str(item["text"])) for item in raw.get("pages", [])]
    if not pages:
        raise ValueError("text fixture must contain at least one page entry")
    return ExtractionResult.from_pages(
        source_path=Path(pdf_path),
        pages=pages,
        method=f"text-fixture:{Path(path).name}",
    )


if __name__ == "__main__":
    main()
