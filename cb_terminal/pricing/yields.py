"""Cash-flow yields for issuance checks and traded convertible-bond quotes.

The convertible option, issuer calls, and event puts are deliberately excluded:
YTM and yield-to-put are conventional promised-cash-flow yields.  Prospectus
quotes remain source terms; calculations in this module are independent values
used to reconcile those terms and to analyse observed market prices.
"""

from __future__ import annotations

import calendar
import math
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Mapping, Optional

from cb_terminal.domain import Contract, PutSchedule


DEFAULT_YIELD_FREQUENCY = 2
DEFAULT_DAY_COUNT = "ACT/365.25"
YIELD_MATCH_TOLERANCE_BPS = 1.0
NEGLIGIBLE_YTM_CUTOFF = 0.0001  # 1 basis point in annual-decimal yield.


@dataclass(frozen=True)
class YieldCashFlow:
    payment_date: date
    amount: float


@dataclass(frozen=True)
class YieldCalculation:
    """One solved nominal annual yield, expressed as a decimal."""

    annual_yield: Optional[float]
    frequency: int
    settlement_date: date
    target_date: date
    price: float
    dirty_price: float
    accrued_interest: float
    target_price: float
    price_basis: str
    day_count: str
    status: str
    message: str = ""
    cash_flows: tuple[YieldCashFlow, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "annual_yield": self.annual_yield,
            "annual_yield_percent": (
                self.annual_yield * 100.0 if self.annual_yield is not None else None
            ),
            "frequency": self.frequency,
            "settlement_date": self.settlement_date.isoformat(),
            "target_date": self.target_date.isoformat(),
            "price": self.price,
            "dirty_price": self.dirty_price,
            "accrued_interest": self.accrued_interest,
            "target_price": self.target_price,
            "price_basis": self.price_basis,
            "day_count": self.day_count,
            "status": self.status,
            "message": self.message,
            "cash_flows": [
                {"date": cash_flow.payment_date.isoformat(), "amount": cash_flow.amount}
                for cash_flow in self.cash_flows
            ],
        }


def calculate_yield_to_maturity(
    contract: Contract,
    *,
    price: float,
    settlement_date: date,
    frequency: int | None = None,
) -> YieldCalculation:
    """Calculate promised yield to contractual maturity from an observed price."""

    selected_frequency = _yield_frequency(
        frequency,
        contract.yield_to_maturity_frequency,
        contract.coupon.frequency,
    )
    return _calculate_contract_yield(
        contract,
        price=price,
        settlement_date=settlement_date,
        target_date=contract.maturity_date,
        target_price=contract.maturity_price,
        frequency=selected_frequency,
        target_kind="maturity",
    )


def calculate_yield_to_put(
    contract: Contract,
    put: PutSchedule,
    *,
    price: float,
    settlement_date: date,
    frequency: int | None = None,
) -> YieldCalculation:
    """Calculate yield to one deterministic scheduled holder put."""

    if put.model_type != "scheduled_put" or put.date is None:
        return _unavailable_calculation(
            price=price,
            settlement_date=settlement_date,
            target_date=put.date or settlement_date,
            target_price=put.price,
            frequency=_yield_frequency(
                frequency,
                put.yield_to_put_frequency,
                contract.yield_to_maturity_frequency,
                contract.coupon.frequency,
            ),
            day_count=_normalized_day_count(contract.day_count),
            message="yield to put requires a dated scheduled holder put",
        )
    selected_frequency = _yield_frequency(
        frequency,
        put.yield_to_put_frequency,
        contract.yield_to_maturity_frequency,
        contract.coupon.frequency,
    )
    return _calculate_contract_yield(
        contract,
        price=price,
        settlement_date=settlement_date,
        target_date=put.date,
        target_price=put.price,
        frequency=selected_frequency,
        target_kind="put",
    )


