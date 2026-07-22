"""Transparent stdlib-only convertible-bond pricing engine."""

from __future__ import annotations

import calendar
import math
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Optional

from cb_terminal.domain import Assumptions, Contract, Diagnostics, FXConvention, MarketSnapshot, PricingResult
from cb_terminal.pricing.black_scholes import norm_cdf
from cb_terminal.validation import require_finite, require_non_negative, require_positive


MODEL_VERSION = "v2"


@dataclass(frozen=True)
class _LatticeParameters:
    requested_steps: int
    steps: int
    dt: float
    up: float
    down: float
    probability: float
    method: str
    warnings: tuple[str, ...] = ()


class PricingEngine:
    """Convertible bond pricing engine.

    Supported model modes:
    - simple_crr: baseline one-value CRR tree discounted at risk-free + spread.
    - tf_split_tree: Tsiveriotis-Fernandes-style split cash/equity tree where
      the cash component is risky and the equity/conversion component is
      discounted at the risk-free rate.
    """

    SUPPORTED_MODEL_MODES = {"simple_crr", "tf_split_tree"}

    def __init__(self, model_mode: str = "tf_split_tree"):
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
        """Solve for a unique volatility root using sampled bracketing.

        Returns decimal volatility (0.35 for 35%). Callable convertibles can
        have a flat or non-monotone price/volatility relationship, so their
        bracket is sampled first and ambiguous roots are rejected.
        """

        target = market.bond_price if target_price is None else target_price
        if target is None:
            raise ValueError("target_price or market.bond_price is required for implied_vol")
        require_finite("target_price", target)
        require_non_negative("low", low)
        require_positive("high", high)
        require_positive("tolerance", tolerance)
        require_positive("max_iterations", max_iterations)
        if low >= high:
            raise ValueError("implied-volatility bracket requires low < high")

        def value(vol: float) -> float:
            return self.price(contract, market, replace(assumptions, volatility=vol)).fair_value

        sample_count = 32 if contract.calls else 1
        vols = [low + (high - low) * index / sample_count for index in range(sample_count + 1)]
        prices = [value(vol) for vol in vols]
        price_span = max(prices) - min(prices)
        sensitivity_tolerance = max(tolerance, 1e-10 * max(1.0, abs(target)))
        if price_span <= sensitivity_tolerance:
            raise ValueError(
                "implied volatility is not identifiable: model price is insensitive to volatility "
                "at the selected contract terms and credit spread"
            )
        if target < min(prices) - tolerance or target > max(prices) + tolerance:
            raise ValueError(
                f"target price {target:.6f} outside vol bracket price range "
                f"[{min(prices):.6f}, {max(prices):.6f}]"
            )

        exact_roots: list[float] = []
        brackets: list[tuple[float, float, float, float]] = []
        for index, (left_vol, right_vol) in enumerate(zip(vols, vols[1:])):
            left_error = prices[index] - target
            right_error = prices[index + 1] - target
            left_is_exact = abs(left_error) <= tolerance
            right_is_exact = abs(right_error) <= tolerance
            if left_is_exact:
                exact_roots.append(left_vol)
            if left_error * right_error < 0.0 and not left_is_exact and not right_is_exact:
                brackets.append((left_vol, right_vol, left_error, right_error))
        if abs(prices[-1] - target) <= tolerance:
            exact_roots.append(vols[-1])

        deduplicated_exact: list[float] = []
        for root in exact_roots:
            if not deduplicated_exact or abs(root - deduplicated_exact[-1]) > 1e-12:
                deduplicated_exact.append(root)
        candidate_count = len(deduplicated_exact) + len(brackets)
        if candidate_count == 0:
            raise ValueError(
                "no stable implied-volatility root found inside the sampled bracket; "
                "the price/volatility curve may be non-monotone"
            )
        if candidate_count > 1:
            raise ValueError(
                "implied volatility is not unique inside the bracket; "
                "call features make the price/volatility relationship non-monotone"
            )
        if deduplicated_exact:
            return deduplicated_exact[0]

        lo, hi, lo_error, _ = brackets[0]
        mid = (lo + hi) / 2.0
        for _ in range(max_iterations):
            mid = (lo + hi) / 2.0
            mid_error = value(mid) - target
            if abs(mid_error) <= tolerance:
                return mid
            if lo_error * mid_error <= 0.0:
                hi = mid
            else:
                lo = mid
                lo_error = mid_error
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
        conversion_ratio = float(market_context["conversion_ratio"])
        base_context = {**market_context, "model_mode": self.model_mode}
        conversion_allowed = _conversion_allowed_steps(contract, valuation_date, steps=0)
        if maturity_years <= 0:
            conversion_value = _parity(conversion_ratio, stock_price)
            maturity_cash = contract.maturity_price + _maturity_coupon_amount(contract)
            put_by_step = _scheduled_puts(contract, valuation_date, 0, maturity_years)
            call_by_step = _soft_calls(
                contract,
                valuation_date,
                0,
                maturity_years,
                float(market_context["conversion_price_in_cb_currency"]),
            )
            terminal, _, conflict = _select_obstacle(
                maturity_cash,
                conversion_value,
                0 in conversion_allowed,
                put_by_step.get(0),
                call_by_step.get(0, ()),
                stock_price,
            )
            warnings = ["overlapping put/call constraints conflict; holder floor retained"] if conflict else []
            details = {
                **base_context,
                "requested_steps": int(assumptions.steps),
                "effective_steps": 0,
                "lattice_method": "at_maturity",
                "risk_neutral_probability": None,
            }
            return terminal, _diagnostics(contract, assumptions, maturity_years, 0.0, 1.0, 1.0, warnings, details)

        lattice = _lattice_parameters(
            assumptions,
            maturity_years,
            effective_steps=_effective_lattice_steps(contract, valuation_date, maturity_years, assumptions.steps),
        )
        steps = lattice.steps
        dt = lattice.dt
        up = lattice.up
        down = lattice.down
        q = lattice.probability
        warnings = list(lattice.warnings)

        credit_rate = assumptions.risk_free_rate + assumptions.credit_spread
        discount = math.exp(-credit_rate * dt)
        conversion_allowed = _conversion_allowed_steps(contract, valuation_date, steps)
        coupon_by_step, coupon_dates = _coupon_amounts_by_step(contract, valuation_date, steps, maturity_years)
        if len(coupon_dates) > len(coupon_by_step):
            warnings.append(
                "multiple coupon dates share a lattice node; increase steps for more accurate coupon timing"
            )
        put_by_step = _scheduled_puts(contract, valuation_date, steps, maturity_years)
        call_by_step = _soft_calls(
            contract,
            valuation_date,
            steps,
            maturity_years,
            float(market_context["conversion_price_in_cb_currency"]),
        )

        maturity_cash = contract.maturity_price + coupon_by_step.get(steps, 0.0)
        values = []
        for j in range(steps + 1):
            stock = stock_price * (up ** j) * (down ** (steps - j))
            conversion_value = conversion_ratio * stock
            terminal, _, conflict = _select_obstacle(
                maturity_cash,
                conversion_value,
                steps in conversion_allowed,
                put_by_step.get(steps),
                call_by_step.get(steps, ()),
                stock,
            )
            if conflict and not any("overlapping put/call" in warning for warning in warnings):
                warnings.append("overlapping put/call constraints conflict; holder floor retained")
            values.append(terminal)

        for step in range(steps - 1, -1, -1):
            next_values = []
            for j in range(step + 1):
                stock = stock_price * (up ** j) * (down ** (step - j))
                conversion_value = conversion_ratio * stock
                continuation = discount * (q * values[j + 1] + (1.0 - q) * values[j])
                continuation += coupon_by_step.get(step, 0.0)
                node_value, _, conflict = _select_obstacle(
                    continuation,
                    conversion_value,
                    step in conversion_allowed,
                    put_by_step.get(step),
                    call_by_step.get(step, ()),
                    stock,
                )
                if conflict and not any("overlapping put/call" in warning for warning in warnings):
                    warnings.append("overlapping put/call constraints conflict; holder floor retained")
                next_values.append(node_value)
            values = next_values

        details = _lattice_context(base_context, lattice)
        return values[0], _diagnostics(contract, assumptions, maturity_years, dt, up, down, warnings, details)

    def _tf_split_price(
        self, contract: Contract, market: MarketSnapshot, assumptions: Assumptions, market_context: dict[str, object]
    ) -> tuple[float, Diagnostics]:
        valuation_date = assumptions.valuation_date or market.as_of_date or contract.pricing_date
        stock_price = float(market_context["stock_price_in_cb_currency"])
        maturity_years = max((contract.maturity_date - valuation_date).days / 365.25, 0.0)
        conversion_ratio = float(market_context["conversion_ratio"])
        base_context = {**market_context, "model_mode": self.model_mode}
        conversion_allowed = _conversion_allowed_steps(contract, valuation_date, steps=0)
        if maturity_years <= 0:
            conversion_value = _parity(conversion_ratio, stock_price)
            maturity_cash = contract.maturity_price + _maturity_coupon_amount(contract)
            put_by_step = _scheduled_puts(contract, valuation_date, 0, maturity_years)
            call_by_step = _soft_calls(
                contract,
                valuation_date,
                0,
                maturity_years,
                float(market_context["conversion_price_in_cb_currency"]),
            )
            terminal, selection, conflict = _select_obstacle(
                maturity_cash,
                conversion_value,
                0 in conversion_allowed,
                put_by_step.get(0),
                call_by_step.get(0, ()),
                stock_price,
            )
            equity_component, cash_component = _components_for_selection(
                selection,
                terminal,
                0.0,
                maturity_cash,
            )
            warnings = ["overlapping put/call constraints conflict; holder floor retained"] if conflict else []
            context = {
                **base_context,
                "equity_component": equity_component,
                "cash_component": cash_component,
                "requested_steps": int(assumptions.steps),
                "effective_steps": 0,
                "lattice_method": "at_maturity",
                "risk_neutral_probability": None,
            }
            return terminal, _diagnostics(contract, assumptions, maturity_years, 0.0, 1.0, 1.0, warnings, context)

        lattice = _lattice_parameters(
            assumptions,
            maturity_years,
            effective_steps=_effective_lattice_steps(contract, valuation_date, maturity_years, assumptions.steps),
        )
        steps = lattice.steps
        dt = lattice.dt
        up = lattice.up
        down = lattice.down
        q = lattice.probability
        warnings = list(lattice.warnings)

        equity_discount = math.exp(-assumptions.risk_free_rate * dt)
        cash_discount = math.exp(-(assumptions.risk_free_rate + assumptions.credit_spread) * dt)
        conversion_allowed = _conversion_allowed_steps(contract, valuation_date, steps)
        coupon_by_step, coupon_dates = _coupon_amounts_by_step(contract, valuation_date, steps, maturity_years)
        if len(coupon_dates) > len(coupon_by_step):
            warnings.append(
                "multiple coupon dates share a lattice node; increase steps for more accurate coupon timing"
            )
        put_by_step = _scheduled_puts(contract, valuation_date, steps, maturity_years)
        call_by_step = _soft_calls(
            contract,
            valuation_date,
            steps,
            maturity_years,
            float(market_context["conversion_price_in_cb_currency"]),
        )

        maturity_cash = contract.maturity_price + coupon_by_step.get(steps, 0.0)
        terminal_put = put_by_step.get(steps)
        terminal_calls = call_by_step.get(steps, ())
        terminal_obstacles_are_nonbinding = (
            (terminal_put is None or terminal_put <= maturity_cash)
            and all(call_price >= maturity_cash for _, call_price in terminal_calls)
        )
        terminal_smoothing = (
            assumptions.volatility * math.sqrt(dt) > 1e-12
            and steps in conversion_allowed
            and terminal_obstacles_are_nonbinding
        )
        equity_values: list[float] = []
        cash_values: list[float] = []
        if terminal_smoothing:
            smoothing_step = steps - 1
            for j in range(steps):
                stock = stock_price * (up ** j) * (down ** (smoothing_step - j))
                equity_continuation, cash_continuation = _tf_european_terminal_components(
                    stock,
                    maturity_cash,
                    conversion_ratio,
                    assumptions,
                    dt,
                )
                cash_continuation += coupon_by_step.get(smoothing_step, 0.0)
                continuation = equity_continuation + cash_continuation
                conversion_value = conversion_ratio * stock
                node_value, selection, conflict = _select_obstacle(
                    continuation,
                    conversion_value,
                    smoothing_step in conversion_allowed,
                    put_by_step.get(smoothing_step),
                    call_by_step.get(smoothing_step, ()),
                    stock,
                )
                if conflict and not any("overlapping put/call" in warning for warning in warnings):
                    warnings.append("overlapping put/call constraints conflict; holder floor retained")
                node_equity, node_cash = _components_for_selection(
                    selection,
                    node_value,
                    equity_continuation,
                    cash_continuation,
                )
                equity_values.append(node_equity)
                cash_values.append(node_cash)
            rollback_start = steps - 2
        else:
            for j in range(steps + 1):
                stock = stock_price * (up ** j) * (down ** (steps - j))
                conversion_value = conversion_ratio * stock
                terminal, selection, conflict = _select_obstacle(
                    maturity_cash,
                    conversion_value,
                    steps in conversion_allowed,
                    put_by_step.get(steps),
                    call_by_step.get(steps, ()),
                    stock,
                )
                if conflict and not any("overlapping put/call" in warning for warning in warnings):
                    warnings.append("overlapping put/call constraints conflict; holder floor retained")
                if (
                    selection in {"continuation", "conversion"}
                    and steps in conversion_allowed
                    and _nearly_equal(conversion_value, maturity_cash)
                ):
                    equity_values.append(0.5 * terminal)
                    cash_values.append(0.5 * terminal)
                else:
                    node_equity, node_cash = _components_for_selection(selection, terminal, 0.0, maturity_cash)
                    equity_values.append(node_equity)
                    cash_values.append(node_cash)
            rollback_start = steps - 1

        for step in range(rollback_start, -1, -1):
            next_equity: list[float] = []
            next_cash: list[float] = []
            for j in range(step + 1):
                stock = stock_price * (up ** j) * (down ** (step - j))
                conversion_value = conversion_ratio * stock
                equity_continuation = equity_discount * (q * equity_values[j + 1] + (1.0 - q) * equity_values[j])
                cash_continuation = cash_discount * (q * cash_values[j + 1] + (1.0 - q) * cash_values[j])
                cash_continuation += coupon_by_step.get(step, 0.0)
                continuation = equity_continuation + cash_continuation
                node_value, selection, conflict = _select_obstacle(
                    continuation,
                    conversion_value,
                    step in conversion_allowed,
                    put_by_step.get(step),
                    call_by_step.get(step, ()),
                    stock,
                )
                if conflict and not any("overlapping put/call" in warning for warning in warnings):
                    warnings.append("overlapping put/call constraints conflict; holder floor retained")
                node_equity, node_cash = _components_for_selection(
                    selection,
                    node_value,
                    equity_continuation,
                    cash_continuation,
                )
                next_equity.append(node_equity)
                next_cash.append(node_cash)
            equity_values = next_equity
            cash_values = next_cash

        context = {
            **_lattice_context(base_context, lattice),
            "equity_component": equity_values[0],
            "cash_component": cash_values[0],
            "terminal_smoothing": "analytic_one_step" if terminal_smoothing else "none",
        }
        return equity_values[0] + cash_values[0], _diagnostics(contract, assumptions, maturity_years, dt, up, down, warnings, context)


