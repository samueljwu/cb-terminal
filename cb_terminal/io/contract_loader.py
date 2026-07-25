"""JSON contract loading for normalized Phase 1 deal files."""

from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path
from typing import Any, Dict, Optional

from cb_terminal.domain import CallSchedule, Contract, ConversionTerms, CouponSchedule, FXConvention, PutSchedule
from cb_terminal.validation import require_positive


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    return date.fromisoformat(value)


def _as_decimal_percent(value: Any, *, bps: bool = False) -> float:
    """Convert common JSON percent conventions into decimals.

    The project sample stores vol/rates as 35.0, 3.5, 250.0 bps.  Contract
    coupon_rate is already a percent-like number in existing sample contracts.  This helper is
    intentionally conservative and only used for assumption convenience, not for
    core terms where explicit decimals are expected.
    """

    if value is None:
        return 0.0
    number = float(value)
    return number / 10000.0 if bps else number / 100.0


def _optional_finite_float(value: Any, field: str) -> Optional[float]:
    if value in (None, ""):
        return None
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    return number


def _quoted_yield_and_frequency(
    yield_value: Any,
    frequency_value: Any,
    *,
    yield_field: str,
    frequency_field: str,
) -> tuple[Optional[float], Optional[int]]:
    """Load an optional quoted percent yield and its compounding frequency."""

    quoted_percent = _optional_finite_float(yield_value, yield_field)
    if quoted_percent is None and frequency_value in (None, ""):
        return None, None
    if quoted_percent is None:
        raise ValueError(f"{frequency_field} requires {yield_field}")
    if not -100.0 < quoted_percent <= 100.0:
        raise ValueError(f"{yield_field} must be greater than -100% and no more than 100%")
    if frequency_value in (None, ""):
        raise ValueError(f"{yield_field} requires {frequency_field}")
    frequency_number = _optional_finite_float(frequency_value, frequency_field)
    if frequency_number is None or not frequency_number.is_integer() or not 1 <= frequency_number <= 365:
        raise ValueError(f"{frequency_field} must be an integer between 1 and 365")
    return quoted_percent / 100.0, int(frequency_number)


def _parse_fx_convention(value: Any) -> Optional[FXConvention]:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip().upper().replace(" ", "_").replace("-", "_")
    try:
        return FXConvention[text]
    except KeyError:
        return FXConvention(text)


def _parse_fixed_fx_convention(conversion: Dict[str, Any], contract_currency: str, stock_currency: str) -> Optional[FXConvention]:
    explicit = conversion.get("fixed_exchange_rate_convention")
    if explicit is not None:
        return _parse_fx_convention(explicit)
    units = conversion.get("fixed_exchange_rate_units")
    if units is None or str(units).strip() == "":
        return None
    text = " ".join(str(units).strip().upper().replace("/", " per ").split())
    if " PER " not in text:
        return _parse_fx_convention(units)
    numerator, denominator = [part.strip() for part in text.split(" PER ", 1)]
    cb = (contract_currency or "").strip().upper()
    stock = (stock_currency or "").strip().upper()
    if numerator == cb and denominator == stock:
        return FXConvention.CB_PER_STOCK
    if numerator == stock and denominator == cb:
        return FXConvention.STOCK_PER_CB
    raise ValueError(
        f"fixed_exchange_rate_units {units!r} are incompatible with contract currency {cb!r} and stock currency {stock!r}"
    )


def _fixed_fx_rate_in_standard_convention(conversion: Dict[str, Any], contract_currency: str, stock_currency: str) -> Optional[float]:
    """Return fixed FX as stock currency per CB currency.

    Prospectuses may print either "TWD 31.951 = USD 1.00" or the inverse.
    Internally we standardize to STOCK_PER_CB and invert legacy/inverse units at
    the loader boundary so downstream pricing and UI never need a convention
    switch.
    """

    if conversion.get("fixed_exchange_rate") is None:
        return None
    rate = float(conversion["fixed_exchange_rate"])
    convention = _parse_fixed_fx_convention(conversion, contract_currency, stock_currency)
    if convention is FXConvention.CB_PER_STOCK:
        require_positive("fixed_exchange_rate", rate)
        return 1.0 / rate
    return rate


