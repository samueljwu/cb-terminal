"""Validation gates for valuation-ready market-history CSVs.

Raw dealer quote exports and daily equity/FX files are not model inputs. They
must first become valuation-ready rows with explicit CB identity. This module
checks that those rows belong to the contract being priced.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from cb_terminal.domain import Contract

IDENTITY_COLUMNS = ("cb_instrument_id", "cb_reference_security")
OPTIONAL_IDENTITY_COLUMNS = ("cb_contract_id",)


@dataclass(frozen=True)
class MarketHistoryValidationResult:
    row_count: int
    checked_identity_rows: int
    errors: list[str]
    warnings: list[str]

    @property
    def ok(self) -> bool:
        return not self.errors

    def raise_for_errors(self) -> None:
        if self.errors:
            raise ValueError("market-history validation failed: " + "; ".join(self.errors))


def validate_market_history_file_for_contract(path: str | Path, contract: Contract) -> MarketHistoryValidationResult:
    """Validate that a valuation-ready CSV is aligned with ``contract``.

    Priced rows fail closed: if a row has a CB price, it must carry CB identity
    columns. This blocks one issuer quote history from being priced with another
    issuer's equity/FX history.
    """

    csv_path = Path(path)
    errors: list[str] = []
    warnings: list[str] = []
    allowed_instruments = _allowed_cb_identifiers(contract)
    allowed_reference_securities = _allowed_reference_securities(contract)
    with csv_path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            return MarketHistoryValidationResult(0, 0, ["market-history CSV must include a header row"], [])
        fieldnames = {_normalize_header(name): name for name in reader.fieldnames if name is not None}
        row_count = 0
        checked_rows = 0
        missing_identity = [name for name in IDENTITY_COLUMNS if name not in fieldnames]
        for source_row, raw in enumerate(reader, start=2):
            if _blank_row(raw.values()):
                continue
            row_count += 1
            if _blank(_value(raw, "bond_price")):
                continue
            if missing_identity:
                errors.append(
                    f"row {source_row}: priced market-history rows require identity column(s): "
                    + ", ".join(missing_identity)
                )
                continue
            checked_rows += 1
            cb_instrument_id = _value(raw, fieldnames["cb_instrument_id"])
            cb_reference_security = _value(raw, fieldnames["cb_reference_security"])
            cb_contract_id = _value(raw, fieldnames.get("cb_contract_id", ""))
            _validate_cross_currency_fx(raw, fieldnames, contract, source_row, errors)
            instrument_matches = cb_instrument_id in allowed_instruments
            if not instrument_matches:
                errors.append(
                    f"row {source_row}: cb_instrument_id {cb_instrument_id!r} is not in allowed CB identifiers "
                    f"{sorted(allowed_instruments)!r}"
                )
            if cb_reference_security and cb_reference_security not in allowed_reference_securities:
                message = (
                    f"row {source_row}: cb_reference_security {cb_reference_security!r} is not in allowed reference securities "
                    f"{sorted(allowed_reference_securities)!r}"
                )
                if instrument_matches:
                    warnings.append(message + "; accepted because ISIN/canonical cb_instrument_id matched")
                else:
                    errors.append(message)
            if cb_contract_id and cb_contract_id != contract.id:
                errors.append(
                    f"row {source_row}: cb_contract_id {cb_contract_id!r} does not match contract id {contract.id!r}"
                )
    if row_count and checked_rows == 0 and not errors:
        warnings.append("no priced rows carried CB identity provenance")
    return MarketHistoryValidationResult(row_count, checked_rows, errors, warnings)


def _validate_cross_currency_fx(
    row: dict[str, str], fieldnames: dict[str, str], contract: Contract, source_row: int, errors: list[str]
) -> None:
    contract_currency = _normalize_currency(contract.currency)
    settlement_currency = _normalize_currency(contract.settlement_currency)
    default_bond_currency = settlement_currency or contract_currency
    contract_stock_currency = _normalize_currency(contract.stock_currency or default_bond_currency)
    row_stock_currency = _normalize_currency(_value(row, fieldnames.get("stock_currency")) or contract_stock_currency)
    row_bond_currency = _normalize_currency(_value(row, fieldnames.get("bond_price_currency")) or default_bond_currency)
    allowed_bond_currencies = {currency for currency in (contract_currency, settlement_currency) if currency}
    if allowed_bond_currencies and row_bond_currency not in allowed_bond_currencies:
        errors.append(
            f"row {source_row}: bond_price_currency {row_bond_currency!r} does not match contract currency "
            f"{contract_currency!r} or settlement currency {settlement_currency!r}"
        )
    if not row_stock_currency or not row_bond_currency or row_stock_currency == row_bond_currency:
        return
    fx_rate_text = _value(row, fieldnames.get("market_fx_rate"))
    if "market_fx_rate" not in fieldnames or _blank(fx_rate_text):
        errors.append(
            f"row {source_row}: market_fx_rate is required for cross-currency market history "
            f"({row_stock_currency} stock vs {row_bond_currency} CB); add the daily FX rate to the canonical market-history CSV"
        )
    else:
        try:
            fx_rate = float(fx_rate_text)
        except ValueError:
            errors.append(f"row {source_row}: market_fx_rate must be numeric for cross-currency market history")
        else:
            if fx_rate <= 0:
                errors.append(f"row {source_row}: market_fx_rate must be positive for cross-currency market history")
    fx_convention = _value(row, fieldnames.get("fx_convention")).upper()
    if "fx_convention" not in fieldnames or _blank(fx_convention):
        errors.append(
            f"row {source_row}: fx_convention is required for cross-currency market history "
            f"({row_stock_currency} stock vs {row_bond_currency} CB); expected STOCK_PER_CB or CB_PER_STOCK"
        )
    elif fx_convention not in {"STOCK_PER_CB", "CB_PER_STOCK"}:
        errors.append(
            f"row {source_row}: fx_convention {fx_convention!r} is invalid for cross-currency market history; "
            "expected STOCK_PER_CB or CB_PER_STOCK"
        )


def _normalize_currency(value: object) -> str:
    return str(value or "").strip().upper()


def _allowed_cb_identifiers(contract: Contract) -> set[str]:
    instrument = contract.metadata.get("instrument", {}) if contract.metadata else {}
    values: set[str] = set()
    canonical_id = str(instrument.get("canonical_id") or "").strip()
    canonical_type = str(instrument.get("canonical_id_type") or "").strip().upper()
    if canonical_id:
        values.add(canonical_id)
    elif canonical_type == "PENDING_ISIN":
        values.add(contract.id)
    for key in ("bloomberg_ids", "aliases"):
        for value in instrument.get(key, []) or []:
            if value:
                values.add(str(value))
    if canonical_id:
        values.discard(contract.id)
    return values


def _allowed_reference_securities(contract: Contract) -> set[str]:
    instrument = contract.metadata.get("instrument", {}) if contract.metadata else {}
    values = set(_allowed_cb_identifiers(contract))
    description_alias = str(contract.description or "").strip()
    if description_alias:
        values.add(description_alias)
    for value in instrument.get("aliases", []) or []:
        if value:
            values.add(str(value))
    return values


def _normalize_header(value: str) -> str:
    return "_".join((value or "").strip().lower().replace("-", "_").replace(" ", "_").split("_"))


def _blank(value: object) -> bool:
    return value is None or str(value).strip() == ""


def _blank_row(values: Iterable[object]) -> bool:
    return all(_blank(value) for value in values)


def _value(row: dict[str, str], key: str | None) -> str:
    if not key:
        return ""
    return str(row.get(key) or "").strip()