def _effective_lattice_steps(
    contract: Contract,
    valuation_date: date,
    maturity_years: float,
    requested_steps: int,
) -> int:
    """Keep dated rights off the valuation node on deliberately coarse grids.

    A uniform monthly minimum preserves recombination while giving coupons,
    puts, calls, and conversion boundaries a meaningful future node. Normal
    production grids (250+) are unchanged for usual CB maturities.
    """

    has_dated_events = bool(_coupon_dates(contract, valuation_date))
    has_dated_events = has_dated_events or any(
        put.model_type == "scheduled_put"
        and put.date is not None
        and valuation_date < put.date <= contract.maturity_date
        for put in contract.puts
    )
    has_dated_events = has_dated_events or any(
        call.model_type == "soft_call"
        and call.start_date is not None
        and call.start_date <= contract.maturity_date
        for call in contract.calls
    )
    conversion_boundaries = [contract.conversion.start_date, contract.conversion.end_date]
    conversion_boundaries.extend(boundary for window in contract.conversion.windows for boundary in window)
    has_dated_events = has_dated_events or any(
        boundary is not None and valuation_date < boundary <= contract.maturity_date
        for boundary in conversion_boundaries
    )
    if not has_dated_events:
        return max(1, int(requested_steps))
    monthly_steps = max(1, int(math.ceil(maturity_years * 12.0 - 1e-12)))
    return max(1, int(requested_steps), monthly_steps)