def calculate_market_yields(
    contract: Contract,
    *,
    price: float | None,
    settlement_date: date,
    same_day_settlement_assumed: bool = False,
) -> dict[str, Any]:
    """Return market YTM plus every future deterministic yield-to-put.

    The earliest unexpired scheduled put is exposed as ``yield_to_put`` for the
    summary/table.  All future puts are retained in ``yield_to_puts``.
    """

    if price is None:
        return {
            "settlement_date": settlement_date.isoformat(),
            "yield_to_maturity": None,
            "yield_to_maturity_detail": None,
            "yield_to_put": None,
            "yield_to_put_date": None,
            "yield_to_puts": [],
            "same_day_settlement_assumed": same_day_settlement_assumed,
            "warning": "market bond price is required for traded yields",
        }

    ytm = calculate_yield_to_maturity(
        contract,
        price=float(price),
        settlement_date=settlement_date,
    )
    future_puts = sorted(
        (
            put
            for put in contract.puts
            if put.model_type == "scheduled_put"
            and put.date is not None
            and settlement_date < put.date <= contract.maturity_date
        ),
        key=lambda put: put.date or contract.maturity_date,
    )
    put_calculations = [
        calculate_yield_to_put(
            contract,
            put,
            price=float(price),
            settlement_date=settlement_date,
        )
        for put in future_puts
    ]
    primary_put = put_calculations[0] if put_calculations else None
    warning_messages = (
        [
            "quote as-of date used as same-day settlement; "
            "contractual settlement lag/calendar unavailable"
        ]
        if same_day_settlement_assumed
        else []
    )
    warning_messages.extend(
        calculation.message
        for calculation in (ytm, *put_calculations)
        if calculation.message
    )
    return {
        "settlement_date": settlement_date.isoformat(),
        "yield_to_maturity": ytm.annual_yield,
        "yield_to_maturity_detail": ytm.to_payload(),
        "yield_to_put": primary_put.annual_yield if primary_put else None,
        "yield_to_put_date": primary_put.target_date.isoformat() if primary_put else None,
        "yield_to_puts": [calculation.to_payload() for calculation in put_calculations],
        "same_day_settlement_assumed": same_day_settlement_assumed,
        "warning": "; ".join(dict.fromkeys(warning_messages)),
    }


def issuance_yield_checks(
    contract: Contract,
    *,
    tolerance_bps: float = YIELD_MATCH_TOLERANCE_BPS,
) -> dict[str, Any]:
    """Reconcile source-quoted issuance yields without replacing the quotes."""

    settlement_date = contract.issue_date or contract.pricing_date
    ytm = calculate_yield_to_maturity(
        contract,
        price=contract.issue_price,
        settlement_date=settlement_date,
    )
    ytm_check = _comparison_payload(
        quoted_yield=contract.yield_to_maturity,
        calculated=ytm,
        tolerance_bps=tolerance_bps,
    )

    put_checks = []
    for index, put in enumerate(contract.puts):
        if (
            put.model_type != "scheduled_put"
            or put.date is None
            or put.date <= settlement_date
        ):
            continue
        calculation = calculate_yield_to_put(
            contract,
            put,
            price=contract.issue_price,
            settlement_date=settlement_date,
        )
        put_checks.append(
            {
                "put_index": index,
                "put_date": put.date.isoformat(),
                **_comparison_payload(
                    quoted_yield=put.yield_to_put,
                    calculated=calculation,
                    tolerance_bps=tolerance_bps,
                ),
            }
        )
    return {
        "settlement_date": settlement_date.isoformat(),
        "price": contract.issue_price,
        "price_source": "gross_issue_price",
        "yield_to_maturity": ytm_check,
        "yield_to_puts": put_checks,
    }


def _comparison_payload(
    *,
    quoted_yield: float | None,
    calculated: YieldCalculation,
    tolerance_bps: float,
) -> dict[str, Any]:
    calculated_yield = calculated.annual_yield
    difference_bps = (
        (calculated_yield - quoted_yield) * 10_000.0
        if calculated_yield is not None and quoted_yield is not None
        else None
    )
    if calculated_yield is None:
        status = "unavailable"
    elif quoted_yield is None:
        status = "calculated_only"
    elif abs(float(difference_bps)) <= tolerance_bps:
        status = "match"
    else:
        status = "mismatch"
    return {
        "quoted_yield": quoted_yield,
        "quoted_yield_percent": quoted_yield * 100.0 if quoted_yield is not None else None,
        "calculated_yield": calculated_yield,
        "calculated_yield_percent": (
            calculated_yield * 100.0 if calculated_yield is not None else None
        ),
        "difference_bps": difference_bps,
        "tolerance_bps": tolerance_bps,
        "status": status,
        "calculation": calculated.to_payload(),
    }


