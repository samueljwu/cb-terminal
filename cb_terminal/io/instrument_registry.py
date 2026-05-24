"""Instrument identity registry helpers.

The registry is small and JSON-backed. It gives pricing and history code one
place to resolve a security before joining raw quote IDs, Bloomberg labels,
prospectus names, and generated CSVs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class InstrumentIdentity:
    """Canonical identity for a traded or reference instrument."""

    registry_id: str
    instrument_type: str
    display_name: str
    issuer_legal_name: str = ""
    issuer_short_name: str = ""
    maturity_date: str = ""
    canonical_id: str = ""
    canonical_id_type: str = ""
    contract_id: str = ""
    bloomberg_ids: tuple[str, ...] = field(default_factory=tuple)
    aliases: tuple[str, ...] = field(default_factory=tuple)
    deal_names: tuple[str, ...] = field(default_factory=tuple)
    underlying_instrument_id: str = ""
    fx_instrument_id: str = ""
    price_currency: str = ""
    stock_currency: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def primary_id(self) -> str:
        """Identifier to use for stored market observations.

        For finalized convertible bonds this should be the ISIN.  Draft or
        indicative contracts may temporarily use a registry id, but their
        ``canonical_id_type`` must make that limitation explicit.
        """

        return self.canonical_id or self.registry_id

    def all_identifiers(self) -> set[str]:
        values = {
            self.registry_id,
            self.canonical_id,
            self.contract_id,
            *self.bloomberg_ids,
            *self.aliases,
            *self.deal_names,
            self.display_name,
        }
        return {value.strip() for value in values if value and value.strip()}


def load_instrument_registry(path: str | Path) -> list[InstrumentIdentity]:
    """Load an instrument registry JSON file."""

    with Path(path).open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    records = payload.get("instruments", payload if isinstance(payload, list) else [])
    return [instrument_from_dict(record) for record in records]


def instrument_from_dict(raw: dict[str, Any]) -> InstrumentIdentity:
    return InstrumentIdentity(
        registry_id=str(raw.get("registry_id", "")).strip(),
        instrument_type=str(raw.get("instrument_type", "")).strip(),
        display_name=str(raw.get("display_name", "")).strip(),
        issuer_legal_name=str(raw.get("issuer_legal_name", "")).strip(),
        issuer_short_name=str(raw.get("issuer_short_name", "")).strip(),
        maturity_date=str(raw.get("maturity_date", "")).strip(),
        canonical_id=str(raw.get("canonical_id", "")).strip(),
        canonical_id_type=str(raw.get("canonical_id_type", "")).strip().upper(),
        contract_id=str(raw.get("contract_id", "")).strip(),
        bloomberg_ids=_tuple(raw.get("bloomberg_ids", [])),
        aliases=_tuple(raw.get("aliases", [])),
        deal_names=_tuple(raw.get("deal_names", [])),
        underlying_instrument_id=str(raw.get("underlying_instrument_id", "")).strip(),
        fx_instrument_id=str(raw.get("fx_instrument_id", "")).strip(),
        price_currency=str(raw.get("price_currency", "")).strip(),
        stock_currency=str(raw.get("stock_currency", "")).strip(),
        raw=dict(raw),
    )


def require_unique_primary_ids(instruments: Iterable[InstrumentIdentity]) -> None:
    seen: dict[str, str] = {}
    for instrument in instruments:
        primary_id = instrument.primary_id
        if not primary_id:
            raise ValueError(f"instrument {instrument.registry_id!r} has no primary identifier")
        previous = seen.get(primary_id)
        if previous is not None:
            raise ValueError(f"duplicate primary instrument id {primary_id!r}: {previous!r} and {instrument.registry_id!r}")
        seen[primary_id] = instrument.registry_id


def find_instrument(instruments: Iterable[InstrumentIdentity], identifier: str) -> InstrumentIdentity | None:
    needle = _norm(identifier)
    if not needle:
        return None
    matches = [instrument for instrument in instruments if needle in {_norm(value) for value in instrument.all_identifiers()}]
    if len(matches) > 1:
        names = ", ".join(sorted(match.registry_id for match in matches))
        raise ValueError(f"identifier {identifier!r} is ambiguous across instruments: {names}")
    return matches[0] if matches else None


def cb_display_name(issuer_short_name: str, maturity_date: str, coupon_rate: float | str | None = 0.0) -> str:
    """Return standardized CB display name, e.g. ``Issuer 0 31``."""

    issuer = " ".join((issuer_short_name or "").strip().split())
    if not issuer:
        raise ValueError("issuer_short_name is required for display-name generation")
    year = (maturity_date or "").strip()[:4]
    if len(year) != 4 or not year.isdigit():
        raise ValueError("maturity_date must start with a four-digit year")
    return f"{issuer} {_coupon_display(coupon_rate)} {year[-2:]}"


def _coupon_display(value: float | str | None) -> str:
    if value is None or str(value).strip() == "":
        return "0"
    number = float(str(value).strip().rstrip("%"))
    if 0 < abs(number) < 1.0:
        number *= 100.0
    text = f"{number:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def _tuple(values: Any) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        values = [values]
    return tuple(str(value).strip() for value in values if str(value).strip())


def _norm(value: str) -> str:
    return " ".join((value or "").strip().casefold().split())
