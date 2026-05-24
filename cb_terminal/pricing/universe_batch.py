"""Universe-level batch pricing and report generation."""

from __future__ import annotations

import csv
import html
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cb_terminal.domain import Assumptions
from cb_terminal.io.contract_loader import load_contract_json
from cb_terminal.io.market_history import load_market_history_csv
from cb_terminal.io.market_history_validation import validate_market_history_file_for_contract
from cb_terminal.pricing.batch import RESULT_FIELDNAMES, price_history
from cb_terminal.pricing.engine import PricingEngine
from cb_terminal.prospectus.universe import UniverseItem, load_universe

UNIVERSE_FIELDNAMES = [
    "contract_id",
    "issuer",
    "underlying_ticker",
    "contract_status",
] + RESULT_FIELDNAMES

LATEST_SUMMARY_FIELDNAMES = [
    "contract_id",
    "issuer",
    "underlying_ticker",
    "contract_status",
    "date",
    "bond_price",
    "fair_value",
    "parity",
    "cheapness",
    "implied_volatility",
    "warning_count",
    "error",
]


@dataclass(frozen=True)
class UniversePricingReport:
    priced_contracts: int
    skipped_contracts: int
    result_rows: int
    skipped: list[dict[str, str]]
    latest_rows: list[dict[str, str]]


def price_universe(
    universe_path: str | Path,
    defaults: Assumptions,
    *,
    output_csv: str | Path,
    latest_summary_csv: str | Path | None = None,
    html_report: str | Path | None = None,
    project_root: str | Path | None = None,
    model_mode: str = "tf_split_tree",
) -> UniversePricingReport:
    root = Path(project_root) if project_root is not None else Path(universe_path).resolve().parent.parent.parent
    items = load_universe(universe_path)
    aggregate_rows: list[dict[str, str]] = []
    latest_rows: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    priced_contracts = 0

    for item in items:
        contract_path = _resolve_project_path(root, item.contract_path)
        market_path = _resolve_project_path(root, item.market_history_path) if item.market_history_path else None
        if not contract_path.exists():
            skipped.append(_skip(item, f"missing contract file: {contract_path}"))
            continue
        if market_path is None or not market_path.exists():
            skipped.append(_skip(item, f"missing market history file: {market_path}"))
            continue
        try:
            contract = load_contract_json(contract_path)
            validate_market_history_file_for_contract(market_path, contract).raise_for_errors()
            market_rows = load_market_history_csv(market_path)
            result_rows = price_history(contract, market_rows, defaults, engine=PricingEngine(model_mode=model_mode))
        except Exception as exc:
            skipped.append(_skip(item, str(exc)))
            continue
        priced_contracts += 1
        item_rows = [_universe_row(item, result) for result in result_rows]
        aggregate_rows.extend(item_rows)
        if item_rows:
            latest_rows.append(_latest_summary_row(item_rows[-1]))

    _write_csv(output_csv, UNIVERSE_FIELDNAMES, aggregate_rows)
    if latest_summary_csv is not None:
        _write_csv(latest_summary_csv, LATEST_SUMMARY_FIELDNAMES, latest_rows)
    if html_report is not None:
        _write_html_report(html_report, latest_rows, skipped)
    return UniversePricingReport(
        priced_contracts=priced_contracts,
        skipped_contracts=len(skipped),
        result_rows=len(aggregate_rows),
        skipped=skipped,
        latest_rows=latest_rows,
    )


def _resolve_project_path(root: Path, path: str | None) -> Path:
    root_resolved = root.resolve()
    if path is None:
        return root_resolved / "<missing>"
    candidate = Path(path)
    resolved = candidate.resolve() if candidate.is_absolute() else (root_resolved / candidate).resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(f"universe path escapes project root: {path}") from exc
    return resolved


def _skip(item: UniverseItem, reason: str) -> dict[str, str]:
    return {
        "contract_id": item.id,
        "issuer": item.issuer,
        "underlying_ticker": item.underlying_ticker,
        "reason": reason,
    }