def _calculate_contract_yield(
    contract: Contract,
    *,
    price: float,
    settlement_date: date,
    target_date: date,
    target_price: float,
    frequency: int,
    target_kind: str,
) -> YieldCalculation:
    day_count, day_count_warning = _day_count_details(contract.day_count)
    price_basis, basis_warning = _price_basis(contract.quote_convention)
    if not math.isfinite(price) or price <= 0.0:
        return _unavailable_calculation(
            price=price,
            settlement_date=settlement_date,
            target_date=target_date,
            target_price=target_price,
            frequency=frequency,
            day_count=day_count,
            message="yield requires a positive finite bond price",
        )
    if settlement_date >= target_date:
        return _unavailable_calculation(
            price=price,
            settlement_date=settlement_date,
            target_date=target_date,
            target_price=target_price,
            frequency=frequency,
            day_count=day_count,
            message="yield target must be after settlement",
        )

    issue_date = contract.issue_date or contract.pricing_date
    coupon_dates = _coupon_schedule(
        issue_date=issue_date,
        maturity_date=contract.maturity_date,
        frequency=contract.coupon.frequency,
    )
    coupon_amount = (
        contract.face * contract.coupon.annual_rate / contract.coupon.frequency
        if contract.coupon.frequency > 0
        else 0.0
    )
    accrued_interest = (
        _accrued_interest(
            settlement_date,
            coupon_dates,
            coupon_amount,
            issue_date=issue_date,
            day_count=day_count,
        )
        if coupon_amount
        else 0.0
    )
    dirty_price = price if price_basis == "dirty" else price + accrued_interest

    amounts_by_date: dict[date, float] = {}
    for coupon_date in coupon_dates:
        if settlement_date < coupon_date <= target_date:
            amounts_by_date[coupon_date] = amounts_by_date.get(coupon_date, 0.0) + coupon_amount
    amounts_by_date[target_date] = amounts_by_date.get(target_date, 0.0) + target_price
    if target_kind == "put" and coupon_amount and target_date not in coupon_dates:
        # Scheduled holder puts are normally paid with interest accrued to the
        # redemption date.  The normalized schema does not yet carry an
        # include/exclude-accrued flag, so make the assumption visible.
        target_accrued = _accrued_interest(
            target_date,
            coupon_dates,
            coupon_amount,
            issue_date=issue_date,
            day_count=day_count,
        )
        amounts_by_date[target_date] += target_accrued
    cash_flows = tuple(
        YieldCashFlow(payment_date=payment_date, amount=amount)
        for payment_date, amount in sorted(amounts_by_date.items())
        if amount > 0.0
    )
    if not cash_flows:
        return _unavailable_calculation(
            price=price,
            settlement_date=settlement_date,
            target_date=target_date,
            target_price=target_price,
            frequency=frequency,
            day_count=day_count,
            message="no positive promised cash flows remain after settlement",
        )

    try:
        solved = _solve_nominal_yield(
            dirty_price=dirty_price,
            settlement_date=settlement_date,
            cash_flows=cash_flows,
            frequency=frequency,
            day_count=day_count,
        )
    except ValueError as exc:
        return YieldCalculation(
            annual_yield=None,
            frequency=frequency,
            settlement_date=settlement_date,
            target_date=target_date,
            price=price,
            dirty_price=dirty_price,
            accrued_interest=accrued_interest,
            target_price=target_price,
            price_basis=price_basis,
            day_count=day_count,
            status="unavailable",
            message=str(exc),
            cash_flows=cash_flows,
        )

    if target_kind == "maturity" and abs(solved) < NEGLIGIBLE_YTM_CUTOFF:
        solved = 0.0

    warnings = []
    if coupon_amount:
        warnings.append(
            "coupon dates, stubs, and end-of-month treatment inferred backward from maturity"
        )
    if basis_warning and coupon_amount:
        warnings.append(basis_warning)
    if day_count_warning:
        warnings.append(day_count_warning)
    if target_kind == "put" and coupon_amount and target_date not in coupon_dates:
        warnings.append("put payoff assumed to include accrued coupon interest")
    return YieldCalculation(
        annual_yield=solved,
        frequency=frequency,
        settlement_date=settlement_date,
        target_date=target_date,
        price=price,
        dirty_price=dirty_price,
        accrued_interest=accrued_interest,
        target_price=target_price,
        price_basis=price_basis,
        day_count=day_count,
        status="calculated_with_assumptions" if warnings else "calculated",
        message="; ".join(warnings),
        cash_flows=cash_flows,
    )


