"""Normalize settlement-linked CB cash flows into their economic currency."""

from __future__ import annotations

import math
from dataclasses import replace

from cb_terminal.domain import Contract, FXConvention, MarketSnapshot


def economic_terms(contract: Contract, market: MarketSnapshot) -> tuple[Contract, MarketSnapshot, float]:
    """Return native-currency inputs and the factor from native to quote points.

    A linked USD/TWD bond pays fixed TWD cash amounts converted to USD at the
    payment fixing.  Valuing those amounts and the TWD shares in TWD, then
    translating the present value at spot, keeps one consistent numeraire.
    """
    if not contract.cash_fx_linked:
        return contract, market, 1.0
    economic = contract.economic_currency.upper()
    output = contract.currency.upper()
    if economic == output:
        return contract, market, 1.0
    if economic != contract.stock_currency.upper() or contract.settlement_currency.upper() != output:
        raise ValueError("linked cash flows require a quoted FX rate between economic and settlement currencies")
    fixed = contract.conversion.fixed_fx_rate
    if fixed is None or not math.isfinite(fixed) or fixed <= 0.0:
        raise ValueError("linked cash flows require a positive fixed contract FX rate")
    if contract.conversion.fixed_fx_convention is FXConvention.CB_PER_STOCK:
        fixed = 1.0 / fixed
    if market.fx_rate == 1.0 and market.fx_convention is None:
        raise ValueError("linked cash flows require an explicit market FX quote")
    spot = market.fx_rate
    if market.fx_convention is FXConvention.CB_PER_STOCK:
        spot = 1.0 / spot
    if not math.isfinite(spot) or spot <= 0.0:
        raise ValueError("linked cash flows require a positive market FX rate")
    native_contract = replace(
        contract,
        currency=economic,
        settlement_currency=economic,
        face=contract.face * fixed,
        issue_price=contract.issue_price * fixed,
        maturity_price=contract.maturity_price * fixed,
        puts=[replace(put, price=put.price * fixed) for put in contract.puts],
        calls=[replace(call, price=call.price * fixed) for call in contract.calls],
        conversion=replace(contract.conversion, fixed_fx_rate=None, fixed_fx_convention=None),
        cash_fx_linked=False,
    )
    native_market = replace(
        market,
        stock_currency=economic,
        bond_price=None if market.bond_price is None else market.bond_price * spot,
        bond_price_currency=economic,
        fx_rate=1.0,
        fx_convention=None,
    )
    return native_contract, native_market, 1.0 / spot
