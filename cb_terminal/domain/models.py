"""Typed domain objects for convertible-bond pricing.

These are small dataclasses, not framework-bound models. They separate deal
terms, market data, and assumptions so pricing runs are reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any, Dict, List, Optional

from cb_terminal.domain.serialization import DomainSerializable


class FXConvention(Enum):
    """Direction of an FX rate between CB and stock currencies.

    Project convention is STOCK_PER_CB: quote FX as stock currency per one unit
    of the CB/output currency, e.g. TWD per USD for a USD CB convertible into
    TWD shares. CB_PER_STOCK is retained only to read legacy data explicitly
    labelled in the inverse direction.
    """

    STOCK_PER_CB = "STOCK_PER_CB"
    CB_PER_STOCK = "CB_PER_STOCK"


@dataclass(frozen=True)
class CouponSchedule(DomainSerializable):
    """Coupon approximation used by the lattice.

    coupon_rate is annual, expressed as decimal (0.03 for 3%).  Frequency of
    zero disables coupons.  The current lattice pays coupons at time steps that
    align approximately with coupon dates.
    """

    annual_rate: float = 0.0
    frequency: int = 0


@dataclass(frozen=True)
class ConversionTerms(DomainSerializable):
    underlying_ticker: str
    conversion_price: float
    reference_share_price: Optional[float] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    # Prospectus fixed FX used to turn the stock-currency conversion price into
    # the CB economic currency. None/<=0 means no fixed rate was provided; the
    # pricing engine then falls back to the market FX rate with a warning.
    fixed_fx_rate: Optional[float] = None
    fixed_fx_convention: Optional[FXConvention] = None
    # Some CBs open conversion in multiple disjoint windows.  When populated,
    # these windows take precedence over the outer start/end envelope.
    windows: tuple[tuple[date, date], ...] = ()


@dataclass(frozen=True)
class PutSchedule(DomainSerializable):
    """Holder put.

    model_type may be "scheduled_put" for lattice exercise or "event_put" for
    documentation/future event probability handling.  Only dated scheduled puts
    are exercised in Phase 1.
    """

    put_type: str
    price: float
    date: Optional[date] = None
    model_type: str = "scheduled_put"
    description: str = ""


@dataclass(frozen=True)
class CallSchedule(DomainSerializable):
    """Issuer call terms.

    soft calls are approximated by a single stock barrier equal to
    trigger_ratio * conversion_price after start_date.
    """

    call_type: str
    price: float
    start_date: Optional[date] = None
    start_date_calendar_status: str = ""
    trigger_ratio: Optional[float] = None
    trigger_days: Optional[int] = None
    trigger_window_days: Optional[int] = None
    last_observation_max_days_before_notice: Optional[int] = None
    observation_rule: str = ""
    trigger_basis: str = "conversion_price"
    price_rule: str = ""
    model_type: str = "soft_call"
    description: str = ""


@dataclass(frozen=True)
class Contract(DomainSerializable):
    id: str
    issuer: str
    description: str
    currency: str
    settlement_currency: str
    stock_currency: str
    face: float
    issue_price: float
    maturity_price: float
    pricing_date: date
    maturity_date: date
    coupon: CouponSchedule
    conversion: ConversionTerms
    puts: List[PutSchedule] = field(default_factory=list)
    calls: List[CallSchedule] = field(default_factory=list)
    source: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def maturity_years_from_pricing(self) -> float:
        return max((self.maturity_date - self.pricing_date).days / 365.25, 0.0)


@dataclass(frozen=True)
class Assumptions(DomainSerializable):
    """Run-level valuation assumptions, all decimal rates except where noted."""

    volatility: float
    risk_free_rate: float
    credit_spread: float = 0.0
    borrow_rate: float = 0.0
    dividend_yield: float = 0.0
    steps: int = 100
    valuation_date: Optional[date] = None

    @property
    def equity_carry(self) -> float:
        # Stock forward drift under a simple risk-neutral approximation.
        return self.risk_free_rate - self.dividend_yield - self.borrow_rate


@dataclass(frozen=True)
class MarketSnapshot(DomainSerializable):
    stock_price: float
    stock_currency: Optional[str] = None
    bond_price: Optional[float] = None
    bond_price_currency: Optional[str] = None
    fx_rate: float = 1.0
    fx_convention: Optional[FXConvention] = None
    as_of_date: Optional[date] = None


@dataclass(frozen=True)
class MarketRow(DomainSerializable):
    """Normalized historical market input row for batch valuation."""

    as_of_date: date
    stock_price: float
    bond_price: Optional[float] = None
    market_fx_rate: float = 1.0
    stock_currency: Optional[str] = None
    bond_price_currency: Optional[str] = None
    fx_convention: Optional[FXConvention] = None
    assumption_overrides: Dict[str, float] = field(default_factory=dict)
    source: str = ""
    source_row: int = 0

    def to_market_snapshot(self) -> MarketSnapshot:
        return MarketSnapshot(
            stock_price=self.stock_price,
            stock_currency=self.stock_currency,
            bond_price=self.bond_price,
            bond_price_currency=self.bond_price_currency,
            fx_rate=self.market_fx_rate,
            fx_convention=self.fx_convention,
            as_of_date=self.as_of_date,
        )


@dataclass(frozen=True)
class Diagnostics(DomainSerializable):
    steps: int
    maturity_years: float
    dt: float
    up: float
    down: float
    equity_discount_rate: float
    credit_discount_rate: float
    conversion_ratio: float
    warnings: List[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PricingResult(DomainSerializable):
    fair_value: float
    bond_floor: float
    parity: float
    cheapness: Optional[float]
    implied_volatility: Optional[float]
    diagnostics: Diagnostics
    output_currency: str = ""