def _solve_nominal_yield(
    *,
    dirty_price: float,
    settlement_date: date,
    cash_flows: tuple[YieldCashFlow, ...],
    frequency: int,
    day_count: str,
) -> float:
    if frequency <= 0:
        raise ValueError("yield compounding frequency must be positive")
    if not math.isfinite(dirty_price) or dirty_price <= 0.0:
        raise ValueError("dirty price must be positive and finite")

    exponents = [
        frequency * _year_fraction(settlement_date, cash_flow.payment_date, day_count)
        for cash_flow in cash_flows
    ]
    if not exponents or any(exponent <= 0.0 for exponent in exponents):
        raise ValueError("all yield cash flows must occur after settlement")

    def present_value(annual_yield: float) -> float:
        base = 1.0 + annual_yield / frequency
        if base <= 0.0:
            return math.inf
        log_base = math.log(base)
        total = 0.0
        for cash_flow, exponent in zip(cash_flows, exponents):
            log_discount = exponent * log_base
            if log_discount < -700.0:
                return math.inf
            if log_discount > 700.0:
                continue
            total += cash_flow.amount * math.exp(-log_discount)
            if not math.isfinite(total):
                return math.inf
        return total

    low = -float(frequency) + 1e-12
    high = 1.0
    low_error = present_value(low) - dirty_price
    high_error = present_value(high) - dirty_price
    while high_error > 0.0 and high < 1_000_000.0:
        high *= 2.0
        high_error = present_value(high) - dirty_price
    if not math.isfinite(low_error) and high_error <= 0.0:
        low_error = math.inf
    if low_error < 0.0 or high_error > 0.0:
        raise ValueError("could not bracket a unique promised-cash-flow yield")

    for _ in range(220):
        midpoint = (low + high) / 2.0
        error = present_value(midpoint) - dirty_price
        if abs(error) <= max(1e-12, dirty_price * 1e-13):
            return midpoint
        if error > 0.0:
            low = midpoint
        else:
            high = midpoint
    return (low + high) / 2.0


def _coupon_schedule(
    *,
    issue_date: date,
    maturity_date: date,
    frequency: int,
) -> tuple[date, ...]:
    if frequency <= 0 or maturity_date <= issue_date:
        return ()
    dates: list[date] = []
    if 12 % frequency == 0:
        months_per_coupon = 12 // frequency
        period = 0
        while True:
            cursor = _shift_months(
                maturity_date,
                -months_per_coupon * period,
                preserve_end_of_month=True,
            )
            if cursor <= issue_date:
                break
            dates.append(cursor)
            period += 1
    else:
        days_per_coupon = 365.25 / frequency
        index = 0
        while True:
            cursor = maturity_date - timedelta(days=int(round(days_per_coupon * index)))
            if cursor <= issue_date:
                break
            dates.append(cursor)
            index += 1
    return tuple(sorted(set(dates)))


def _accrued_interest(
    settlement_date: date,
    coupon_dates: tuple[date, ...],
    coupon_amount: float,
    *,
    issue_date: date,
    day_count: str,
) -> float:
    if coupon_amount <= 0.0 or settlement_date <= issue_date or not coupon_dates:
        return 0.0
    next_coupon = next(
        (coupon_date for coupon_date in coupon_dates if coupon_date > settlement_date),
        None,
    )
    if next_coupon is None:
        return 0.0
    previous_candidates = [
        coupon_date for coupon_date in coupon_dates if coupon_date <= settlement_date
    ]
    previous_coupon = previous_candidates[-1] if previous_candidates else issue_date
    full_period = _year_fraction(previous_coupon, next_coupon, day_count)
    if full_period <= 0.0:
        return 0.0
    elapsed = _year_fraction(previous_coupon, settlement_date, day_count)
    fraction = min(1.0, max(0.0, elapsed / full_period))
    return coupon_amount * fraction


def _yield_frequency(*values: int | None) -> int:
    for value in values:
        if value is None:
            continue
        try:
            frequency = int(value)
        except (TypeError, ValueError):
            continue
        if frequency > 0:
            return frequency
    return DEFAULT_YIELD_FREQUENCY


def _price_basis(raw: str) -> tuple[str, str]:
    normalized = str(raw or "").strip().lower()
    if "dirty" in normalized or "including accrued" in normalized:
        return "dirty", ""
    if "clean" in normalized or "excluding accrued" in normalized or "without accrued" in normalized:
        return "clean", ""
    return "clean", "clean market-price convention assumed"


