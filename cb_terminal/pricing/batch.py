"""Phase 2 batch pricing for historical market rows."""

from __future__ import annotations

import csv
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Iterable, Optional

from cb_terminal.domain import Assumptions, Contract, MarketRow
from cb_terminal.pricing.engine import PricingEngine


@dataclass(frozen=True)
class ResultRow:
    as_of_date: date
    stock_price: float
    bond_price: Optional[float]
    market_fx_rate: float
    fair_value: Optional[float]
    parity: Optional[float]
    bond_floor: Optional[float]
    cheapness: Optional[float]
    implied_volatility: Optional[float]
    output_currency: str
    warning_count: int
    warnings: str
    source: str
    source_row: int
    assumption_source: str
    valuation_date: date
    volatility: float
    risk_free_rate: float
    credit_spread: float
    borrow_rate: float
    dividend_yield: float
    steps: int
    error: str = ""


RESULT_FIELDNAMES = [
    "date",
    "stock_price",
    "bond_price",
    "market_fx_rate",
    "fair_value",
    "parity",
    "bond_floor",
    "cheapness",
    "implied_volatility",
    "output_currency",
    "warning_count",
    "warnings",
    "source",
    "source_row",
    "assumption_source",
    "valuation_date",
    "volatility",
    "risk_free_rate",
    "credit_spread",
    "borrow_rate",
    "dividend_yield",
    "steps",
    "error",
]


def price_history(
    contract: Contract,
    rows: Iterable[MarketRow],
    defaults: Assumptions,
    *,
    engine: Optional[PricingEngine] = None,
) -> list[ResultRow]:
    pricer = engine or PricingEngine()
    results: list[ResultRow] = []
    for row in rows:
        market = row.to_market_snapshot()
        assumptions = assumptions_for_row(defaults, row)
        assumption_source = "row_override" if row.assumption_overrides else "defaults"
        try:
            result = pricer.price(contract, market, assumptions)
            warnings = list(result.diagnostics.warnings)
            implied_volatility = None
            if market.bond_price is not None:
                try:
                    implied_volatility = pricer.implied_vol(contract, market, assumptions, target_price=market.bond_price)
                except ValueError as exc:
                    warnings.append(f"implied_volatility: {exc}")
            results.append(
                _make_result_row(
                    row,
                    assumptions,
                    assumption_source,
                    fair_value=result.fair_value,
                    parity=result.parity,
                    bond_floor=result.bond_floor,
                    cheapness=result.cheapness,
                    implied_volatility=implied_volatility,
                    output_currency=result.output_currency,
                    warnings=warnings,
                )
            )
        except Exception as exc:
            warning = f"pricing_error: {exc}"
            results.append(
                _make_result_row(
                    row,
                    assumptions,
                    assumption_source,
                    fair_value=None,
                    parity=None,
                    bond_floor=None,
                    cheapness=None,
                    implied_volatility=None,
                    output_currency=contract.currency or contract.settlement_currency,
                    warnings=[warning],
                    error=str(exc),
                )
            )
    return results


def _make_result_row(
    row: MarketRow,
    assumptions: Assumptions,
    assumption_source: str,
    *,
    fair_value: Optional[float],
    parity: Optional[float],
    bond_floor: Optional[float],
    cheapness: Optional[float],
    implied_volatility: Optional[float],
    output_currency: str,
    warnings: list[str],
    error: str = "",
) -> ResultRow:
    valuation_date = assumptions.valuation_date or row.as_of_date
    return ResultRow(
        as_of_date=row.as_of_date,
        stock_price=row.stock_price,
        bond_price=row.bond_price,
        market_fx_rate=row.market_fx_rate,
        fair_value=fair_value,
        parity=parity,
        bond_floor=bond_floor,
        cheapness=cheapness,
        implied_volatility=implied_volatility,
        output_currency=output_currency,
        warning_count=len(warnings),
        warnings="; ".join(warnings),
        source=row.source,
        source_row=row.source_row,
        assumption_source=assumption_source,
        valuation_date=valuation_date,
        volatility=assumptions.volatility,
        risk_free_rate=assumptions.risk_free_rate,
        credit_spread=assumptions.credit_spread,
        borrow_rate=assumptions.borrow_rate,
        dividend_yield=assumptions.dividend_yield,
        steps=int(assumptions.steps),
        error=error,
    )


def assumptions_for_row(defaults: Assumptions, row: MarketRow) -> Assumptions:
    updates = dict(row.assumption_overrides)
    updates["valuation_date"] = row.as_of_date
    return replace(defaults, **updates)


def write_results_csv(results: Iterable[ResultRow], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=RESULT_FIELDNAMES)
        writer.writeheader()
        for result in results:
            writer.writerow(_result_to_dict(result))


def _result_to_dict(result: ResultRow) -> dict[str, object]:
    return {
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
        "warning_count": result.warning_count,
        "warnings": result.warnings,
        "source": result.source,
        "source_row": result.source_row,
        "assumption_source": result.assumption_source,
        "valuation_date": result.valuation_date.isoformat(),
        "volatility": _format_optional_float(result.volatility),
        "risk_free_rate": _format_optional_float(result.risk_free_rate),
        "credit_spread": _format_optional_float(result.credit_spread),
        "borrow_rate": _format_optional_float(result.borrow_rate),
        "dividend_yield": _format_optional_float(result.dividend_yield),
        "steps": result.steps,
        "error": result.error,
    }


def _format_optional_float(value: Optional[float]) -> str:
    if value is None:
        return ""
    return f"{value:.10g}"