def _lattice_parameters(
    assumptions: Assumptions,
    maturity_years: float,
    *,
    effective_steps: Optional[int] = None,
) -> _LatticeParameters:
    """Return a recombining stock lattice without clipping probabilities.

    CRR is used whenever its no-arbitrage probability is valid. At zero
    volatility the tree collapses to the exact deterministic carry path. For
    low-volatility/high-carry inputs that invalidate CRR at the requested time
    step, an equal-probability log tree is shifted by ``log(cosh)`` so its
    one-step expected stock growth still equals the risk-neutral forward.
    """

    requested_steps = max(1, int(assumptions.steps))
    steps = max(requested_steps, int(effective_steps or requested_steps))
    refinement_warnings = (
        (
            f"effective lattice steps raised from {requested_steps} to {steps} "
            "to resolve contractual event timing",
        )
        if steps > requested_steps
        else ()
    )
    dt = maturity_years / steps
    sigma_step = assumptions.volatility * math.sqrt(dt)
    try:
        growth = math.exp(assumptions.equity_carry * dt)
    except OverflowError as exc:
        raise ValueError("carry/time-step combination overflows the stock lattice; increase steps") from exc
    if sigma_step <= 1e-12:
        return _LatticeParameters(
            requested_steps=requested_steps,
            steps=steps,
            dt=dt,
            up=growth,
            down=growth,
            probability=0.5,
            method="deterministic",
            warnings=refinement_warnings,
        )

    try:
        up = math.exp(sigma_step)
        down = math.exp(-sigma_step)
    except OverflowError as exc:
        raise ValueError("volatility/time-step combination overflows the stock lattice; increase steps") from exc
    probability = (growth - down) / (up - down)
    probability_tolerance = 1e-12
    if -probability_tolerance <= probability <= 1.0 + probability_tolerance:
        # Only remove floating-point noise at an otherwise valid boundary.
        probability = 0.0 if abs(probability) <= probability_tolerance else probability
        probability = 1.0 if abs(probability - 1.0) <= probability_tolerance else probability
        return _LatticeParameters(
            requested_steps=requested_steps,
            steps=steps,
            dt=dt,
            up=up,
            down=down,
            probability=probability,
            method="crr",
            warnings=refinement_warnings,
        )

    center = assumptions.equity_carry * dt - _log_cosh(sigma_step)
    try:
        shifted_up = math.exp(center + sigma_step)
        shifted_down = math.exp(center - sigma_step)
    except OverflowError as exc:
        raise ValueError("carry/volatility combination overflows the fallback stock lattice") from exc
    return _LatticeParameters(
        requested_steps=requested_steps,
        steps=steps,
        dt=dt,
        up=shifted_up,
        down=shifted_down,
        probability=0.5,
        method="drift_adjusted_equal_probability",
        warnings=refinement_warnings + (
            "requested CRR step violates the no-arbitrage probability bound; "
            "used a martingale-preserving equal-probability tree",
        ),
    )


