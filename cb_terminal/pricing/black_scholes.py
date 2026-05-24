"""Dependency-light Black-Scholes helpers."""

from __future__ import annotations

import math


def norm_cdf(x: float) -> float:
    """Standard normal cumulative distribution using math.erf."""

    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _d1_d2(spot: float, strike: float, maturity: float, rate: float, carry: float, vol: float) -> tuple[float, float]:
    if maturity <= 0 or vol <= 0:
        raise ValueError("maturity and vol must be positive for d1/d2")
    sigma_t = vol * math.sqrt(maturity)
    d1 = (math.log(spot / strike) + (carry + 0.5 * vol * vol) * maturity) / sigma_t
    return d1, d1 - sigma_t


def black_scholes_call(
    spot: float,
    strike: float,
    maturity: float,
    rate: float,
    vol: float,
    dividend_yield: float = 0.0,
    borrow_rate: float = 0.0,
) -> float:
    """European call with continuous dividend/borrow stock carry."""

    if maturity <= 0:
        return max(spot - strike, 0.0)
    if vol <= 0:
        forward_spot = spot * math.exp((rate - dividend_yield - borrow_rate) * maturity)
        return math.exp(-rate * maturity) * max(forward_spot - strike, 0.0)
    carry = rate - dividend_yield - borrow_rate
    d1, d2 = _d1_d2(spot, strike, maturity, rate, carry, vol)
    return spot * math.exp((carry - rate) * maturity) * norm_cdf(d1) - strike * math.exp(-rate * maturity) * norm_cdf(d2)


def black_scholes_put(
    spot: float,
    strike: float,
    maturity: float,
    rate: float,
    vol: float,
    dividend_yield: float = 0.0,
    borrow_rate: float = 0.0,
) -> float:
    """European put with continuous dividend/borrow stock carry."""

    if maturity <= 0:
        return max(strike - spot, 0.0)
    if vol <= 0:
        forward_spot = spot * math.exp((rate - dividend_yield - borrow_rate) * maturity)
        return math.exp(-rate * maturity) * max(strike - forward_spot, 0.0)
    carry = rate - dividend_yield - borrow_rate
    d1, d2 = _d1_d2(spot, strike, maturity, rate, carry, vol)
    return strike * math.exp(-rate * maturity) * norm_cdf(-d2) - spot * math.exp((carry - rate) * maturity) * norm_cdf(-d1)
