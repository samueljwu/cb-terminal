"""Phase 2 batch pricing for historical market rows."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Iterable, Optional

from cb_terminal.domain import Assumptions, Contract, MarketRow, MarketSnapshot
from cb_terminal.pricing.engine import MODEL_VERSION, ImpliedVolError, PricingEngine
from cb_terminal.pricing.yields import calculate_market_yields


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
    yield_to_maturity: Optional[float]
    yield_to_put: Optional[float]
    yield_to_put_date: Optional[date]
    yield_accrued_interest: Optional[float]
    yield_dirty_price: Optional[float]
    yield_price_basis: str
    yield_warning: str
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
    model_version: str
    error: str = ""
    iv_diagnostics: dict[str, object] = field(default_factory=dict)


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
    "iv_diagnostics",
    "yield_to_maturity",
    "yield_to_put",
    "yield_to_put_date",
    "yield_accrued_interest",
    "yield_dirty_price",
    "yield_price_basis",
    "yield_warning",
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
    "model_version",
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
        yield_values: dict[str, object] = {
            "yield_to_maturity": None,
            "yield_to_put": None,
            "yield_to_put_date": None,
            "yield_accrued_interest": None,
            "yield_dirty_price": None,
            "yield_price_basis": "",
            "yield_warning": "",
        }
        if market.bond_price is not None:
            try:
                market_yields = calculate_market_yields(
                    contract,
                    price=market.bond_price,
                    settlement_date=row.as_of_date,
                    same_day_settlement_assumed=True,
                    market=market,
                )
                ytm_detail = market_yields.get("yield_to_maturity_detail")
                ytm_detail = ytm_detail if isinstance(ytm_detail, dict) else {}
                put_date = market_yields.get("yield_to_put_date")
                yield_values = {
                    "yield_to_maturity": market_yields.get("yield_to_maturity"),
                    "yield_to_put": market_yields.get("yield_to_put"),
                    "yield_to_put_date": (
                        date.fromisoformat(str(put_date)) if put_date else None
                    ),
                    "yield_accrued_interest": ytm_detail.get("accrued_interest"),
                    "yield_dirty_price": ytm_detail.get("dirty_price"),
                    "yield_price_basis": str(ytm_detail.get("price_basis") or ""),
                    "yield_warning": str(market_yields.get("warning") or ""),
                }
            except Exception as exc:  # yield diagnostics must not discard a pricing row
                yield_values["yield_warning"] = f"yield calculation failed: {exc}"
        try:
            result = pricer.price(contract, market, assumptions)
            reported_assumptions = replace(
                assumptions,
                dividend_yield=float(result.diagnostics.details.get("effective_dividend_yield", assumptions.dividend_yield)),
            )
            warnings = list(result.diagnostics.warnings)
            if yield_values["yield_warning"]:
                warnings.append(f"yield: {yield_values['yield_warning']}")
            implied_volatility = None
            iv_diagnostics: dict[str, object] = {"status": "no_market_price"}
            if market.bond_price is not None:
                implied_volatility, iv_diagnostics = _implied_vol_with_refinement(
                    pricer, contract, market, assumptions,
                )
                if iv_diagnostics["status"] == "unavailable":
                    warnings.append(f"implied_volatility: {iv_diagnostics['message']}")
                elif iv_diagnostics["status"] == "refined":
                    warnings.append(
                        f"implied_volatility: solved at {iv_diagnostics['iv_steps']} steps "
                        f"and checked at {iv_diagnostics['validation_steps']} steps; "
                        "base lattice skipped the target price"
                    )
            results.append(
                _make_result_row(
                    row,
                    reported_assumptions,
                    assumption_source,
                    fair_value=result.fair_value,
                    parity=result.parity,
                    bond_floor=result.bond_floor,
                    cheapness=result.cheapness,
                    implied_volatility=implied_volatility,
                    iv_diagnostics=iv_diagnostics,
                    **yield_values,
                    output_currency=result.output_currency,
                    warnings=warnings,
                    model_version=f"{pricer.model_mode}:{MODEL_VERSION}",
                )
            )
        except Exception as exc:
            warning = f"pricing_error: {exc}"
            warnings = [warning]
            if yield_values["yield_warning"]:
                warnings.append(f"yield: {yield_values['yield_warning']}")
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
                    iv_diagnostics={"status": "pricing_error", "message": str(exc)},
                    **yield_values,
                    output_currency=contract.currency or contract.settlement_currency,
                    warnings=warnings,
                    model_version=f"{pricer.model_mode}:{MODEL_VERSION}",
                    error=str(exc),
                )
            )
    return results


def _implied_vol_with_refinement(
    pricer: PricingEngine, contract: Contract, market: MarketSnapshot, assumptions: Assumptions,
) -> tuple[Optional[float], dict[str, object]]:
    """Retry a skipped target on at most two finer grids, with an IV stability check."""
    target = market.bond_price
    try:
        iv = pricer.implied_vol(contract, market, assumptions, target_price=target)
        return iv, {"status": "solved", "iv_steps": int(assumptions.steps), "price_tolerance": 1e-6}
    except ImpliedVolError as exc:
        diagnostic: dict[str, object] = {
            "status": "unavailable", "code": exc.code, "message": str(exc),
            **exc.details,
        }
        if exc.code != "price_gap":
            return None, diagnostic
    except ValueError as exc:
        return None, {"status": "unavailable", "code": "solver_error", "message": str(exc),
                      "steps": int(assumptions.steps)}

    base_steps = int(assumptions.steps)
    first_steps = min(1000, max(500, base_steps * 2))
    validation_steps = min(1000, first_steps * 2)
    if first_steps <= base_steps or validation_steps <= first_steps:
        diagnostic["message"] += "; no finer validation grid is available"
        return None, diagnostic
    diagnostic["refinement_steps"] = [first_steps, validation_steps]
    refined_ivs: list[float] = []
    for step_count in (first_steps, validation_steps):
        try:
            refined_ivs.append(pricer.implied_vol(
                contract, market, replace(assumptions, steps=step_count), target_price=target,
            ))
        except ValueError as exc:
            diagnostic["refinement_failure"] = {
                "steps": step_count,
                "code": exc.code if isinstance(exc, ImpliedVolError) else "solver_error",
                "message": str(exc),
                **(exc.details if isinstance(exc, ImpliedVolError) else {}),
            }
            diagnostic["message"] += f"; finer-grid check failed at {step_count} steps: {exc}"
            return None, diagnostic
    first_iv, validation_iv = refined_ivs
    iv_difference = abs(first_iv - validation_iv)
    diagnostic.update({
        "iv_steps": first_steps, "validation_steps": validation_steps,
        "validation_iv": validation_iv, "iv_difference": iv_difference,
        "stability_tolerance": 0.001,
    })
    if iv_difference > 0.001:
        diagnostic["message"] += "; refined IV changes by more than 0.1 percentage point"
        return None, diagnostic
    diagnostic["status"] = "refined"
    return first_iv, diagnostic


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
    iv_diagnostics: dict[str, object],
    yield_to_maturity: Optional[float],
    yield_to_put: Optional[float],
    yield_to_put_date: Optional[date],
    yield_accrued_interest: Optional[float],
    yield_dirty_price: Optional[float],
    yield_price_basis: str,
    yield_warning: str,
    output_currency: str,
    warnings: list[str],
    model_version: str,
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
        iv_diagnostics=iv_diagnostics,
        yield_to_maturity=yield_to_maturity,
        yield_to_put=yield_to_put,
        yield_to_put_date=yield_to_put_date,
        yield_accrued_interest=yield_accrued_interest,
        yield_dirty_price=yield_dirty_price,
        yield_price_basis=yield_price_basis,
        yield_warning=yield_warning,
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
        model_version=model_version,
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
        "iv_diagnostics": json.dumps(result.iv_diagnostics, sort_keys=True),
        "yield_to_maturity": _format_optional_float(result.yield_to_maturity),
        "yield_to_put": _format_optional_float(result.yield_to_put),
        "yield_to_put_date": (
            result.yield_to_put_date.isoformat() if result.yield_to_put_date else ""
        ),
        "yield_accrued_interest": _format_optional_float(result.yield_accrued_interest),
        "yield_dirty_price": _format_optional_float(result.yield_dirty_price),
        "yield_price_basis": result.yield_price_basis,
        "yield_warning": result.yield_warning,
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
        "model_version": result.model_version,
        "error": result.error,
    }


def _format_optional_float(value: Optional[float]) -> str:
    if value is None:
        return ""
    return f"{value:.10g}"