def _log_cosh(value: float) -> float:
    magnitude = abs(value)
    return magnitude + math.log1p(math.exp(-2.0 * magnitude)) - math.log(2.0)


def _lattice_context(context: dict[str, object], lattice: _LatticeParameters) -> dict[str, object]:
    return {
        **context,
        "requested_steps": lattice.requested_steps,
        "effective_steps": lattice.steps,
        "lattice_method": lattice.method,
        "risk_neutral_probability": lattice.probability,
    }


def _value_tolerance(*values: float) -> float:
    return 1e-10 * max(1.0, *(abs(value) for value in values))


def _nearly_equal(left: float, right: float) -> bool:
    return abs(left - right) <= _value_tolerance(left, right)


def _select_obstacle(
    continuation: float,
    conversion_value: float,
    conversion_is_allowed: bool,
    put_price: Optional[float],
    calls: tuple[tuple[float, float], ...] | list[tuple[float, float]],
    stock: float,
) -> tuple[float, str, bool]:
    """Apply simultaneous holder floors and issuer caps at one node.

    The holder owns conversion/put rights, while an active issuer call caps
    continuation at the better of call cash and forced conversion. If a put
    floor exceeds a simultaneous call cap, the holder floor is retained and a
    conflict flag is surfaced for prospectus-priority review.
    """

    holder_value = -math.inf
    holder_selection = "continuation"
    if conversion_is_allowed:
        holder_value = conversion_value
        holder_selection = "conversion"
    if put_price is not None and (put_price > holder_value or _nearly_equal(put_price, holder_value)):
        holder_value = put_price
        holder_selection = "put"

    eligible_caps: list[tuple[float, str, float, float]] = []
    for trigger_stock, call_price in calls:
        if stock >= trigger_stock:
            if conversion_value >= call_price:
                eligible_caps.append((conversion_value, "call_conversion", trigger_stock, call_price))
            else:
                eligible_caps.append((call_price, "call_cash", trigger_stock, call_price))
    call_cap = math.inf
    call_selection = "continuation"
    if eligible_caps:
        call_cap, call_selection, _, _ = min(eligible_caps, key=lambda item: item)

    conflict = holder_value > call_cap + _value_tolerance(holder_value, call_cap)
    capped_continuation = min(continuation, call_cap)
    if holder_value > capped_continuation and not _nearly_equal(holder_value, capped_continuation):
        return holder_value, holder_selection, conflict
    if holder_value > capped_continuation:
        return holder_value, holder_selection, conflict
    if call_cap < continuation and not _nearly_equal(call_cap, continuation):
        return call_cap, call_selection, conflict
    return continuation, "continuation", conflict


