"""Transparent stdlib-only convertible-bond pricing engine."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, timedelta
from typing import Optional

from cb_terminal.domain import Assumptions, Contract, Diagnostics, FXConvention, MarketSnapshot, PricingResult
from cb_terminal.validation import require_non_negative, require_positive


class PricingEngine:
    """Convertible bond pricing engine.

    Supported model modes:
    - simple_crr: baseline one-value CRR tree discounted at risk-free + spread.
    - tf_split_tree: Tsiveriotis-Fernandes-style split cash/equity tree where
      the cash component is risky and the equity/conversion component is
      discounted at the risk-free rate.
    """

    SUPPORTED_MODEL_MODES = {"simple_crr", "tf_split_tree"}

    def __init__(self, model_mode: str = "simple_crr"):
        if model_mode not in self.SUPPORTED_MODEL_MODES:
            raise ValueError(f"unsupported model_mode: {model_mode}")
        self.model_mode = model_mode

    def price(self, contract: Contract, market: MarketSnapshot, assumptions: Assumptions) -> PricingResult:
        _validate_inputs(contract, market, assumptions)
        market_context = _market_context(contract, market)
        if self.model_mode == "tf_split_tree":
            fair_value, diagnostics = self._tf_split_price(contract, market, assumptions, market_context)
        else:
            fair_value, diagnostics = self._binomial_price(contract, market, assumptions, market_context)
        valuation_date = assumptions.valuation_date or market.as_of_date or contract.pricing_date
        parity = _parity(float(market_context["conversion_ratio"]), market_context["stock_price_in_cb_currency"])
        bond_floor = _bond_floor(contract, assumptions, valuation_date)
        bond_price = market_context["bond_price_in_output_currency"]
        cheapness = None if bond_price is None else fair_value - bond_price
        return PricingResult(
            fair_value=fair_value,
            bond_floor=bond_floor,
            parity=parity,
            cheapness=cheapness,
            implied_volatility=None,
            diagnostics=diagnostics,
            output_currency=market_context["output_currency"],
        )

    def implied_vol(
        self,
        contract: Contract,
        market: MarketSnapshot,
        assumptions: Assumptions,
        target_price: Optional[float] = None,
        low: float = 0.0001,
        high: float = 3.0,
        tolerance: float = 1e-6,
        max_iterations: int = 100,
    ) -> float:
        """Solve for volatility by bisection.

        Returns decimal volatility (0.35 for 35%).  Raises ValueError when the
        target lies outside the price range of the bracket.
        """

        target = market.bond_price if target_price is None else target_price
        if target is None:
            raise ValueError("target_price or market.bond_price is required for implied_vol")

        def value(vol: float) -> float:
            return self.price(contract, market, replace(assumptions, volatility=vol)).fair_value

        low_price = value(low)
        high_price = value(high)
        if low_price > high_price:
            low_price, high_price = high_price, low_price
            low, high = high, low
        if target < low_price - tolerance or target > high_price + tolerance:
            raise ValueError(
                f"target price {target:.6f} outside vol bracket price range "
                f"[{low_price:.6f}, {high_price:.6f}]"
            )

        lo, hi = low, high
        mid = (lo + hi) / 2.0
        for _ in range(max_iterations):
            mid = (lo + hi) / 2.0
            mid_price = value(mid)
            if abs(mid_price - target) <= tolerance:
                return mid
            if mid_price < target:
                lo = mid
            else:
                hi = mid
        return mid

    def cheapness(
        self, contract: Contract, market: MarketSnapshot, assumptions: Assumptions
    ) -> float:
        result = self.price(contract, market, assumptions)
        if result.cheapness is None:
            raise ValueError("market.bond_price is required for cheapness")
        return result.cheapness

    def price_with_implied_vol(
        self, contract: Contract, market: MarketSnapshot, assumptions: Assumptions
    ) -> PricingResult:
        result = self.price(contract, market, assumptions)
        iv = None
        if market.bond_price is not None:
            iv = self.implied_vol(contract, market, assumptions, target_price=market.bond_price)
        return PricingResult(
            fair_value=result.fair_value,
            bond_floor=result.bond_floor,
            parity=result.parity,
            cheapness=result.cheapness,
            implied_volatility=iv,
            diagnostics=result.diagnostics,
            output_currency=result.output_currency,
        )

    def _binomial_price(
        self, contract: Contract, market: MarketSnapshot, assumptions: Assumptions, market_context: dict[str, object]
    ) -> tuple[float, Diagnostics]:
        valuation_date = assumptions.valuation_date or market.as_of_date or contract.pricing_date
        stock_price = float(market_context["stock_price_in_cb_currency"])
        maturity_years = max((contract.maturity_date - valuation_date).days / 365.25, 0.0)
        conversion_allowed = _conversion_allowed_steps(contract, valuation_date, steps=0)
        if maturity_years <= 0:
            conversion_value = _parity(float(market_context["conversion_ratio"]), stock_price)
            terminal = max(contract.maturity_price, conversion_value) if 0 in conversion_allowed else contract.maturity_price
            return terminal, _diagnostics(contract, assumptions, maturity_years, 0.0, 1.0, 1.0, [], market_context)

        steps = max(1, int(assumptions.steps))
        dt = maturity_years / steps
        sigma = max(assumptions.volatility, 1e-8)
        up = math.exp(sigma * math.sqrt(dt))
        down = 1.0 / up
        growth = math.exp(assumptions.equity_carry * dt)
        denominator = up - down
        q = (growth - down) / denominator if denominator else 0.5
        warnings = []
        if q < 0.0 or q > 1.0:
            warnings.append("risk-neutral probability clipped; increase steps or review carry/volatility")
            q = min(max(q, 0.0), 1.0)

        credit_rate = assumptions.risk_free_rate + assumptions.credit_spread
        discount = math.exp(-credit_rate * dt)
        conversion_ratio = float(market_context["conversion_ratio"])
        conversion_allowed = _conversion_allowed_steps(contract, valuation_date, steps)
        coupon_steps = _coupon_steps(contract, steps, maturity_years)
        put_by_step = _scheduled_puts(contract, valuation_date, steps, maturity_years)
        call_by_step = _soft_calls(
            contract,
            valuation_date,
            steps,
            maturity_years,
            float(market_context["conversion_price_in_cb_currency"]),
        )

        values = []
        for j in range(steps + 1):
            stock = stock_price * (up ** j) * (down ** (steps - j))
            conversion_value = conversion_ratio * stock
            values.append(max(contract.maturity_price, conversion_value) if steps in conversion_allowed else contract.maturity_price)

        coupon_amount = contract.face * contract.coupon.annual_rate / contract.coupon.frequency if contract.coupon.frequency else 0.0
        for step in range(steps - 1, -1, -1):
            next_values = []
            for j in range(step + 1):
                stock = stock_price * (up ** j) * (down ** (step - j))
                conversion_value = conversion_ratio * stock
                continuation = discount * (q * values[j + 1] + (1.0 - q) * values[j])
                if (step + 1) in coupon_steps:
                    continuation += coupon_amount * discount
                conversion_is_allowed = step in conversion_allowed
                node_value = max(continuation, conversion_value) if conversion_is_allowed else continuation
                put_price = put_by_step.get(step)
                if put_price is not None:
                    node_value = max(node_value, put_price)
                call = call_by_step.get(step)
                if call is not None:
                    trigger_stock, call_price = call
                    if stock >= trigger_stock:
                        call_takeout = max(call_price, conversion_value) if conversion_is_allowed else call_price
                        node_value = min(node_value, call_takeout)
                next_values.append(node_value)
            values = next_values

        return values[0], _diagnostics(contract, assumptions, maturity_years, dt, up, down, warnings, market_context)

    def _tf_split_price(
        self, contract: Contract, market: MarketSnapshot, assumptions: Assumptions, market_context: dict[str, object]
    ) -> tuple[float, Diagnostics]:
        valuation_date = assumptions.valuation_date or market.as_of_date or contract.pricing_date
        stock_price = float(market_context["stock_price_in_cb_currency"])
        maturity_years = max((contract.maturity_date - valuation_date).days / 365.25, 0.0)
        conversion_ratio = float(market_context["conversion_ratio"])
        conversion_allowed = _conversion_allowed_steps(contract, valuation_date, steps=0)
        if maturity_years <= 0:
            conversion_value = _parity(conversion_ratio, stock_price)
            if 0 in conversion_allowed and conversion_value > contract.maturity_price:
                terminal = conversion_value
                equity_component = conversion_value
                cash_component = 0.0
            else:
                terminal = contract.maturity_price
                equity_component = 0.0
                cash_component = contract.maturity_price
            context = {**market_context, "model_mode": self.model_mode, "equity_component": equity_component, "cash_component": cash_component}
            return terminal, _diagnostics(contract, assumptions, maturity_years, 0.0, 1.0, 1.0, [], context)

        steps = max(1, int(assumptions.steps))
        dt = maturity_years / steps
        sigma = max(assumptions.volatility, 1e-8)
        up = math.exp(sigma * math.sqrt(dt))
        down = 1.0 / up
        growth = math.exp(assumptions.equity_carry * dt)
        denominator = up - down
        q = (growth - down) / denominator if denominator else 0.5
        warnings = []
        if q < 0.0 or q > 1.0:
            warnings.append("risk-neutral probability clipped; increase steps or review carry/volatility")
            q = min(max(q, 0.0), 1.0)

        equity_discount = math.exp(-assumptions.risk_free_rate * dt)
        cash_discount = math.exp(-(assumptions.risk_free_rate + assumptions.credit_spread) * dt)
        conversion_allowed = _conversion_allowed_steps(contract, valuation_date, steps)
        coupon_steps = _coupon_steps(contract, steps, maturity_years)
        put_by_step = _scheduled_puts(contract, valuation_date, steps, maturity_years)
        call_by_step = _soft_calls(
            contract,
            valuation_date,
            steps,
            maturity_years,
            float(market_context["conversion_price_in_cb_currency"]),
        )

        equity_values: list[float] = []
        cash_values: list[float] = []
        for j in range(steps + 1):
            stock = stock_price * (up ** j) * (down ** (steps - j))
            conversion_value = conversion_ratio * stock
            if steps in conversion_allowed and conversion_value > contract.maturity_price:
                equity_values.append(conversion_value)
                cash_values.append(0.0)
            else:
                equity_values.append(0.0)
                cash_values.append(contract.maturity_price)

        coupon_amount = contract.face * contract.coupon.annual_rate / contract.coupon.frequency if contract.coupon.frequency else 0.0
        for step in range(steps - 1, -1, -1):
            next_equity: list[float] = []
            next_cash: list[float] = []
            for j in range(step + 1):
                stock = stock_price * (up ** j) * (down ** (step - j))
                conversion_value = conversion_ratio * stock
                equity_continuation = equity_discount * (q * equity_values[j + 1] + (1.0 - q) * equity_values[j])
                cash_continuation = cash_discount * (q * cash_values[j + 1] + (1.0 - q) * cash_values[j])
                if (step + 1) in coupon_steps:
                    cash_continuation += coupon_amount * cash_discount
                node_equity = equity_continuation
                node_cash = cash_continuation
                node_value = node_equity + node_cash
                if step in conversion_allowed and conversion_value > node_value:
                    node_equity = conversion_value
                    node_cash = 0.0
                    node_value = conversion_value
                put_price = put_by_step.get(step)
                if put_price is not None and put_price > node_value:
                    node_equity = 0.0
                    node_cash = put_price
                    node_value = put_price
                call = call_by_step.get(step)
                if call is not None:
                    trigger_stock, call_price = call
                    if stock >= trigger_stock:
                        call_takeout = max(call_price, conversion_value) if step in conversion_allowed else call_price
                        if node_value > call_takeout:
                            if call_takeout == conversion_value and step in conversion_allowed:
                                node_equity = conversion_value
                                node_cash = 0.0
                            else:
                                node_equity = 0.0
                                node_cash = call_takeout
                next_equity.append(node_equity)
                next_cash.append(node_cash)
            equity_values = next_equity
            cash_values = next_cash

        context = {
            **market_context,
            "model_mode": self.model_mode,
            "equity_component": equity_values[0],
            "cash_component": cash_values[0],
        }
        return equity_values[0] + cash_values[0], _diagnostics(contract, assumptions, maturity_years, dt, up, down, warnings, context)


def _validate_inputs(contract: Contract, market: MarketSnapshot, assumptions: Assumptions) -> None:
    require_positive("face", contract.face)
    require_positive("conversion_price", contract.conversion.conversion_price)
    require_positive("stock_price", market.stock_price)
    require_positive("fx_rate", market.fx_rate)
    require_non_negative("volatility", assumptions.volatility)
    require_positive("steps", assumptions.steps)


def _normalize_currency(value: Optional[str]) -> str:
    return (value or "").strip().upper()


def _normalize_fx_convention(value: object) -> Optional[FXConvention]:
    if value is None:
        return None
    if isinstance(value, FXConvention):
        return value
    if isinstance(value, str):
        try:
            return FXConvention[value.upper()]
        except KeyError:
            try:
                return FXConvention(value.upper())
            except ValueError as exc:
                raise ValueError(f"unsupported FX convention: {value}") from exc
    raise ValueError(f"unsupported FX convention: {value!r}")


def _standard_fx_convention() -> FXConvention:
    """Canonical project FX convention: stock currency per CB currency."""

    return FXConvention.STOCK_PER_CB


def _market_context(contract: Contract, market: MarketSnapshot) -> dict[str, object]:
    """Normalize market prices into the CB/output currency with explicit FX details."""

    output_currency = _normalize_currency(contract.currency or contract.settlement_currency)
    contract_stock_currency = _normalize_currency(contract.stock_currency or output_currency)
    market_stock_currency = _normalize_currency(market.stock_currency or contract_stock_currency or output_currency)
    bond_price_currency = _normalize_currency(market.bond_price_currency or output_currency)

    if contract_stock_currency and market_stock_currency and market_stock_currency != contract_stock_currency:
        raise ValueError(
            f"market stock currency {market_stock_currency} is incompatible with contract stock currency {contract_stock_currency}"
        )
    if market.bond_price is not None and bond_price_currency != output_currency:
        settlement_currency = _normalize_currency(contract.settlement_currency)
        if not settlement_currency or bond_price_currency != settlement_currency:
            raise ValueError(
                f"market bond price currency {bond_price_currency} is incompatible with output currency {output_currency}"
            )

    same_currency = market_stock_currency == output_currency
    fx_convention = _normalize_fx_convention(market.fx_convention)
    warnings: list[str] = []

    stock_fx_rate = market.fx_rate
    conversion_fx_rate = 1.0
    conversion_fx_source = "same_currency"
    conversion_fx_convention: Optional[FXConvention] = None

    if same_currency:
        stock_price_in_cb = market.stock_price
        conversion_price_in_cb = contract.conversion.conversion_price
        stock_fx_rate = 1.0
        if fx_convention is not None and abs(market.fx_rate - 1.0) > 1e-12:
            warnings.append("FX convention supplied for same-currency contract; stock price left unchanged")
    else:
        if fx_convention is None:
            fx_convention = _standard_fx_convention()
        if contract.conversion.fixed_fx_rate is not None and contract.conversion.fixed_fx_rate > 0.0:
            conversion_fx_rate = contract.conversion.fixed_fx_rate
            conversion_fx_convention = _normalize_fx_convention(contract.conversion.fixed_fx_convention)
            if conversion_fx_convention is None:
                conversion_fx_convention = _standard_fx_convention()
            conversion_fx_source = "fixed_contract_fx"
        else:
            conversion_fx_rate = market.fx_rate
            conversion_fx_convention = fx_convention
            conversion_fx_source = "market_fx_fallback"
            warnings.append("fixed conversion FX rate missing; using market FX rate for conversion price")

        if fx_convention is FXConvention.CB_PER_STOCK:
            stock_price_in_cb = market.stock_price * stock_fx_rate
        elif fx_convention is FXConvention.STOCK_PER_CB:
            stock_price_in_cb = market.stock_price / stock_fx_rate
        else:  # pragma: no cover - guarded by enum normalization
            raise ValueError(f"unsupported FX convention: {fx_convention}")
        conversion_price_in_cb = _convert_stock_currency_to_cb(
            contract.conversion.conversion_price,
            conversion_fx_rate,
            conversion_fx_convention,
        )

    conversion_ratio = _conversion_ratio(contract, conversion_price_in_cb)

    return {
        "output_currency": output_currency,
        "stock_currency": market_stock_currency,
        "bond_price_currency": bond_price_currency,
        "stock_price": market.stock_price,
        "stock_price_in_cb_currency": stock_price_in_cb,
        "conversion_price_in_cb_currency": conversion_price_in_cb,
        "conversion_ratio": conversion_ratio,
        "bond_price_in_output_currency": market.bond_price,
        "fx_rate": market.fx_rate,
        "stock_fx_rate": stock_fx_rate,
        "conversion_fx_rate": conversion_fx_rate,
        "conversion_fx_convention": conversion_fx_convention.name if conversion_fx_convention is not None else None,
        "conversion_fx_source": conversion_fx_source,
        "fx_convention": fx_convention.name if fx_convention is not None else None,
        "same_currency": same_currency,
        "warnings": warnings,
    }


def _convert_stock_currency_to_cb(amount: float, fx_rate: float, convention: FXConvention) -> float:
    if convention is FXConvention.CB_PER_STOCK:
        return amount * fx_rate
    if convention is FXConvention.STOCK_PER_CB:
        return amount / fx_rate
    raise ValueError(f"unsupported FX convention: {convention}")


def _conversion_ratio(contract: Contract, conversion_price_in_cb_currency: Optional[float] = None) -> float:
    # Number of reference shares per pricing face using the conversion price in
    # the CB/output currency.  For cross-currency CBs, conversion_price is a
    # stock-currency term and must be FX-converted before computing the ratio.
    conversion_price = (
        contract.conversion.conversion_price
        if conversion_price_in_cb_currency is None
        else conversion_price_in_cb_currency
    )
    return contract.face / conversion_price


def _parity(conversion_ratio: float, stock_price: float) -> float:
    return conversion_ratio * stock_price


def _coupon_count(maturity_years: float, frequency: int) -> int:
    # The engine is intentionally approximate, but exact-calendar annual/semi-
    # annual maturities such as 2026-01-01 to 2027-01-01 are 0.9993 years on a
    # 365.25 basis. Add a small tolerance before flooring so those coupons are
    # not accidentally dropped.
    return max(0, int(math.floor(maturity_years * frequency + 0.01)))


def _bond_floor(contract: Contract, assumptions: Assumptions, valuation_date: date) -> float:
    maturity_years = max((contract.maturity_date - valuation_date).days / 365.25, 0.0)
    rate = assumptions.risk_free_rate + assumptions.credit_spread
    pv = contract.maturity_price * math.exp(-rate * maturity_years)
    if contract.coupon.frequency:
        coupon = contract.face * contract.coupon.annual_rate / contract.coupon.frequency
        count = _coupon_count(maturity_years, contract.coupon.frequency)
        for k in range(1, count + 1):
            pv += coupon * math.exp(-rate * k / contract.coupon.frequency)
    return pv


def _coupon_steps(contract: Contract, steps: int, maturity_years: float) -> set[int]:
    if not contract.coupon.frequency or contract.coupon.annual_rate == 0.0:
        return set()
    coupon_count = _coupon_count(maturity_years, contract.coupon.frequency)
    coupon_steps = set()
    for k in range(1, coupon_count + 1):
        t = k / contract.coupon.frequency
        coupon_steps.add(min(steps, max(1, int(round(t / maturity_years * steps)))))
    return coupon_steps


def _step_from_date(event_date: date, valuation_date: date, steps: int, maturity_years: float) -> Optional[int]:
    years = (event_date - valuation_date).days / 365.25
    if years < 0 or years > maturity_years or maturity_years <= 0:
        return None
    return min(steps, max(0, int(round(years / maturity_years * steps))))


def _node_date(valuation_date: date, maturity_date: date, step: int, steps: int) -> date:
    if steps <= 0:
        return valuation_date
    if step >= steps:
        return maturity_date
    total_days = (maturity_date - valuation_date).days
    return valuation_date + timedelta(days=int(round(total_days * step / steps)))


def _conversion_allowed_on_date(contract: Contract, node_date: date) -> bool:
    start_date = contract.conversion.start_date
    end_date = contract.conversion.end_date
    if start_date is not None and node_date < start_date:
        return False
    if end_date is not None and node_date > end_date:
        return False
    return True


def _conversion_allowed_steps(contract: Contract, valuation_date: date, steps: int) -> set[int]:
    return {
        step
        for step in range(max(0, steps) + 1)
        if _conversion_allowed_on_date(contract, _node_date(valuation_date, contract.maturity_date, step, steps))
    }


def _scheduled_puts(contract: Contract, valuation_date: date, steps: int, maturity_years: float) -> dict[int, float]:
    put_by_step: dict[int, float] = {}
    for put in contract.puts:
        if put.model_type != "scheduled_put" or put.date is None:
            continue
        step = _step_from_date(put.date, valuation_date, steps, maturity_years)
        if step is not None:
            put_by_step[step] = max(put_by_step.get(step, 0.0), put.price)
    return put_by_step


def _soft_calls(
    contract: Contract,
    valuation_date: date,
    steps: int,
    maturity_years: float,
    conversion_price_in_cb_currency: float,
) -> dict[int, tuple[float, float]]:
    call_by_step: dict[int, tuple[float, float]] = {}
    for call in contract.calls:
        if call.model_type != "soft_call" or call.start_date is None or call.trigger_ratio is None:
            continue
        start_step = _step_from_date(call.start_date, valuation_date, steps, maturity_years)
        if start_step is None:
            continue
        trigger_stock = call.trigger_ratio * conversion_price_in_cb_currency
        for step in range(start_step, steps + 1):
            call_by_step[step] = (trigger_stock, call.price)
    return call_by_step


def _diagnostics(
    contract: Contract,
    assumptions: Assumptions,
    maturity_years: float,
    dt: float,
    up: float,
    down: float,
    warnings: list[str],
    market_context: Optional[dict[str, object]] = None,
) -> Diagnostics:
    details = dict(market_context or {})
    combined_warnings = list(warnings)
    combined_warnings.extend(details.pop("warnings", []))
    return Diagnostics(
        steps=max(1, int(assumptions.steps)),
        maturity_years=maturity_years,
        dt=dt,
        up=up,
        down=down,
        equity_discount_rate=assumptions.risk_free_rate,
        credit_discount_rate=assumptions.risk_free_rate + assumptions.credit_spread,
        conversion_ratio=float(details.get("conversion_ratio", _conversion_ratio(contract))),
        warnings=combined_warnings,
        details=details,
    )
