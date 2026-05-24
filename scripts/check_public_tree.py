#!/usr/bin/env python3
"""Fail if private prospectus or price-history artifacts are tracked or staged."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

BLOCKED_SUFFIXES = {
    ".pdf",
    ".xls",
    ".xlsx",
    ".xlsm",
    ".sqlite",
    ".sqlite3",
    ".db",
}

BLOCKED_PREFIXES = (
    "data/raw/",
    "data/price_history/raw/",
    "data/price_history/generated/",
    "data/reports/",
)

BLOCKED_EXACT_PREFIXES = (
    "data/cb_terminal.sqlite",
    "data/price_history/price_history.sqlite",
)

ALLOWED_PREFIXES = (
    "tests/fixtures/",
)

ALLOWED_CSV_FIXTURE_WORDS = (
    "synthetic",
    "template",
    "anchor",
)


def git_lines(*args: str) -> list[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        print(result.stderr.strip(), file=sys.stderr)
        raise SystemExit(result.returncode)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def is_allowed_fixture(path: str) -> bool:
    if not path.startswith(ALLOWED_PREFIXES):
        return False
    lower_name = Path(path).name.lower()
    if Path(path).suffix.lower() == ".csv":
        return any(word in lower_name for word in ALLOWED_CSV_FIXTURE_WORDS)
    return False


def is_blocked(path: str) -> bool:
    path = path.replace("\\", "/")
    lower = path.lower()
    if is_allowed_fixture(path):
        return False
    if lower.startswith(BLOCKED_PREFIXES):
        return True
    if lower.startswith(BLOCKED_EXACT_PREFIXES):
        return True
    if Path(lower).suffix in BLOCKED_SUFFIXES:
        return True
    if "prospectus" in lower and Path(lower).suffix == ".pdf":
        return True
    if "price_history" in lower and Path(lower).suffix in {".csv", ".xls", ".xlsx", ".xlsm"}:
        return True
    return False


def main() -> int:
    tracked = set(git_lines("ls-files"))
    staged = set(git_lines("diff", "--cached", "--name-only"))
    offenders = sorted(path for path in tracked | staged if is_blocked(path))
    if offenders:
        print("Blocked private financial artifacts:")
        for path in offenders:
            print(f"  {path}")
        print("\nDo not commit prospectuses, uploaded price history, generated valuation CSVs, or SQLite runtime databases.")
        return 1
    print("Public tree check passed: no blocked prospectus or price-history artifacts are tracked/staged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