def _components_for_selection(
    selection: str,
    value: float,
    continuation_equity: float,
    continuation_cash: float,
) -> tuple[float, float]:
    if selection in {"conversion", "call_conversion"}:
        return value, 0.0
    if selection in {"put", "call_cash"}:
        return 0.0, value
    return continuation_equity, continuation_cash


def _tf_european_terminal_components(
    stock: float,
    cash_redemption: float,
    conversion_ratio: float,
    assumptions: Assumptions,
    dt: float,
) -> tuple[float, float]:
    """Exact one-step TF split for ``max(cash, conversion_ratio * stock)``."""

    strike = cash_redemption / conversion_ratio
    sigma_t = assumptions.volatility * math.sqrt(dt)
    d1 = (
        math.log(stock / strike)
        + (assumptions.equity_carry + 0.5 * assumptions.volatility**2) * dt
    ) / sigma_t
    d2 = d1 - sigma_t
    equity = (
        conversion_ratio
        * stock
        * math.exp(-(assumptions.dividend_yield + assumptions.borrow_rate) * dt)
        * norm_cdf(d1)
    )
    cash = (
        cash_redemption
        * math.exp(-(assumptions.risk_free_rate + assumptions.credit_spread) * dt)
        * norm_cdf(-d2)
    )
    return equity, cash


