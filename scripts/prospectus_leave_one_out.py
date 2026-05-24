#!/usr/bin/env python3
"""Leave-one-out prospectus extraction evaluation.

This is a development validation/refinement harness, not a runtime extraction
feature.  It rotates every raw prospectus as a holdout, does not mutate parser
state, and does not fit document-specific rules; the training set is reported
separately so parser changes can be judged against all-but-one documents before
looking at the rotated holdout score.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from cb_terminal.prospectus.draft_contract import draft_contracts_from_text
from cb_terminal.prospectus.extraction import extract_text_from_pdf

CORE_FIELDS: tuple[str, ...] = (
    "issuer.name",
    "instrument.canonical_id",
    "bond.currency",
    "bond.issue_size",
    "bond.issue_price",
    "bond.denomination",
    "bond.maturity_date",
    "redemption.maturity_price",
    "conversion.underlying_ticker",
    "conversion.initial_conversion_price",
)

OPTIONAL_VALUATION_FIELDS: tuple[str, ...] = (
    "conversion.fixed_exchange_rate",
    "conversion.conversion_premium",
    "conversion.start_date",
    "conversion.end_date",
)


def _get(mapping: dict[str, Any], dotted: str) -> Any:
    current: Any = mapping
    for part in dotted.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _present(value: Any) -> bool:
    return value not in (None, "", "needs_review", [])


@dataclass(frozen=True)
class DocumentScore:
    path: str
    status: str
    page_count: int
    draft_count: int
    core_present: int
    core_evidence: int
    optional_present: int
    optional_evidence: int
    missing_core: list[str]
    missing_core_evidence: list[str]
    missing_optional: list[str]
    missing_optional_evidence: list[str]

    @property
    def core_score(self) -> float:
        return self.core_present / len(CORE_FIELDS)

    @property
    def core_evidence_score(self) -> float:
        return self.core_evidence / len(CORE_FIELDS)

    @property
    def optional_score(self) -> float:
        return self.optional_present / len(OPTIONAL_VALUATION_FIELDS)

    @property
    def optional_evidence_score(self) -> float:
        return self.optional_evidence / len(OPTIONAL_VALUATION_FIELDS)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "status": self.status,
            "page_count": self.page_count,
            "draft_count": self.draft_count,
            "core_present": self.core_present,
            "core_total": len(CORE_FIELDS),
            "core_score": round(self.core_score, 4),
            "core_evidence": self.core_evidence,
            "core_evidence_score": round(self.core_evidence_score, 4),
            "optional_present": self.optional_present,
            "optional_total": len(OPTIONAL_VALUATION_FIELDS),
            "optional_score": round(self.optional_score, 4),
            "optional_evidence": self.optional_evidence,
            "optional_evidence_score": round(self.optional_evidence_score, 4),
            "missing_core": self.missing_core,
            "missing_core_evidence": self.missing_core_evidence,
            "missing_optional": self.missing_optional,
            "missing_optional_evidence": self.missing_optional_evidence,
        }


def score_pdf(pdf: Path) -> DocumentScore:
    extraction = extract_text_from_pdf(pdf)
    drafts = draft_contracts_from_text(extraction.text, source_file=str(pdf)) if extraction.has_text else []
    # Score at document level: a field is present/evidence-backed if every draft
    # has it.  Multi-series docs must therefore extract the term for each series.
    missing_core: list[str] = []
    missing_core_evidence: list[str] = []
    missing_optional: list[str] = []
    missing_optional_evidence: list[str] = []
    if not drafts:
        missing_core = list(CORE_FIELDS)
        missing_core_evidence = list(CORE_FIELDS)
        missing_optional = list(OPTIONAL_VALUATION_FIELDS)
        missing_optional_evidence = list(OPTIONAL_VALUATION_FIELDS)
    else:
        for field in CORE_FIELDS:
            if not all(_present(_get(draft, field)) for draft in drafts):
                missing_core.append(field)
            if not all(field in draft.get("source_review", {}).get("term_evidence", {}) for draft in drafts):
                missing_core_evidence.append(field)
        for field in OPTIONAL_VALUATION_FIELDS:
            if not all(_present(_get(draft, field)) for draft in drafts):
                missing_optional.append(field)
            if not all(field in draft.get("source_review", {}).get("term_evidence", {}) for draft in drafts):
                missing_optional_evidence.append(field)
    return DocumentScore(
        path=str(pdf),
        status=extraction.status,
        page_count=extraction.page_count,
        draft_count=len(drafts),
        core_present=len(CORE_FIELDS) - len(missing_core),
        core_evidence=len(CORE_FIELDS) - len(missing_core_evidence),
        optional_present=len(OPTIONAL_VALUATION_FIELDS) - len(missing_optional),
        optional_evidence=len(OPTIONAL_VALUATION_FIELDS) - len(missing_optional_evidence),
        missing_core=missing_core,
        missing_core_evidence=missing_core_evidence,
        missing_optional=missing_optional,
        missing_optional_evidence=missing_optional_evidence,
    )


def build_report(root: Path) -> dict[str, Any]:
    prospectus_dir = root / "data/raw/prospectuses"
    pdfs = sorted(prospectus_dir.glob("*.pdf"))
    doc_scores = [score_pdf(pdf) for pdf in pdfs]
    rotations: list[dict[str, Any]] = []
    for holdout in doc_scores:
        training = [score for score in doc_scores if score.path != holdout.path]
        rotations.append({
            "holdout": Path(holdout.path).name,
            "training_documents": len(training),
            "training_core_score_avg": round(mean(score.core_score for score in training), 4) if training else 0.0,
            "training_core_evidence_score_avg": round(mean(score.core_evidence_score for score in training), 4) if training else 0.0,
            "training_optional_score_avg": round(mean(score.optional_score for score in training), 4) if training else 0.0,
            "training_optional_evidence_score_avg": round(mean(score.optional_evidence_score for score in training), 4) if training else 0.0,
            "holdout_core_score": round(holdout.core_score, 4),
            "holdout_core_evidence_score": round(holdout.core_evidence_score, 4),
            "holdout_optional_score": round(holdout.optional_score, 4),
            "holdout_optional_evidence_score": round(holdout.optional_evidence_score, 4),
            "holdout_missing_core": holdout.missing_core,
            "holdout_missing_core_evidence": holdout.missing_core_evidence,
            "holdout_missing_optional": holdout.missing_optional,
            "holdout_missing_optional_evidence": holdout.missing_optional_evidence,
        })
    return {
        "prospectus_count": len(pdfs),
        "core_fields": CORE_FIELDS,
        "optional_valuation_fields": OPTIONAL_VALUATION_FIELDS,
        "documents": [score.as_dict() for score in doc_scores],
        "rotations": rotations,
        "overall": {
            "core_score_avg": round(mean(score.core_score for score in doc_scores), 4) if doc_scores else 0.0,
            "core_evidence_score_avg": round(mean(score.core_evidence_score for score in doc_scores), 4) if doc_scores else 0.0,
            "optional_score_avg": round(mean(score.optional_score for score in doc_scores), 4) if doc_scores else 0.0,
            "optional_evidence_score_avg": round(mean(score.optional_evidence_score for score in doc_scores), 4) if doc_scores else 0.0,
            "min_core_score": round(min((score.core_score for score in doc_scores), default=0.0), 4),
            "min_core_evidence_score": round(min((score.core_evidence_score for score in doc_scores), default=0.0), 4),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true", help="Emit full JSON report")
    parser.add_argument("--min-core-score", type=float, default=1.0)
    parser.add_argument("--min-core-evidence-score", type=float, default=0.9)
    parser.add_argument("--min-optional-score", type=float, default=0.0)
    parser.add_argument("--min-optional-evidence-score", type=float, default=0.0)
    args = parser.parse_args()
    report = build_report(args.root)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"prospectuses={report['prospectus_count']}")
        print("overall", report["overall"])
        for rotation in report["rotations"]:
            print(
                f"holdout={rotation['holdout']} "
                f"core={rotation['holdout_core_score']:.2f} "
                f"core_evidence={rotation['holdout_core_evidence_score']:.2f} "
                f"optional={rotation['holdout_optional_score']:.2f} "
                f"optional_evidence={rotation['holdout_optional_evidence_score']:.2f}"
            )
            if rotation["holdout_missing_core"] or rotation["holdout_missing_core_evidence"]:
                print("  missing_core", rotation["holdout_missing_core"])
                print("  missing_core_evidence", rotation["holdout_missing_core_evidence"])
    overall = report["overall"]
    if overall["min_core_score"] < args.min_core_score:
        return 1
    if overall["min_core_evidence_score"] < args.min_core_evidence_score:
        return 1
    if overall["optional_score_avg"] < args.min_optional_score:
        return 1
    if overall["optional_evidence_score_avg"] < args.min_optional_evidence_score:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