def _normalized_day_count(raw: str) -> str:
    return _day_count_details(raw)[0]


def _day_count_is_fallback(raw: str) -> bool:
    canonical, warning = _day_count_details(raw)
    return canonical == DEFAULT_DAY_COUNT and bool(warning)


def _day_count_details(raw: str) -> tuple[str, str]:
    source = str(raw or "").strip()
    normalized = re.sub(
        r"[^A-Z0-9/.]+",
        "",
        source.upper().replace("ACTUAL", "ACT"),
    )
    if normalized in {"ACT/360", "A/360"}:
        return "ACT/360", ""
    if normalized in {
        "ACT/365",
        "ACT/365F",
        "ACT/365FIXED",
        "A/365",
        "A/365F",
    }:
        return "ACT/365", ""
    if normalized in {"ACT/ACTISDA", "ISDA"}:
        return "ACT/ACT", ""
    if normalized == "ACT/ACT":
        return (
            "ACT/ACT",
            "ambiguous ACT/ACT treated as ISDA-style calendar-year fractions",
        )
    if normalized in {"ACT/ACTICMA", "ICMA"}:
        return (
            "ACT/ACT",
            "ACT/ACT ICMA approximated with ISDA-style calendar-year fractions",
        )
    if normalized in {
        "30/360",
        "30U/360",
        "30/360US",
        "BOND",
        "BONDBASIS",
    }:
        return "30/360", ""
    if normalized in {"30E/360", "EUROPEAN30/360", "EUROBOND30/360"}:
        return "30E/360", ""
    if normalized == "30E/360ISDA":
        return (
            "30E/360",
            "30E/360 ISDA approximated with the European 30E/360 rule",
        )
    if normalized in {"ACT/365.25", "A/365.25"}:
        return "ACT/365.25", ""
    if normalized in {"", "NEEDSREVIEW", "UNKNOWN", "N/A", "NA", "NONE"}:
        return DEFAULT_DAY_COUNT, "ACT/365.25 day-count fallback used"
    return (
        DEFAULT_DAY_COUNT,
        f"unrecognized day-count convention {source!r}; ACT/365.25 fallback used",
    )


def _year_fraction(start: date, end: date, day_count: str) -> float:
    if end <= start:
        return 0.0
    if day_count == "ACT/360":
        return (end - start).days / 360.0
    if day_count == "ACT/365":
        return (end - start).days / 365.0
    if day_count == "30/360":
        d1 = min(start.day, 30)
        d2 = end.day
        if d1 == 30:
            d2 = min(d2, 30)
        return ((end.year - start.year) * 360 + (end.month - start.month) * 30 + d2 - d1) / 360.0
    if day_count == "30E/360":
        d1 = min(start.day, 30)
        d2 = min(end.day, 30)
        return ((end.year - start.year) * 360 + (end.month - start.month) * 30 + d2 - d1) / 360.0
    if day_count == "ACT/ACT":
        total = 0.0
        cursor = start
        while cursor < end:
            boundary = min(end, date(cursor.year + 1, 1, 1))
            denominator = 366.0 if calendar.isleap(cursor.year) else 365.0
            total += (boundary - cursor).days / denominator
            cursor = boundary
        return total
    return (end - start).days / 365.25


def _shift_months(
    value: date,
    months: int,
    *,
    preserve_end_of_month: bool = False,
) -> date:
    month_index = value.year * 12 + value.month - 1 + months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    target_month_end = calendar.monthrange(year, month)[1]
    source_is_month_end = value.day == calendar.monthrange(value.year, value.month)[1]
    day = (
        target_month_end
        if preserve_end_of_month and source_is_month_end
        else min(value.day, target_month_end)
    )
    return date(year, month, day)


def _unavailable_calculation(
    *,
    price: float,
    settlement_date: date,
    target_date: date,
    target_price: float,
    frequency: int,
    day_count: str,
    message: str,
) -> YieldCalculation:
    return YieldCalculation(
        annual_yield=None,
        frequency=frequency,
        settlement_date=settlement_date,
        target_date=target_date,
        price=price,
        dirty_price=price,
        accrued_interest=0.0,
        target_price=target_price,
        price_basis="unknown",
        day_count=day_count,
        status="unavailable",
        message=message,
    )