def _validate_inputs(contract: Contract, market: MarketSnapshot, assumptions: Assumptions) -> None:
    require_positive("face", contract.face)
    require_positive("maturity_price", contract.maturity_price)
    require_positive("conversion_price", contract.conversion.conversion_price)
    require_positive("stock_price", market.stock_price)
    require_positive("fx_rate", market.fx_rate)
    if market.bond_price is not None:
        require_positive("bond_price", market.bond_price)
    require_non_negative("volatility", assumptions.volatility)
    require_finite("risk_free_rate", assumptions.risk_free_rate)
    require_non_negative("credit_spread", assumptions.credit_spread)
    require_non_negative("borrow_rate", assumptions.borrow_rate)
    require_non_negative("dividend_yield", assumptions.dividend_yield)
    require_positive("steps", assumptions.steps)
    if int(assumptions.steps) != assumptions.steps:
        raise ValueError(f"steps must be an integer, got {assumptions.steps!r}")
    require_non_negative("coupon annual_rate", contract.coupon.annual_rate)
    require_non_negative("coupon frequency", contract.coupon.frequency)
    if int(contract.coupon.frequency) != contract.coupon.frequency:
        raise ValueError(f"coupon frequency must be an integer, got {contract.coupon.frequency!r}")
    if contract.coupon.annual_rate > 0.0 and contract.coupon.frequency == 0:
        raise ValueError("positive coupon annual_rate requires a positive frequency")
    if contract.conversion.fixed_fx_rate is not None:
        require_finite("fixed conversion FX rate", contract.conversion.fixed_fx_rate)
        require_non_negative("fixed conversion FX rate", contract.conversion.fixed_fx_rate)
    valuation_date = assumptions.valuation_date or market.as_of_date or contract.pricing_date
    if contract.pricing_date > contract.maturity_date:
        raise ValueError("contract pricing_date cannot be after maturity_date")
    if valuation_date > contract.maturity_date:
        raise ValueError(
            f"valuation_date {valuation_date.isoformat()} is after maturity {contract.maturity_date.isoformat()}"
        )
    if (
        contract.conversion.start_date is not None
        and contract.conversion.end_date is not None
        and contract.conversion.start_date > contract.conversion.end_date
    ):
        raise ValueError("conversion start_date cannot be after end_date")
    for index, (window_start, window_end) in enumerate(contract.conversion.windows):
        if window_start > window_end:
            raise ValueError(f"conversion window {index} start_date cannot be after end_date")
    for put in contract.puts:
        require_positive("put price", put.price)
    for call in contract.calls:
        require_positive("call price", call.price)
        if call.trigger_ratio is not None:
            require_positive("call trigger_ratio", call.trigger_ratio)


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


