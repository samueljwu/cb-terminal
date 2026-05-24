"""Domain dataclasses for the clean cb-terminal core."""

from cb_terminal.domain.serialization import DomainJSONEncoder, DomainSerializable, dump_json, dumps_json, to_jsonable
from cb_terminal.domain.models import (
    Assumptions,
    CallSchedule,
    Contract,
    ConversionTerms,
    CouponSchedule,
    Diagnostics,
    FXConvention,
    MarketRow,
    MarketSnapshot,
    PutSchedule,
    PricingResult,
)

__all__ = [
    "Assumptions",
    "CallSchedule",
    "Contract",
    "ConversionTerms",
    "CouponSchedule",
    "Diagnostics",
    "DomainJSONEncoder",
    "DomainSerializable",
    "FXConvention",
    "MarketRow",
    "MarketSnapshot",
    "PricingResult",
    "PutSchedule",
    "dump_json",
    "dumps_json",
    "to_jsonable",
]
