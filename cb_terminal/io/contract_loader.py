"""JSON contract loading for normalized Phase 1 deal files."""

from __future__ import annotations

import json
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

    face = float(bond.get("pricing_face", bond.get("denomination", 100.0)))
    conversion_price = float(conversion.get("initial_conversion_price"))
    contract_currency = bond.get("currency", "")
    stock_currency = bond.get("stock_currency", "")
    require_positive("face", face)
    require_positive("conversion_price", conversion_price)

    coupon = CouponSchedule(
        annual_rate=_as_decimal_percent(bond.get("coupon_rate", 0.0)),
        frequency=int(bond.get("coupon_frequency", 0) or 0),
    )
    conversion_terms = ConversionTerms(
        underlying_ticker=conversion.get("underlying_ticker", issuer.get("ticker", "")),
        conversion_price=conversion_price,
        reference_share_price=(
            float(conversion["reference_share_price"])
            if conversion.get("reference_share_price") is not None
            else None
        ),
        start_date=_parse_date(conversion.get("start_date")),
        end_date=_parse_date(conversion.get("end_date")),
        fixed_fx_rate=_fixed_fx_rate_in_standard_convention(conversion, contract_currency, stock_currency),
        fixed_fx_convention=(FXConvention.STOCK_PER_CB if conversion.get("fixed_exchange_rate") is not None else None),
    )

    calls = []
    for item in raw.get("calls", []):
        calls.append(
            CallSchedule(
                call_type=item.get("type", "call"),
                model_type=item.get("model_type", "soft_call"),
                start_date=_parse_date(item.get("start_date")),
                price=float(item.get("price", face)),
                trigger_ratio=(
                    float(item["trigger_ratio"]) if item.get("trigger_ratio") is not None else None
                ),
                description=item.get("description", ""),
            )
        )

    puts = []
    for item in raw.get("puts", []):
        puts.append(
            PutSchedule(
                put_type=item.get("type", "put"),
                model_type=item.get("model_type", "event_put"),
                date=_parse_date(item.get("date") or item.get("start_date")),
                price=float(item.get("price", face)),
                description=item.get("description", ""),
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
        issue_price=float(bond.get("issue_price", face)),
        maturity_price=float(redemption.get("maturity_price", face)),
        pricing_date=_parse_date(bond.get("pricing_date") or raw.get("as_of_date")) or date.today(),
        maturity_date=_parse_date(bond.get("maturity_date")) or date.today(),
        coupon=coupon,
        conversion=conversion_terms,
        puts=puts,
        calls=calls,
        source={
            "source_file": raw.get("source_file"),
            "source_type": raw.get("source_type"),
            "status": raw.get("status"),
        },
        metadata={
            "instrument": raw.get("instrument", {}),
            "ratings": issuer.get("ratings", {}),
            "model_notes": raw.get("model_notes", []),
            "raw_keys": sorted(raw.keys()),
        },
    )
    require_positive("maturity_years", contract.maturity_years_from_pricing)
    return contract


def loads_contract_json(text: str) -> Contract:
    return contract_from_dict(json.loads(text))


def load_contract_json(path: str | Path) -> Contract:
    with Path(path).open("r", encoding="utf-8") as handle:
        return contract_from_dict(json.load(handle))