def _coupon_dates(contract: Contract, valuation_date: date) -> list[date]:
    """Infer future coupon dates backwards from maturity.

    The normalized contract currently carries frequency rather than explicit
    payment dates. Anchoring to maturity preserves the next coupon when the
    valuation date moves between anniversaries and is materially safer than
    restarting a synthetic schedule on every valuation date.
    """

    frequency = contract.coupon.frequency
    if frequency <= 0 or contract.coupon.annual_rate == 0.0:
        return []
    full_years = max((contract.maturity_date - contract.pricing_date).days / 365.25, 0.0)
    payment_count = max(1, int(math.floor(full_years * frequency + 0.02)))
    dates: list[date] = []
    if 12 % frequency == 0:
        months_per_coupon = 12 // frequency
        for index in range(payment_count):
            payment_date = _shift_months(contract.maturity_date, -months_per_coupon * index)
            if payment_date > valuation_date:
                dates.append(payment_date)
    else:
        days_per_coupon = 365.25 / frequency
        for index in range(payment_count):
            payment_date = contract.maturity_date - timedelta(days=int(round(days_per_coupon * index)))
            if payment_date > valuation_date:
                dates.append(payment_date)
    return sorted(set(dates))


def _maturity_coupon_amount(contract: Contract) -> float:
    if contract.coupon.frequency <= 0 or contract.coupon.annual_rate == 0.0:
        return 0.0
    return contract.face * contract.coupon.annual_rate / contract.coupon.frequency


def _shift_months(value: date, months: int) -> date:
    month_index = value.year * 12 + (value.month - 1) + months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _coupon_amounts_by_step(
    contract: Contract,
    valuation_date: date,
    steps: int,
    maturity_years: float,
) -> tuple[dict[int, float], list[date]]:
    coupon_dates = _coupon_dates(contract, valuation_date)
    if not coupon_dates:
        return {}, []
    coupon_amount = contract.face * contract.coupon.annual_rate / contract.coupon.frequency
    amounts: dict[int, float] = {}
    for payment_date in coupon_dates:
        step = _step_from_date(payment_date, valuation_date, steps, maturity_years)
        if step is None:
            continue
        # A strictly future cash flow must not be pulled onto the valuation node
        # merely because the user requested a very coarse tree.
        step = max(1, step)
        amounts[step] = amounts.get(step, 0.0) + coupon_amount
    return amounts, coupon_dates


def _bond_floor(contract: Contract, assumptions: Assumptions, valuation_date: date) -> float:
    """Risky cash-only value including scheduled holder puts.

    Cash flows use a flat continuously compounded risky rate, consistent with
    the TF cash leg. Coupons are maturity-anchored and puts are exercised
    optimally in a deterministic backward cash-flow recursion.
    """

    rate = assumptions.risk_free_rate + assumptions.credit_spread
    coupon_amount = (
        contract.face * contract.coupon.annual_rate / contract.coupon.frequency
        if contract.coupon.frequency
        else 0.0
    )
    coupon_by_date = {payment_date: coupon_amount for payment_date in _coupon_dates(contract, valuation_date)}
    put_by_date: dict[date, float] = {}
    for put in contract.puts:
        if (
            put.model_type == "scheduled_put"
            and put.date is not None
            and valuation_date <= put.date <= contract.maturity_date
        ):
            put_by_date[put.date] = max(put_by_date.get(put.date, 0.0), put.price)

    maturity_coupon = coupon_by_date.get(contract.maturity_date, 0.0)
    if valuation_date == contract.maturity_date:
        maturity_coupon = _maturity_coupon_amount(contract)
    value = contract.maturity_price + maturity_coupon
    if contract.maturity_date in put_by_date:
        value = max(value, put_by_date[contract.maturity_date])
    future_date = contract.maturity_date
    event_dates = sorted(
        (set(coupon_by_date) | set(put_by_date)) - {contract.maturity_date},
        reverse=True,
    )
    for event_date in event_dates:
        years = (future_date - event_date).days / 365.25
        value *= math.exp(-rate * years)
        value += coupon_by_date.get(event_date, 0.0)
        if event_date in put_by_date:
            value = max(value, put_by_date[event_date])
        future_date = event_date
    years_to_first_event = (future_date - valuation_date).days / 365.25
    return value * math.exp(-rate * years_to_first_event)


def _step_from_date(event_date: date, valuation_date: date, steps: int, maturity_years: float) -> Optional[int]:
    years = (event_date - valuation_date).days / 365.25
    if maturity_years <= 0:
        return 0 if event_date == valuation_date else None
    if years < 0 or years > maturity_years:
        return None
    scaled_step = years / maturity_years * steps
    # A future right belongs to the first lattice node on or after its date; it
    # must never be rounded back onto the valuation node.
    return min(steps, max(0, int(math.ceil(scaled_step - 1e-12))))