def _universe_row(item: UniverseItem, result: Any) -> dict[str, str]:
    return {
        "contract_id": item.id,
        "issuer": item.issuer,
        "underlying_ticker": item.underlying_ticker,
        "contract_status": item.status,
        "date": result.as_of_date.isoformat(),
        "stock_price": _format_optional_float(result.stock_price),
        "bond_price": _format_optional_float(result.bond_price),
        "market_fx_rate": _format_optional_float(result.market_fx_rate),
        "fair_value": _format_optional_float(result.fair_value),
        "parity": _format_optional_float(result.parity),
        "bond_floor": _format_optional_float(result.bond_floor),
        "cheapness": _format_optional_float(result.cheapness),
        "implied_volatility": _format_optional_float(result.implied_volatility),
        "output_currency": result.output_currency,
        "warning_count": str(result.warning_count),
        "warnings": result.warnings,
        "source": result.source,
        "source_row": str(result.source_row),
        "assumption_source": result.assumption_source,
        "valuation_date": result.valuation_date.isoformat(),
        "volatility": _format_optional_float(result.volatility),
        "risk_free_rate": _format_optional_float(result.risk_free_rate),
        "credit_spread": _format_optional_float(result.credit_spread),
        "borrow_rate": _format_optional_float(result.borrow_rate),
        "dividend_yield": _format_optional_float(result.dividend_yield),
        "steps": str(result.steps),
        "error": result.error,
    }


def _latest_summary_row(row: dict[str, str]) -> dict[str, str]:
    return {field: row.get(field, "") for field in LATEST_SUMMARY_FIELDNAMES}


def _write_csv(path: str | Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_html_report(path: str | Path, latest_rows: list[dict[str, str]], skipped: list[dict[str, str]]) -> None:
    cards = []
    for row in latest_rows:
        cards.append(
            "<tr>"
            f"<td>{html.escape(row['contract_id'])}</td>"
            f"<td>{html.escape(row['issuer'])}</td>"
            f"<td>{html.escape(row['underlying_ticker'])}</td>"
            f"<td>{html.escape(row['date'])}</td>"
            f"<td>{html.escape(row['bond_price'])}</td>"
            f"<td>{html.escape(row['fair_value'])}</td>"
            f"<td>{html.escape(row['cheapness'])}</td>"
            f"<td>{html.escape(row['implied_volatility'])}</td>"
            f"<td>{html.escape(row['warning_count'])}</td>"
            "</tr>"
        )
    skipped_items = "".join(
        f"<li>{html.escape(item['contract_id'])}: {html.escape(item['reason'])}</li>" for item in skipped
    ) or "<li>None</li>"
    document = f"""<!doctype html>
<html lang=\"en\">
<head>
<meta charset=\"utf-8\">
<title>CB Universe Report</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #172033; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border: 1px solid #d8dee9; padding: 0.45rem; text-align: right; }}
th:first-child, td:first-child, th:nth-child(2), td:nth-child(2), th:nth-child(3), td:nth-child(3) {{ text-align: left; }}
th {{ background: #eef3fb; }}
.warning {{ color: #8a5a00; }}
</style>
</head>
<body>
<h1>CB Universe Report</h1>
<p>Latest row per priced contract. Inputs are explicit assumptions plus each contract's market-history CSV.</p>
<table>
<thead><tr><th>Contract</th><th>Issuer</th><th>Underlying</th><th>Date</th><th>Bond Px</th><th>Fair Value</th><th>Cheapness</th><th>Implied Vol</th><th>Warnings</th></tr></thead>
<tbody>{''.join(cards)}</tbody>
</table>
<h2>Skipped contracts / missing data</h2>
<ul class=\"warning\">{skipped_items}</ul>
</body>
</html>
"""
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(document, encoding="utf-8")


def _format_optional_float(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.10g}"
    return str(value)