def contract_from_dict(raw: Dict[str, Any]) -> Contract:
    issuer = raw.get("issuer", {})
    bond = raw.get("bond", {})
    redemption = raw.get("redemption", {})
    conversion = raw.get("conversion", {})
    pricing_date = _parse_date(bond.get("pricing_date") or raw.get("as_of_date"))
    maturity_date = _parse_date(bond.get("maturity_date"))
    if pricing_date is None:
        raise ValueError("bond.pricing_date is required; refusing to substitute the current date")
    if maturity_date is None:
        raise ValueError("bond.maturity_date is required; refusing to substitute the current date")
    if bond.get("coupon_rate") is None:
        raise ValueError("bond.coupon_rate is required; refusing to treat a missing coupon as zero")
    if bond.get("coupon_frequency") is None:
        raise ValueError("bond.coupon_frequency is required; refusing to infer a payment schedule")

    coupon_rate_percent = float(bond["coupon_rate"])
    coupon_frequency_value = float(bond["coupon_frequency"])
    if coupon_rate_percent < 0:
        raise ValueError("bond.coupon_rate must be non-negative")
    if coupon_frequency_value < 0 or not coupon_frequency_value.is_integer():
        raise ValueError("bond.coupon_frequency must be a non-negative integer")
    coupon_frequency = int(coupon_frequency_value)
    if coupon_rate_percent > 0 and coupon_frequency == 0:
        raise ValueError("a positive bond.coupon_rate requires bond.coupon_frequency greater than zero")

    face = float(bond.get("pricing_face", bond.get("denomination", 100.0)))
    issue_price = float(bond.get("issue_price", face))
    brokerage = _optional_finite_float(bond.get("brokerage"), "bond.brokerage")
    if brokerage is not None and not 0.0 <= brokerage <= 100.0:
        raise ValueError("bond.brokerage must be between 0 and 100 percentage points")
    investor_offer_price = _optional_finite_float(
        bond.get("investor_offer_price"),
        "bond.investor_offer_price",
    )
    if investor_offer_price is not None and brokerage is None:
        raise ValueError(
            "bond.investor_offer_price requires bond.brokerage so it can equal "
            "bond.issue_price + bond.brokerage"
        )
    if brokerage is not None:
        expected_offer_price = issue_price + brokerage
        if investor_offer_price is None:
            investor_offer_price = expected_offer_price
        elif not math.isclose(investor_offer_price, expected_offer_price, rel_tol=0.0, abs_tol=1e-6):
            raise ValueError("bond.investor_offer_price must equal bond.issue_price + bond.brokerage")
    if investor_offer_price is not None:
        require_positive("investor_offer_price", investor_offer_price)
    yield_to_maturity, yield_to_maturity_frequency = _quoted_yield_and_frequency(
        redemption.get("yield_to_maturity"),
        redemption.get("yield_to_maturity_frequency"),
        yield_field="redemption.yield_to_maturity",
        frequency_field="redemption.yield_to_maturity_frequency",
    )
    conversion_price = float(conversion.get("initial_conversion_price"))
    contract_currency = bond.get("currency", "")
    stock_currency = bond.get("stock_currency", "")
    require_positive("face", face)
    require_positive("issue_price", issue_price)
    require_positive("conversion_price", conversion_price)

    coupon = CouponSchedule(
        annual_rate=_as_decimal_percent(coupon_rate_percent),
        frequency=coupon_frequency,
    )
    closing_date = _parse_date(bond.get("closing_date"))
    conversion_start_date = _parse_date(conversion.get("start_date"))
    conversion_end_date = _parse_date(conversion.get("end_date"))
    conversion_windows: list[tuple[date, date]] = []
    previous_window_end: date | None = None
    for index, window in enumerate(conversion.get("windows") or []):
        if not isinstance(window, dict):
            raise ValueError(f"conversion.windows[{index}] must be an object")
        window_start = _parse_date(window.get("start_date"))
        window_end = _parse_date(window.get("end_date"))
        if window_start is None or window_end is None or window_start > window_end:
            raise ValueError(f"conversion.windows[{index}] requires ordered start_date and end_date")
        if window_start < pricing_date or (closing_date is not None and window_start < closing_date):
            raise ValueError(f"conversion.windows[{index}] cannot begin before pricing/issuance")
        if window_end > maturity_date:
            raise ValueError(f"conversion.windows[{index}] cannot end after maturity")
        if conversion_start_date is not None and window_start < conversion_start_date:
            raise ValueError(f"conversion.windows[{index}] begins before conversion.start_date")
        if conversion_end_date is not None and window_end > conversion_end_date:
            raise ValueError(f"conversion.windows[{index}] ends after conversion.end_date")
        if previous_window_end is not None and window_start <= previous_window_end:
            raise ValueError("conversion.windows must be ordered and non-overlapping")
        conversion_windows.append((window_start, window_end))
        previous_window_end = window_end
    conversion_terms = ConversionTerms(
        underlying_ticker=conversion.get("underlying_ticker", issuer.get("ticker", "")),
        conversion_price=conversion_price,
        reference_share_price=(
            float(conversion["reference_share_price"])
            if conversion.get("reference_share_price") is not None
            else None
        ),
        start_date=conversion_start_date,
        end_date=conversion_end_date,
        fixed_fx_rate=_fixed_fx_rate_in_standard_convention(conversion, contract_currency, stock_currency),
        fixed_fx_convention=(FXConvention.STOCK_PER_CB if conversion.get("fixed_exchange_rate") is not None else None),
        windows=tuple(conversion_windows),
    )

    calls = []
    for item in raw.get("calls", []):
        calls.append(
            CallSchedule(
                call_type=item.get("type", "call"),
                model_type=item.get("model_type", "soft_call"),
                start_date=_parse_date(item.get("start_date")),
                start_date_calendar_status=str(item.get("start_date_calendar_status") or ""),
                price=float(item.get("price", face)),
                trigger_ratio=(
                    float(item["trigger_ratio"]) if item.get("trigger_ratio") is not None else None
                ),
                trigger_days=(int(item["trigger_days"]) if item.get("trigger_days") is not None else None),
                trigger_window_days=(int(item["trigger_window_days"]) if item.get("trigger_window_days") is not None else None),
                last_observation_max_days_before_notice=(
                    int(item["last_observation_max_days_before_notice"])
                    if item.get("last_observation_max_days_before_notice") is not None
                    else None
                ),
                observation_rule=str(item.get("observation_rule") or ""),
                trigger_basis=str(item.get("trigger_basis") or "conversion_price"),
                price_rule=str(item.get("price_rule") or ""),
                description=item.get("description", ""),
            )
        )

    puts = []
    for index, item in enumerate(raw.get("puts", [])):
        put_model_type = item.get("model_type", "event_put")
        if put_model_type != "scheduled_put" and (
            item.get("yield_to_put") not in (None, "")
            or item.get("yield_to_put_frequency") not in (None, "")
        ):
            raise ValueError(
                f"puts.{index}.yield_to_put is only valid for a scheduled_put"
            )
        yield_to_put, yield_to_put_frequency = _quoted_yield_and_frequency(
            item.get("yield_to_put"),
            item.get("yield_to_put_frequency"),
            yield_field=f"puts.{index}.yield_to_put",
            frequency_field=f"puts.{index}.yield_to_put_frequency",
        )
        puts.append(
            PutSchedule(
                put_type=item.get("type", "put"),
                model_type=put_model_type,
                date=_parse_date(item.get("date") or item.get("start_date")),
                price=float(item.get("price", face)),
                description=item.get("description", ""),
                yield_to_put=yield_to_put,
                yield_to_put_frequency=yield_to_put_frequency,
            )
        )

    contract = Contract(
        id=raw.get("id", ""),
        issuer=issuer.get("name", ""),
        description=bond.get("description", ""),
        currency=contract_currency,
        settlement_currency=bond.get("settlement_currency", contract_currency),
        stock_currency=stock_currency,
        face=face,
        issue_price=issue_price,
        maturity_price=float(redemption.get("maturity_price", face)),
        pricing_date=pricing_date,
        maturity_date=maturity_date,
        coupon=coupon,
        conversion=conversion_terms,
        economic_currency=bond.get("economic_currency", contract_currency),
        puts=puts,
        calls=calls,
        source={
            "source_file": raw.get("source_file"),
            "source_type": raw.get("source_type"),
            "status": raw.get("status"),
        },
        metadata={
            "instrument": raw.get("instrument", {}),
            "guarantor": raw.get("guarantor"),
            "exchangeable_terms": raw.get("exchangeable_terms"),
            "ratings": issuer.get("ratings", {}),
            "model_notes": raw.get("model_notes", []),
            "term_extensions": {
                "economic_currency": bond.get("economic_currency", contract_currency),
                "denomination_increment": bond.get("denomination_increment"),
                "brokerage": brokerage,
                "investor_offer_price": investor_offer_price,
                "yield_to_maturity_percent": redemption.get("yield_to_maturity"),
                "yield_to_maturity_frequency": yield_to_maturity_frequency,
                "initial_settlement_exchange_rate": conversion.get("initial_settlement_exchange_rate"),
                "initial_settlement_exchange_rate_units": conversion.get("initial_settlement_exchange_rate_units"),
                "conversion_start_date_rule": conversion.get("start_date_rule"),
                "conversion_end_date_rule": conversion.get("end_date_rule"),
                "conversion_calendar_status": conversion.get("calendar_status"),
                "conditional_early_conversion_start_date": conversion.get("conditional_early_start_date"),
                "conditional_early_conversion_start_rule": conversion.get("conditional_early_start_rule"),
                "conditional_early_conversion_conditions": conversion.get("conditional_early_conditions") or [],
            },
            "raw_keys": sorted(raw.keys()),
        },
        brokerage=brokerage,
        investor_offer_price=investor_offer_price,
        yield_to_maturity=yield_to_maturity,
        yield_to_maturity_frequency=yield_to_maturity_frequency,
        issue_date=closing_date or pricing_date,
        day_count=str(bond.get("day_count") or ""),
        quote_convention=str(raw.get("quote_convention") or ""),
    )
    require_positive("maturity_years", contract.maturity_years_from_pricing)
    return contract


def loads_contract_json(text: str) -> Contract:
    return contract_from_dict(json.loads(text))


def load_contract_json(path: str | Path) -> Contract:
    with Path(path).open("r", encoding="utf-8") as handle:
        return contract_from_dict(json.load(handle))