def _node_date(valuation_date: date, maturity_date: date, step: int, steps: int) -> date:
    if steps <= 0:
        return valuation_date
    if step >= steps:
        return maturity_date
    total_days = (maturity_date - valuation_date).days
    return valuation_date + timedelta(days=int(round(total_days * step / steps)))


def _conversion_allowed_on_date(contract: Contract, node_date: date) -> bool:
    if contract.conversion.windows:
        return any(start_date <= node_date <= end_date for start_date, end_date in contract.conversion.windows)
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
) -> dict[int, tuple[tuple[float, float], ...]]:
    calls_by_step: dict[int, list[tuple[float, float]]] = {}
    for call in contract.calls:
        if call.model_type != "soft_call" or call.start_date is None or call.trigger_ratio is None:
            continue
        start_step = (
            0
            if call.start_date <= valuation_date
            else _step_from_date(call.start_date, valuation_date, steps, maturity_years)
        )
        if start_step is None:
            continue
        trigger_stock = call.trigger_ratio * conversion_price_in_cb_currency
        for step in range(start_step, steps + 1):
            calls_by_step.setdefault(step, []).append((trigger_stock, call.price))
    return {
        step: tuple(sorted(calls, key=lambda item: (item[0], item[1])))
        for step, calls in calls_by_step.items()
    }


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
    details.setdefault("model_version", f"{details.get('model_mode', 'unknown')}:{MODEL_VERSION}")
    details.setdefault("coupon_schedule_source", "maturity_anchored_frequency")
    combined_warnings = list(warnings)
    combined_warnings.extend(details.pop("warnings", []))
    if details.get("same_currency") is False:
        combined_warnings.append(
            "cross-currency valuation is a one-factor effective-stock approximation; FX volatility and stock/FX correlation are not modeled"
        )
    term_extensions = contract.metadata.get("term_extensions", {}) if isinstance(contract.metadata, dict) else {}
    if isinstance(term_extensions, dict):
        economic_currency = str(term_extensions.get("economic_currency") or contract.currency).upper()
        if economic_currency and economic_currency != contract.currency.upper():
            combined_warnings.append(
                f"principal is economically linked to {economic_currency}; valuation is a {contract.currency.upper()} legal-currency approximation and does not revalue settlement-equivalent principal"
            )
        if term_extensions.get("conversion_calendar_status"):
            combined_warnings.append(
                "conversion end date uses a conditional or weekday-only approximation; confirm the contractual exchange and banking calendars"
            )
        if term_extensions.get("conditional_early_conversion_start_rule"):
            combined_warnings.append(
                "event-contingent early conversion rights are documented but not modeled; valuation uses the ordinary conversion start date"
            )
    exchangeable_terms = contract.metadata.get("exchangeable_terms", {}) if isinstance(contract.metadata, dict) else {}
    if isinstance(exchangeable_terms, dict):
        if exchangeable_terms.get("issuer_cash_election"):
            combined_warnings.append(
                "exchangeable-bond cash election and VWAP averaging optionality are documented but not modeled"
            )
        if exchangeable_terms.get("share_redemption_option"):
            combined_warnings.append(
                "exchangeable-bond share-redemption election is not modeled; conversion uses the conservative stored cutoff"
            )
    if any(call.model_type == "soft_call" for call in contract.calls):
        combined_warnings.append(
            "soft-call valuation uses a lattice-date barrier and can be step-sensitive; compare at least two step counts"
        )
        combined_warnings.append(
            "reported bond_floor is the uncallable cash investment value; an issuer call can cap fair value below it"
        )
    for call in contract.calls:
        if call.start_date_calendar_status:
            combined_warnings.append(
                "soft-call start date uses a weekday-only approximation; confirm the contractual exchange calendar"
            )
        if (
            call.trigger_days is not None
            or call.trigger_window_days is not None
            or call.last_observation_max_days_before_notice is not None
        ):
            combined_warnings.append(
                "soft-call observation and notice-lookback rules are approximated as an instantaneous barrier; review trigger-days/window terms"
            )
        if call.price_rule == "early_redemption_amount":
            combined_warnings.append(
                "soft-call Early Redemption Amount is approximated by the static call price"
            )
        if call.trigger_basis != "conversion_price":
            combined_warnings.append(
                "soft-call trigger uses a dynamic Early Redemption Amount/conversion-ratio basis; the lattice approximates it with a static conversion-price barrier"
            )
    return Diagnostics(
        steps=max(0, int(details.get("effective_steps", assumptions.steps))),
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
