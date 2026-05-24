"""Canonical instrument identity helpers.

The project uses one durable instrument key at API/database boundaries. Source-
specific names such as ISIN, Bloomberg ticker, and reference security are typed
primary IDs or aliases; contract references use strict machine contract IDs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping


INSTRUMENT_TYPE_PREFIXES = {
    "convertible_bond": "cb",
    "cb": "cb",
    "equity": "equity",
    "fx": "fx",
}

ID_SCHEME_ALIASES = {
    "ISIN": "isin",
    "BLOOMBERG_TICKER": "bloomberg",
    "BLOOMBERG": "bloomberg",
    "PENDING_ISIN": "pending",
    "EXTERNAL": "external",
    "REGISTRY_ID": "registry",
}


@dataclass(frozen=True)
class InstrumentIdentityRef:
    """Stable identity object passed across APIs and persisted with observations."""

    instrument_key: str
    instrument_type: str
    primary_id_scheme: str
    primary_id: str
    display_name: str = ""
    contract_id: str = ""
    display_id: str = ""
    aliases: tuple[str, ...] = field(default_factory=tuple)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "instrument_key": self.instrument_key,
            "instrument_type": self.instrument_type,
            "primary_id_scheme": self.primary_id_scheme,
            "primary_id": self.primary_id,
        }
        if self.display_name:
            payload["display_name"] = self.display_name
        if self.contract_id:
            payload["contract_id"] = self.contract_id
        if self.display_id:
            payload["display_id"] = self.display_id
        if self.aliases:
            payload["aliases"] = list(self.aliases)
        return payload

    def all_ids(self) -> set[str]:
        return {value for value in (self.primary_id, self.contract_id, *self.aliases) if value}


def contract_id_from_isin(isin: str) -> str:
    """Return the strict machine contract id for a finalized CB ISIN."""

    value = str(isin or "").strip().upper()
    if not _looks_like_isin(value):
        raise ValueError("valid ISIN is required for finalized contract_id")
    return f"{value}_contract"


def contract_display_id(raw: Mapping[str, Any]) -> str:
    """Return the human PM/display identifier: short name + coupon + maturity year."""

    instrument = raw.get("instrument") if isinstance(raw.get("instrument"), Mapping) else {}
    issuer = raw.get("issuer") if isinstance(raw.get("issuer"), Mapping) else {}
    bond = raw.get("bond") if isinstance(raw.get("bond"), Mapping) else {}
    short_name = str(instrument.get("issuer_short_name") or issuer.get("short_name") or issuer.get("name") or instrument.get("display_name") or raw.get("id") or "").strip()
    short_name = short_name.split()[0] if short_name else "CB"
    coupon = _format_coupon(bond.get("coupon_rate"))
    maturity_year = _maturity_year(raw)
    if maturity_year:
        return f"{short_name} {coupon} {maturity_year[-2:]}"
    return str(instrument.get("display_name") or f"{short_name} {coupon}").strip()


def canonical_contract_id(raw: Mapping[str, Any], *, fallback_id: str = "") -> str:
    """Return the strict machine contract id, preserving draft fallback only without final ISIN."""

    instrument = raw.get("instrument") if isinstance(raw.get("instrument"), Mapping) else {}
    primary_id = str(raw.get("isin") or instrument.get("canonical_id") or "").strip()
    if _looks_like_isin(primary_id):
        return contract_id_from_isin(primary_id)
    return str(raw.get("id") or instrument.get("contract_id") or fallback_id or "").strip()


def instrument_key(instrument_type: str, primary_id_scheme: str, primary_id: str, *, fallback: str = "") -> str:
    """Return one stable typed key for an instrument.

    Examples:
    - convertible_bond + ISIN + XS3236970433 -> cb:isin:XS3236970433
    - equity + BLOOMBERG_TICKER + 6669 TT Equity -> equity:bloomberg:6669TTEQUITY
    """

    prefix = INSTRUMENT_TYPE_PREFIXES.get(_norm_token(instrument_type).lower(), _norm_token(instrument_type).lower())
    scheme = ID_SCHEME_ALIASES.get(_norm_token(primary_id_scheme).upper(), _norm_token(primary_id_scheme).lower() or "external")
    raw_id = str(primary_id or fallback or "").strip()
    if not raw_id:
        raise ValueError("primary_id or fallback is required for instrument identity")
    normalized_id = _normalize_id_value(raw_id, scheme)
    return f"{prefix}:{scheme}:{normalized_id}"


def identity_from_observation(
    *,
    instrument_type: str,
    observed_id: str,
    id_scheme: str = "",
    display_name: str = "",
    contract_id: str = "",
    aliases: tuple[str, ...] = (),
) -> InstrumentIdentityRef:
    scheme = id_scheme or _infer_id_scheme(instrument_type, observed_id)
    return InstrumentIdentityRef(
        instrument_key=instrument_key(instrument_type, scheme, observed_id),
        instrument_type=_canonical_instrument_type(instrument_type),
        primary_id_scheme=scheme.upper(),
        primary_id=str(observed_id or "").strip(),
        display_name=display_name,
        contract_id=contract_id,
        aliases=tuple(alias for alias in aliases if alias),
    )


def cb_identity_from_contract(raw: Mapping[str, Any], *, fallback_id: str = "") -> InstrumentIdentityRef:
    instrument = raw.get("instrument") if isinstance(raw.get("instrument"), Mapping) else {}
    contract_id = canonical_contract_id(raw, fallback_id=fallback_id)
    primary_id = str(raw.get("isin") or instrument.get("canonical_id") or "").strip()
    scheme = str(instrument.get("canonical_id_type") or ("ISIN" if _looks_like_isin(primary_id) else "PENDING_ISIN" if primary_id == "PENDING_ISIN" else "REGISTRY_ID")).strip().upper()
    if not primary_id or primary_id == "PENDING_ISIN":
        primary_id = contract_id
        scheme = "PENDING_ISIN" if scheme == "PENDING_ISIN" else "REGISTRY_ID"
    aliases = instrument.get("aliases") if isinstance(instrument.get("aliases"), list) else []
    display_name = str(instrument.get("display_name") or raw.get("display_name") or contract_id).strip()
    registry_key = str(instrument.get("registry_id") or instrument.get("instrument_key") or "").strip()
    key = registry_key if registry_key.startswith("cb:") else instrument_key("convertible_bond", scheme, primary_id, fallback=contract_id)
    return InstrumentIdentityRef(
        instrument_key=key,
        instrument_type="convertible_bond",
        primary_id_scheme=scheme,
        primary_id=primary_id,
        display_name=display_name,
        contract_id=contract_id,
        display_id=contract_display_id(raw),
        aliases=tuple(str(alias).strip() for alias in aliases if str(alias).strip()),
    )


def _infer_id_scheme(instrument_type: str, observed_id: str) -> str:
    if _canonical_instrument_type(instrument_type) == "convertible_bond" and _looks_like_isin(observed_id):
        return "ISIN"
    if _canonical_instrument_type(instrument_type) in {"equity", "fx"}:
        return "BLOOMBERG_TICKER"
    return "EXTERNAL"


def _canonical_instrument_type(value: str) -> str:
    norm = _norm_token(value).lower()
    if norm in {"cb", "convertiblebond", "convertible_bond"}:
        return "convertible_bond"
    return norm or "unknown"


def _looks_like_isin(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", str(value or "").strip().upper()))


def _normalize_id_value(value: str, scheme: str) -> str:
    text = str(value or "").strip()
    if scheme in {"isin", "bloomberg", "external", "registry", "pending"}:
        return re.sub(r"\s+", "", text.upper())
    return re.sub(r"\s+", "", text)


def _format_coupon(value: Any) -> str:
    if value is None or str(value).strip() == "":
        return "0"
    number = float(str(value).strip().rstrip("%"))
    if 0 < abs(number) < 1.0:
        number *= 100.0
    return f"{number:.4f}".rstrip("0").rstrip(".") or "0"


def _maturity_year(raw: Mapping[str, Any]) -> str:
    instrument = raw.get("instrument") if isinstance(raw.get("instrument"), Mapping) else {}
    bond = raw.get("bond") if isinstance(raw.get("bond"), Mapping) else {}
    for value in (bond.get("maturity_date"), instrument.get("maturity_date"), instrument.get("maturity_year")):
        text = str(value or "").strip()
        if len(text) >= 4 and text[:4].isdigit():
            return text[:4]
    return ""


def _norm_token(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", str(value or "").strip()).strip("_")
