"""Small stdlib HTTP workbench for CB pricing and implied-vol charts.

This module intentionally avoids Flask/FastAPI/Chart.js so the standalone repo can
run in a clean Python environment. It serves:

- GET /                 browser workbench
- GET /help             browser help page
- GET /health           JSON health check
- GET /api/universe     JSON list of covered CB choices
- GET /api/batch-price  JSON historical pricing/IV payload

The API accepts project-relative paths only, preventing browser requests from
reading arbitrary files outside the checkout.
"""

from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import html
import json
import os
import re
import sqlite3
import tempfile
import threading
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from cb_terminal.domain import Assumptions, dumps_json, to_jsonable
from cb_terminal.domain.identity import cb_identity_from_contract, instrument_key
from cb_terminal.io.contract_loader import load_contract_json, loads_contract_json
from cb_terminal.io.market_history import ALIASES as MARKET_HISTORY_ALIASES
from cb_terminal.io.market_history import load_market_history_csv
from cb_terminal.io.market_history_validation import validate_market_history_file_for_contract
from cb_terminal.io.market_data_history import MarketDataPoint, load_market_data_file
from cb_terminal.io.price_history import PriceQuoteRow, load_price_history_file
from cb_terminal.io.yield_curves import (
    SUPPORTED_YIELD_CURVE_CURRENCIES,
    YieldCurve,
    curve_currency_for_contract,
    curve_currency_from_contract_dict,
    fetch_worldgovernmentbonds_curve,
    match_curve_for_contract,
)
from cb_terminal.pricing.batch import ResultRow, price_history
from cb_terminal.pricing.engine import MODEL_VERSION as PRICING_MODEL_VERSION, PricingEngine
from cb_terminal.pricing.nuke import nuke
from cb_terminal.pricing.yields import calculate_market_yields, issuance_yield_checks
from cb_terminal.core.time import backup_timestamp, utc_now_iso
from cb_terminal.prospectus.auto_ingest import (
    approve_reviewed_contract,
    auto_ingest_prospectuses,
    backfill_missing_issuance_economics,
)
from cb_terminal.prospectus.evidence import approval_required_evidence_fields, has_valid_page_evidence
from cb_terminal.prospectus.lifecycle import RawProspectusLifecycle
from cb_terminal.prospectus.review import ReviewIssue, validate_contract_dict
from cb_terminal.prospectus.source_indexes import rewrite_prospectus_indexes, upsert_pending_prospectus
from cb_terminal.prospectus.universe import load_universe
from cb_terminal.storage import AssumptionSetRecord, CbTerminalStore, ValuationRunRecord
from cb_terminal.storage.file_artifacts import sha256_bytes, sha256_file
from cb_terminal.storage.canonical_migration import bootstrap_project_catalog
from cb_terminal.storage.canonical_store import CanonicalStore
from cb_terminal.storage.price_history_store import PriceHistoryStore

def _canonical_store_path() -> Path:
    return resolve_project_path(os.environ.get("CB_TERMINAL_DB_PATH") or DEFAULT_DB)


def canonical_store() -> CanonicalStore:
    return CanonicalStore(_canonical_store_path())


def sync_canonical_catalog() -> dict[str, int]:
    return bootstrap_project_catalog(PROJECT_ROOT, _canonical_store_path())


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_UNIVERSE = "data/coverage/universe.json"
DEFAULT_CONTRACT = "data/contracts/XS3236970433_contract.json"
# The GUI/default API must point at model-ready, multi-date valuation history.
# One-row anchor files under tests/fixtures are sanity-check fixtures only;
# using them here makes valuation metrics appear as a single point instead of a time series.
DEFAULT_MARKET_HISTORY = "data/price_history/generated/XS3236970433_valuation_market_history.csv"
DEFAULT_RAW_PRICE_HISTORY = ""
DEFAULT_DB = "data/cb_terminal.sqlite"
DEFAULT_PRICE_HISTORY_DB = "data/price_history/price_history.sqlite"
FX_CANONICAL_SOURCES = "data/coverage/fx_sources.json"
DEFAULT_VOLATILITY = 0.38
DEFAULT_RISK_FREE_RATE = 0.0
DEFAULT_CREDIT_SPREAD = 0.016
DEFAULT_BORROW_RATE = 0.0
DEFAULT_DIVIDEND_YIELD = 0.0
DEFAULT_STEPS = 250
DEFAULT_MODEL_MODE = "tf_split_tree"
DEFAULT_USE_YIELD_CURVE = False
MAX_TREE_STEPS = 500
MAX_JSON_BODY_BYTES = 1_000_000
MAX_UPLOAD_BODY_BYTES = 70_000_000
MAX_UPLOAD_FILE_BYTES = 50_000_000
RAW_QUOTE_DISPLAY_LIMIT = 5000

RAW_PROSPECTUS_DIR = "data/raw/prospectuses"
CONTRACTS_DIR = "data/contracts"
RAW_PRICE_HISTORY_DIR = "data/price_history/raw"
GENERATED_MARKET_HISTORY_DIR = "data/price_history/generated"
COVERAGE_DIR = "data/coverage"
SOURCE_INVENTORY_KINDS: dict[str, dict[str, Any]] = {
    "raw_prospectus": {"directory": RAW_PROSPECTUS_DIR, "extensions": {".pdf"}, "role": "raw_input", "label": "Raw prospectus PDF"},
    "raw_price_history": {"directory": RAW_PRICE_HISTORY_DIR, "extensions": {".csv", ".xlsx"}, "role": "raw_input", "label": "Raw quote / market data file"},
    "generated_market_history": {"directory": GENERATED_MARKET_HISTORY_DIR, "extensions": {".csv"}, "role": "canonical", "label": "Valuation market history CSV"},
    "contract": {"directory": CONTRACTS_DIR, "extensions": {".json"}, "role": "canonical", "label": "Contract terms JSON"},
}
METRIC_GROUPS: dict[str, dict[str, Any]] = {
    "price_stack": {
        "label": "Valuation Stack",
        "unit": "price / parity points",
        "y_label": "Price / parity",
        "metrics": ["bond_price", "fair_value", "parity", "bond_floor"],
        "overlay_ok": True,
    },
    "cheapness_mini": {
        "label": "Cheap/Rich mini-panel",
        "unit": "price points",
        "y_label": "Fair value - market price",
        "metrics": ["cheapness"],
        "zero_line": True,
    },
    "relative_value_drivers": {
        "label": "Relative Value Drivers",
        "unit": "linked small multiples",
        "link_group": "rv-drivers",
        "panels": [
            {"id": "rv-cheapness", "label": "Cheap/Rich", "unit": "price points", "metrics": ["cheapness"], "zero_line": True},
            {"id": "rv-iv", "label": "Implied vol", "unit": "percent", "metrics": ["implied_volatility"]},
            {"id": "rv-credit-spread", "label": "Credit spread", "unit": "bps", "metrics": ["credit_spread"]},
            {"id": "rv-stock", "label": "Underlying stock", "unit": "stock price", "metrics": ["stock_price"]},
        ],
    },
    "volatility_overlay": {
        "label": "Volatility overlay",
        "unit": "percent",
        "y_label": "Volatility",
        "metrics": ["implied_volatility", "volatility"],
        "overlay_ok": True,
    },
    "credit_spread_bps": {
        "label": "Credit spread (bps)",
        "unit": "bps",
        "y_label": "Credit spread (bps)",
        "metrics": ["credit_spread"],
    },
    "assumption_rates_percent": {
        "label": "Rate / vol assumptions",
        "unit": "percent",
        "y_label": "Rate / volatility",
        "metrics": ["volatility", "risk_free_rate", "borrow_rate", "dividend_yield"],
    },
    "market_fx": {
        "label": "Market FX",
        "unit": "FX rate",
        "y_label": "FX rate",
        "metrics": ["market_fx_rate"],
    },
    "raw_quotes": {
        "label": "Raw quote prices",
        "unit": "CB price",
        "y_label": "CB price",
        "metrics": ["mid_price", "bid_price", "ask_price"],
    },
}
CONTRACT_EDIT_ALLOWLIST: dict[str, str] = {
    "instrument.canonical_id_type": "id_type",
    "instrument.canonical_id": "text",
    "instrument.display_name": "text",
    "instrument.issuer_legal_name": "text",
    "instrument.issuer_short_name": "text",
    "instrument.structure_type": "text",
    "issuer.name": "text",
    "issuer.ticker": "text",
    "guarantor.name": "text",
    "exchangeable_terms.reference_company_name": "text",
    "exchangeable_terms.initial_exchange_property_shares": "optional_positive_float",
    "exchangeable_terms.initial_exchange_ratio": "optional_positive_float",
    "exchangeable_terms.initial_exchange_ratio_principal_basis": "optional_positive_float",
    "exchangeable_terms.cash_averaging_period_trading_days": "nonnegative_int",
    "bond.description": "text",
    "bond.currency": "currency",
    "bond.economic_currency": "currency",
    "bond.settlement_currency": "currency",
    "bond.stock_currency": "currency",
    "bond.denomination": "positive_float",
    "bond.denomination_increment": "optional_positive_float",
    "bond.pricing_face": "positive_float",
    "bond.issue_size": "positive_float",
    "bond.issue_price": "positive_float",
    "bond.brokerage": "optional_nonnegative_float",
    "bond.investor_offer_price": "optional_positive_float",
    "bond.coupon_rate": "float",
    "bond.coupon_frequency": "nonnegative_int",
    "bond.pricing_date": "date",
    "bond.closing_date": "date",
    "bond.maturity_date": "date",
    "bond.day_count": "text",
    "redemption.maturity_price": "positive_float",
    "redemption.yield_to_maturity": "optional_float",
    "redemption.yield_to_maturity_frequency": "optional_nonnegative_int",
    "redemption.calculated_yield_to_maturity": "optional_float",
    "redemption.yield_to_maturity_difference_bps": "optional_float",
    "conversion.underlying_ticker": "text",
    "conversion.underlying_exchange": "text",
    "conversion.reference_share_price": "optional_positive_float",
    "conversion.initial_conversion_price": "positive_float",
    "conversion.conversion_premium": "optional_float",
    "conversion.fixed_exchange_rate": "optional_positive_float",
    "conversion.fixed_exchange_rate_units": "text",
    "conversion.initial_settlement_exchange_rate": "optional_positive_float",
    "conversion.initial_settlement_exchange_rate_units": "text",
    "conversion.start_date": "date",
    "conversion.start_date_rule": "text",
    "conversion.end_date": "date",
    "conversion.end_date_rule": "text",
    "conversion.calendar_status": "text",
    "conversion.conditional_early_start_date": "optional_date",
    "conversion.conditional_early_start_rule": "text",
    "conversion.windows.0.start_date": "optional_date",
    "conversion.windows.0.end_date": "optional_date",
    "conversion.windows.1.start_date": "optional_date",
    "conversion.windows.1.end_date": "optional_date",
    "calls.0.start_date": "optional_date",
    "calls.0.start_date_calendar_status": "text",
    "calls.0.price": "positive_float",
    "calls.0.trigger_ratio": "optional_positive_float",
    "calls.0.trigger_days": "nonnegative_int",
    "calls.0.trigger_window_days": "nonnegative_int",
    "calls.0.last_observation_max_days_before_notice": "nonnegative_int",
    "calls.0.observation_rule": "text",
    "calls.0.trigger_basis": "text",
    "calls.0.price_rule": "text",
    "calls.0.description": "text",
    "puts.0.date": "optional_date",
    "puts.0.price": "positive_float",
    "puts.0.yield_to_put": "optional_float",
    "puts.0.yield_to_put_frequency": "optional_nonnegative_int",
    "puts.0.calculated_yield_to_put": "optional_float",
    "puts.0.yield_to_put_difference_bps": "optional_float",
    "puts.0.description": "text",
    "puts.1.date": "optional_date",
    "puts.1.price": "positive_float",
    "puts.1.yield_to_put": "optional_float",
    "puts.1.yield_to_put_frequency": "optional_nonnegative_int",
    "puts.1.calculated_yield_to_put": "optional_float",
    "puts.1.yield_to_put_difference_bps": "optional_float",
    "puts.1.description": "text",
}
UNIT_CHANGING_CONTRACT_FIELDS = {"bond.currency", "bond.economic_currency", "bond.settlement_currency", "bond.stock_currency", "conversion.fixed_exchange_rate_units", "conversion.initial_settlement_exchange_rate_units"}
CONTRACT_FIELD_LABELS: dict[str, str] = {
    "instrument.canonical_id_type": "Identifier status",
    "instrument.canonical_id": "ISIN",
    "instrument.display_name": "PM name",
    "instrument.issuer_legal_name": "Legal issuer",
    "instrument.issuer_short_name": "Short issuer",
    "instrument.structure_type": "Structure type",
    "issuer.name": "Issuer",
    "issuer.ticker": "Ticker",
    "guarantor.name": "Guarantor",
    "exchangeable_terms.reference_company_name": "Reference company",
    "exchangeable_terms.initial_exchange_property_shares": "Initial exchange-property shares",
    "exchangeable_terms.initial_exchange_ratio": "Initial exchange ratio",
    "exchangeable_terms.initial_exchange_ratio_principal_basis": "Exchange-ratio principal basis",
    "exchangeable_terms.cash_averaging_period_trading_days": "Cash averaging period (trading days)",
    "bond.description": "Bond description",
    "bond.currency": "CB currency",
    "bond.economic_currency": "Economic / risk currency",
    "bond.settlement_currency": "Settlement currency",
    "bond.stock_currency": "Stock currency",
    "bond.denomination": "Denomination",
    "bond.denomination_increment": "Denomination increment",
    "bond.pricing_face": "Pricing face",
    "bond.issue_size": "Issue size",
    "bond.issue_price": "Issue price (per 100)",
    "bond.brokerage": "Brokerage (%)",
    "bond.investor_offer_price": "Investor offer price (per 100)",
    "bond.coupon_rate": "Coupon",
    "bond.coupon_frequency": "Coupon frequency",
    "bond.pricing_date": "Pricing date",
    "bond.closing_date": "Closing date",
    "bond.maturity_date": "Maturity",
    "bond.day_count": "Day count",
    "redemption.maturity_price": "Maturity redemption price",
    "redemption.yield_to_maturity": "Quoted YTM (%)",
    "redemption.yield_to_maturity_frequency": "YTM compounding periods / year",
    "redemption.calculated_yield_to_maturity": "Calculated issue YTM (%)",
    "redemption.yield_to_maturity_difference_bps": "Calculated minus quoted YTM (bp)",
    "conversion.underlying_ticker": "Underlying ticker",
    "conversion.underlying_exchange": "Underlying exchange",
    "conversion.reference_share_price": "Reference share price",
    "conversion.initial_conversion_price": "Initial conversion price",
    "conversion.conversion_premium": "Conversion premium (%)",
    "conversion.fixed_exchange_rate": "Fixed FX rate",
    "conversion.fixed_exchange_rate_units": "Fixed FX convention",
    "conversion.initial_settlement_exchange_rate": "Initial settlement FX",
    "conversion.initial_settlement_exchange_rate_units": "Initial settlement FX convention",
    "conversion.start_date": "Conversion start",
    "conversion.start_date_rule": "Conversion start rule",
    "conversion.end_date": "Conversion end",
    "conversion.end_date_rule": "Conversion end rule",
    "conversion.calendar_status": "Calendar resolution status",
    "conversion.conditional_early_start_date": "Conditional early conversion start",
    "conversion.conditional_early_start_rule": "Conditional early conversion rule",
    "conversion.windows.0.start_date": "First conversion window start",
    "conversion.windows.0.end_date": "First conversion window end",
    "conversion.windows.1.start_date": "Second conversion window start",
    "conversion.windows.1.end_date": "Second conversion window end",
    "calls.0.start_date": "Call start",
    "calls.0.start_date_calendar_status": "Call-start calendar status",
    "calls.0.price": "Call price",
    "calls.0.trigger_ratio": "Call trigger",
    "calls.0.trigger_days": "Trigger observation days",
    "calls.0.trigger_window_days": "Trigger window days",
    "calls.0.last_observation_max_days_before_notice": "Last observation before notice (days)",
    "calls.0.observation_rule": "Call observation rule",
    "calls.0.trigger_basis": "Call trigger basis",
    "calls.0.price_rule": "Call price rule",
    "calls.0.description": "Call description",
    "puts.0.date": "Put date",
    "puts.0.price": "Put price",
    "puts.0.yield_to_put": "Yield to first put (%)",
    "puts.0.yield_to_put_frequency": "First-put compounding periods / year",
    "puts.0.calculated_yield_to_put": "Calculated issue yield to first put (%)",
    "puts.0.yield_to_put_difference_bps": "Calculated minus quoted first-put yield (bp)",
    "puts.0.description": "Put description",
    "puts.1.date": "Second put date",
    "puts.1.price": "Second put price",
    "puts.1.yield_to_put": "Yield to second put (%)",
    "puts.1.yield_to_put_frequency": "Second-put compounding periods / year",
    "puts.1.calculated_yield_to_put": "Calculated issue yield to second put (%)",
    "puts.1.yield_to_put_difference_bps": "Calculated minus quoted second-put yield (bp)",
    "puts.1.description": "Second put description",
}
DERIVED_CONTRACT_FIELDS: set[str] = {
    "bond.investor_offer_price",
    "redemption.calculated_yield_to_maturity",
    "redemption.yield_to_maturity_difference_bps",
    "puts.0.calculated_yield_to_put",
    "puts.0.yield_to_put_difference_bps",
    "puts.1.calculated_yield_to_put",
    "puts.1.yield_to_put_difference_bps",
}
PRIMARY_CONTRACT_FIELDS: set[str] = {
    "instrument.canonical_id_type",
    "instrument.canonical_id",
    "issuer.name",
    "bond.currency",
    "bond.issue_size",
    "bond.issue_price",
    "bond.brokerage",
    "bond.investor_offer_price",
    "bond.coupon_rate",
    "bond.maturity_date",
    "redemption.maturity_price",
    "redemption.yield_to_maturity",
    "redemption.yield_to_maturity_frequency",
    "redemption.calculated_yield_to_maturity",
    "redemption.yield_to_maturity_difference_bps",
    "puts.0.date",
    "puts.0.price",
    "puts.0.yield_to_put",
    "puts.0.yield_to_put_frequency",
    "puts.0.calculated_yield_to_put",
    "puts.0.yield_to_put_difference_bps",
    "conversion.underlying_ticker",
    "conversion.initial_conversion_price",
    "conversion.conversion_premium",
}
CONTRACT_FIELD_HELP: dict[str, str] = {
    "instrument.canonical_id_type": "How this bond is identified. A final ISIN is required before market data can be matched.",
    "instrument.canonical_id": "The final 12-character ISIN. A Common Code is supporting evidence, not the canonical identifier.",
    "bond.issue_price": "Price paid to the issuer, quoted per 100 of principal.",
    "bond.brokerage": "Investor-paid brokerage in percentage points of principal.",
    "bond.investor_offer_price": "Calculated automatically as issue price + brokerage.",
    "redemption.yield_to_maturity": "Gross yield quoted in the source. Brokerage is not deducted from this stated yield.",
    "redemption.yield_to_maturity_frequency": "Compounding frequency stated in the source; 2 means semi-annual.",
    "redemption.calculated_yield_to_maturity": "Independently solved from gross issue price, closing date, promised coupons, and maturity redemption.",
    "redemption.yield_to_maturity_difference_bps": "Calculated issue yield minus the prospectus quote. A material difference blocks approval.",
    "puts.0.yield_to_put": "Gross stated yield to the first scheduled put.",
    "puts.0.yield_to_put_frequency": "Compounding frequency for the first scheduled put yield.",
    "puts.0.calculated_yield_to_put": "Independently solved from gross issue price to the first dated holder put.",
    "puts.0.yield_to_put_difference_bps": "Calculated issue yield-to-put minus the prospectus quote.",
}
CONTRACT_FIELD_CHOICES: dict[str, list[dict[str, str]]] = {
    "instrument.canonical_id_type": [
        {"value": "ISIN", "label": "ISIN assigned"},
        {"value": "PENDING_ISIN", "label": "ISIN pending"},
    ],
}
REVIEW_QUEUE_ITEM_ALLOWED_KEYS: set[str] = {
    "prospectus_id",
    "status",
    "review_status",
    "source_path",
    "source_file",
    "source_filename",
    "source_sha256",
    "contract_id",
    "contract_path",
    "review_path",
    "message",
    "blocker",
    "sha256",
    "raw_delete_allowed_after_review",
    "evidence_status",
    "missing_required_evidence",
    "duplicate_of",
    "issuer_hint",
    "document_type_hint",
    "extraction",
    "instrument_display_name",
    "instrument_short_name",
    "instrument_legal_name",
    "instrument_raw_display_name",
    "issuer_legal_name",
    "issuer_short_name",
    "coupon_rate",
    "maturity_date",
    "currency",
    "issue_size",
    "conversion_price",
    "underlying_ticker",
}
CONTRACT_REVIEW_GROUPS: list[tuple[str, str, tuple[str, ...]]] = [
    (
        "identity",
        "Identity",
        (
            "instrument.",
            "issuer.",
            "guarantor.",
            "exchangeable_terms.reference_company_name",
            "bond.description",
            "conversion.underlying_ticker",
            "conversion.underlying_exchange",
        ),
    ),
    (
        "economics",
        "Economics",
        (
            "bond.currency",
            "bond.economic_currency",
            "bond.settlement_currency",
            "bond.stock_currency",
            "bond.denomination",
            "bond.denomination_increment",
            "bond.pricing_face",
            "bond.issue_size",
            "bond.issue_price",
            "bond.brokerage",
            "bond.investor_offer_price",
            "bond.coupon_rate",
            "bond.coupon_frequency",
            "redemption.",
            "conversion.reference_share_price",
            "conversion.initial_conversion_price",
            "conversion.conversion_premium",
            "conversion.fixed_exchange_rate",
            "conversion.fixed_exchange_rate_units",
            "conversion.initial_settlement_exchange_rate",
            "conversion.initial_settlement_exchange_rate_units",
            "exchangeable_terms.initial_exchange_property_shares",
            "exchangeable_terms.initial_exchange_ratio",
            "exchangeable_terms.initial_exchange_ratio_principal_basis",
            "exchangeable_terms.cash_averaging_period_trading_days",
        ),
    ),
    ("dates", "Dates", ("bond.pricing_date", "bond.closing_date", "bond.maturity_date", "conversion.start_date", "conversion.start_date_rule", "conversion.conditional_early_start_date", "conversion.conditional_early_start_rule", "conversion.windows.", "conversion.end_date", "conversion.end_date_rule", "conversion.calendar_status", "calls.0.start_date", "puts.0.date", "puts.1.date")),
    ("special_clauses", "Special clauses", ("calls.0.", "puts.0.", "puts.1.", "bond.day_count")),
]
UPLOAD_KINDS: dict[str, dict[str, Any]] = {
    "prospectus": {"directory": "data/raw/prospectuses", "extensions": {".pdf"}, "parse": False},
    "market_data_auto": {"directory": "data/price_history/raw", "extensions": {".csv", ".xlsx"}, "parse": True},
    # Legacy upload kinds are kept for tests/CLI compatibility; the browser only exposes market_data_auto.
    "raw_price_history": {"directory": "data/price_history/raw", "extensions": {".csv", ".xlsx"}, "parse": True, "legacy": True},
    "market_data_history": {"directory": "data/price_history/raw", "extensions": {".csv", ".xlsx"}, "parse": True, "legacy": True},
    "market_history_csv": {"directory": "data/price_history/generated", "extensions": {".csv"}, "parse": True, "legacy": True},
}
MODEL_VERSION = f"{DEFAULT_MODEL_MODE}:{PRICING_MODEL_VERSION}"
_YIELD_CURVE_CACHE: dict[str, YieldCurve] = {}
_MARKET_HISTORY_UPDATE_LOCKS: dict[str, threading.RLock] = {}
_MARKET_HISTORY_UPDATE_LOCKS_GUARD = threading.Lock()
_UNIVERSE_UPDATE_LOCK = threading.RLock()


def resolve_project_path(value: str | Path) -> Path:
    """Resolve a path inside the project checkout.

    Absolute paths are allowed only if they remain under PROJECT_ROOT. Relative
    paths are interpreted from PROJECT_ROOT. This keeps the local API useful for
    demos while preventing path traversal.
    """

    path = Path(value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    resolved = path.resolve()
    try:
        resolved.relative_to(PROJECT_ROOT.resolve())
    except ValueError as exc:
        raise ValueError(f"path escapes project root: {value}") from exc
    return resolved


def build_universe_payload(universe_path: str | Path = DEFAULT_UNIVERSE) -> dict[str, Any]:
    """Return CB choices for the GUI dropdown and PM workbench.

    The dropdown is contract-first, not pricing-only.  The static coverage
    universe contributes valuation-ready names with market history; prospectus
    intake contributes newly extracted draft contracts from review_queue.json so
    a freshly extracted CB immediately appears in the PM view and single-name
    controls even before market history has been loaded.
    """

    universe_file = resolve_project_path(universe_path)
    canonical_sync = _safe_sync_canonical_catalog()
    items: list[dict[str, Any]] = []
    seen_contract_paths: set[str] = set()
    for item in load_universe(universe_file):
        contract_path = resolve_project_path(item.contract_path)
        market_path = resolve_project_path(item.market_history_path) if item.market_history_path else None
        raw_price_history_path = getattr(item, "raw_price_history_path", "")
        raw_price_path = resolve_project_path(raw_price_history_path) if raw_price_history_path else None
        payload_item = _universe_payload_item_from_contract(
            contract_path=contract_path,
            fallback_id=item.id,
            fallback_issuer=item.issuer,
            fallback_underlying=item.underlying_ticker,
            fallback_isin=item.isin,
            fallback_status=item.status,
            market_path=market_path,
            raw_price_path=raw_price_path,
            source="coverage_universe",
        )
        payload_item = _enrich_payload_item_with_canonical_store(payload_item, canonical_sync)
        items.append(payload_item)
        if payload_item["contract_path"]:
            seen_contract_paths.add(payload_item["contract_path"])

    review_queue_path = universe_file.parent / "review_queue.json"
    for queue_item in _load_json_list(review_queue_path, default=[]):
        if not isinstance(queue_item, Mapping) or not queue_item.get("contract_path"):
            continue
        try:
            contract_path = resolve_project_path(str(queue_item["contract_path"]))
        except ValueError:
            continue
        display_contract_path = _display_path(contract_path)
        if display_contract_path in seen_contract_paths:
            continue
        payload_item = _universe_payload_item_from_contract(
            contract_path=contract_path,
            fallback_id=str(queue_item.get("contract_id") or contract_path.stem),
            fallback_issuer=str(queue_item.get("issuer_hint") or ""),
            fallback_underlying=str(queue_item.get("underlying_ticker") or ""),
            fallback_isin=str(queue_item.get("isin") or ""),
            fallback_status=str(queue_item.get("review_status") or queue_item.get("status") or "needs_review"),
            market_path=None,
            raw_price_path=None,
            source="prospectus_review_queue",
        )
        payload_item = _enrich_payload_item_with_canonical_store(payload_item, canonical_sync)
        items.append(payload_item)
        seen_contract_paths.add(display_contract_path)
    return {"universe_path": _display_path(universe_file), "items": items, "canonical_store": {"path": _display_path(_canonical_store_path()), "sync": canonical_sync}}


def _safe_sync_canonical_catalog() -> dict[str, Any]:
    try:
        return sync_canonical_catalog()
    except Exception as exc:
        return {"error": str(exc)}


def _pricing_readiness_payload(item: Mapping[str, Any]) -> dict[str, Any]:
    missing: list[str] = []
    if not item.get("has_contract"):
        missing.append("contract_terms")
    if not item.get("has_market_history"):
        missing.append("valuation_market_history")
    status = "ready" if not missing else ("missing_contract" if "contract_terms" in missing else "missing_market_history")
    return {
        "status": status,
        "available_for_pricing": status == "ready",
        "has_contract": bool(item.get("has_contract")),
        "has_market_history": bool(item.get("has_market_history")),
        "missing": missing,
        "contract_path": str(item.get("contract_path") or ""),
        "market_history_path": str(item.get("market_history_path") or ""),
        "raw_price_history_path": str(item.get("raw_price_history_path") or ""),
        "source_of_truth": str(item.get("canonical_source", {}).get("source_of_truth") if isinstance(item.get("canonical_source"), Mapping) else ""),
    }


def _date_range_status(summary: Mapping[str, Any], *, required: bool = True) -> dict[str, Any]:
    count = int(summary.get("count") or 0)
    if not required:
        status = "not_required"
    else:
        status = "ready" if count else "missing"
    return {"status": status, "row_count": count, "first_date": str(summary.get("first_date") or ""), "latest_date": str(summary.get("latest_date") or "")}


def _valuation_history_file_summary(path_value: str) -> dict[str, Any]:
    if not path_value:
        return {"status": "missing", "row_count": 0, "first_date": "", "latest_date": "", "path": ""}
    path = resolve_project_path(path_value)
    if not path.exists():
        return {"status": "missing", "row_count": 0, "first_date": "", "latest_date": "", "path": _display_path(path)}
    try:
        rows = load_market_history_csv(path)
    except Exception as exc:
        return {"status": "invalid", "row_count": 0, "first_date": "", "latest_date": "", "path": _display_path(path), "warning": str(exc)}
    dates = [row.as_of_date.isoformat() for row in rows]
    return {"status": "ready" if rows else "empty", "row_count": len(rows), "first_date": min(dates) if dates else "", "latest_date": max(dates) if dates else "", "path": _display_path(path)}


def _contract_data_readiness_payload(item: Mapping[str, Any]) -> dict[str, Any]:
    """PM-facing status: terms + CB/equity/FX histories + valuation-ready overlap."""

    contract_path = str(item.get("contract_path") or "")
    terms = {
        "status": "extracted" if item.get("has_contract") else "missing",
        "contract_status": str(item.get("status") or ""),
        "path": contract_path,
    }
    components: dict[str, Any] = {
        "terms": terms,
        "cb_price_history": {"status": "unknown", "row_count": 0, "first_date": "", "latest_date": ""},
        "equity_price_history": {"status": "unknown", "row_count": 0, "first_date": "", "latest_date": ""},
        "fx_history": {"status": "unknown", "row_count": 0, "first_date": "", "latest_date": "", "global_scope": True},
        "valuation_history": _valuation_history_file_summary(str(item.get("market_history_path") or "")),
    }
    requirements: dict[str, Any] = {}
    if contract_path and item.get("has_contract"):
        try:
            requirements = _contract_market_requirements(contract_path)
            store = _price_history_store()
            components["cb_price_history"] = _date_range_status(store.quote_date_range(instrument_id=requirements["cb_instrument_id"]))
            components["equity_price_history"] = _date_range_status(store.market_data_date_range(instrument_id=requirements["equity_instrument_id"], instrument_type="equity"))
            components["fx_history"] = _date_range_status(store.market_data_date_range(instrument_id=requirements["fx_instrument_id"], instrument_type="fx"), required=bool(requirements.get("requires_fx")))
            components["fx_history"]["global_scope"] = True
        except Exception as exc:
            components.setdefault("warnings", []).append(str(exc))
    latest_candidates = [
        comp.get("latest_date")
        for comp in components.values()
        if isinstance(comp, Mapping) and comp.get("status") in {"ready", "extracted"} and comp.get("latest_date")
    ]
    valuation_latest = str(components["valuation_history"].get("latest_date") or "")
    missing = [name for name, comp in components.items() if isinstance(comp, Mapping) and comp.get("status") in {"missing", "invalid", "empty"}]
    if components["fx_history"].get("status") == "not_required" and "fx_history" in missing:
        missing.remove("fx_history")
    status = "valuation_ready" if components["valuation_history"].get("status") == "ready" else ("needs_join" if all(components[name].get("status") in {"ready", "not_required"} for name in ("cb_price_history", "equity_price_history", "fx_history")) and terms["status"] == "extracted" else "needs_data")
    return {
        "status": status,
        "components": components,
        "missing": missing,
        "valuation_latest_date": valuation_latest,
        "raw_inputs_latest_common_hint": min(latest_candidates) if latest_candidates else "",
        "requirements": {k: v for k, v in requirements.items() if k != "contract"},
        "source_of_truth": "data/price_history/price_history.sqlite + data/cb_terminal.sqlite",
    }


def _enrich_payload_item_with_canonical_store(item: dict[str, Any], sync_result: Mapping[str, Any]) -> dict[str, Any]:
    identity = item.get("identity") if isinstance(item.get("identity"), Mapping) else {}
    contract_id = str(identity.get("contract_id") or item.get("id") or "")
    instrument_key_value = str(identity.get("instrument_key") or "")
    item["contract_id"] = contract_id
    item["instrument_key"] = instrument_key_value
    item["display_id"] = str(identity.get("display_id") or identity.get("display_name") or item.get("instrument_display_name") or item.get("label") or "")
    item["backing_store"] = "sqlite+json"
    item["canonical_record_id"] = ""
    item["readiness"] = _pricing_readiness_payload(item)
    item["data_readiness"] = _contract_data_readiness_payload(item)
    item["pricing_input_status"] = item["readiness"]["status"]
    try:
        store = canonical_store()
        contract = store.get_contract(contract_id)
    except Exception:
        store = None
        contract = None
    if contract:
        item["canonical_record_id"] = contract.get("id", "")
        item["contract_id"] = contract.get("contract_id") or contract_id
        item["instrument_key"] = contract.get("instrument_key") or instrument_key_value
        item["display_id"] = contract.get("display_id") or item["display_id"]
        coverage = None
        if store is not None:
            try:
                coverage = store.coverage_member_for_contract(str(contract.get("contract_id") or contract_id))
            except Exception:
                coverage = None
        if coverage:
            market_history_path = str(coverage.get("market_history_path") or "")
            raw_price_history_path = str(coverage.get("raw_price_history_path") or "")
            if market_history_path:
                item["market_history_path"] = market_history_path
                item["has_market_history"] = resolve_project_path(market_history_path).exists()
            if raw_price_history_path:
                item["raw_price_history_path"] = raw_price_history_path
            item["available_for_pricing"] = bool(item.get("has_contract") and item.get("has_market_history"))
            item["canonical_source"] = {
                "contract_path": item.get("contract_path", ""),
                "market_history_path": item.get("market_history_path", ""),
                "raw_price_history_path": item.get("raw_price_history_path", ""),
                "source_of_truth": "data/cb_terminal.sqlite:coverage_universe",
                "canonical_store": _display_path(_canonical_store_path()),
                "canonical_record_id": coverage.get("id", ""),
                "canonical_contract_record_id": contract.get("id", ""),
            }
            item["readiness"] = _pricing_readiness_payload(item)
            item["data_readiness"] = _contract_data_readiness_payload(item)
            item["pricing_input_status"] = item["readiness"]["status"]
        else:
            item["canonical_source"] = {
                "contract_path": item.get("contract_path", ""),
                "market_history_path": item.get("market_history_path", ""),
                "raw_price_history_path": item.get("raw_price_history_path", ""),
                "source_of_truth": "data/cb_terminal.sqlite:contracts",
                "canonical_store": _display_path(_canonical_store_path()),
                "canonical_record_id": contract.get("id", ""),
                "canonical_contract_record_id": contract.get("id", ""),
            }
            item["readiness"] = _pricing_readiness_payload(item)
            item["data_readiness"] = _contract_data_readiness_payload(item)
            item["pricing_input_status"] = item["readiness"]["status"]
    if sync_result.get("error"):
        item.setdefault("warnings", []).append(f"canonical store sync failed: {sync_result['error']}")
    return item


def _universe_payload_item_from_contract(
    *,
    contract_path: Path,
    fallback_id: str,
    fallback_issuer: str,
    fallback_underlying: str,
    fallback_isin: str,
    fallback_status: str,
    market_path: Path | None,
    raw_price_path: Path | None,
    source: str,
) -> dict[str, Any]:
    has_contract = contract_path.exists()
    has_market_history = bool(market_path and market_path.exists())
    display_parts: dict[str, str] = {}
    contract_status = fallback_status
    issuer = fallback_issuer
    underlying_ticker = fallback_underlying
    isin = fallback_isin
    identity_payload = None
    risk_free_curve_currency = ""
    if has_contract:
        try:
            contract_raw = json.loads(contract_path.read_text(encoding="utf-8"))
            if isinstance(contract_raw, Mapping):
                display_parts = _instrument_display_parts(contract_raw, fallback_id)
                contract_status = str(contract_raw.get("status") or fallback_status)
                issuer_mapping = contract_raw.get("issuer") if isinstance(contract_raw.get("issuer"), Mapping) else {}
                conversion_mapping = contract_raw.get("conversion") if isinstance(contract_raw.get("conversion"), Mapping) else {}
                instrument_mapping = contract_raw.get("instrument") if isinstance(contract_raw.get("instrument"), Mapping) else {}
                issuer = str(issuer_mapping.get("name") or fallback_issuer or "")
                underlying_ticker = str(conversion_mapping.get("underlying_ticker") or issuer_mapping.get("ticker") or fallback_underlying or "")
                isin = str(contract_raw.get("isin") or instrument_mapping.get("canonical_id") or fallback_isin or "")
                identity_payload = cb_identity_from_contract(contract_raw, fallback_id=fallback_id).to_payload()
                risk_free_curve_currency = curve_currency_from_contract_dict(contract_raw)
                risk_free_curve_currency = curve_currency_for_contract(load_contract_json(contract_path))
        except Exception:
            display_parts = {}
    label = display_parts.get("instrument_display_name") or f"{issuer} — {underlying_ticker}".strip(" —") or fallback_id
    display_contract = _display_path(contract_path)
    display_market = _display_path(market_path) if market_path else ""
    display_raw_price = _display_path(raw_price_path) if raw_price_path else ""
    canonical_source = {
        "contract_path": display_contract,
        "market_history_path": display_market,
        "raw_price_history_path": display_raw_price,
        "source_of_truth": "data/coverage/universe.json",
    }
    if identity_payload is None:
        primary_scheme = "ISIN" if isin and isin != "PENDING_ISIN" else "REGISTRY_ID"
        primary_id = isin or fallback_id
        identity_payload = {
            "instrument_key": instrument_key("convertible_bond", primary_scheme, primary_id),
            "instrument_type": "convertible_bond",
            "primary_id_scheme": primary_scheme,
            "primary_id": primary_id,
            "display_name": label,
            "contract_id": fallback_id,
        }
    return {
        "id": fallback_id,
        "label": label,
        "instrument_display_name": display_parts.get("instrument_display_name", label),
        "instrument_short_name": display_parts.get("instrument_short_name", label),
        "instrument_legal_name": display_parts.get("instrument_legal_name", issuer),
        "instrument_raw_display_name": display_parts.get("instrument_raw_display_name", ""),
        "issuer": issuer,
        "underlying_ticker": underlying_ticker,
        "isin": isin,
        "status": contract_status,
        "contract_path": display_contract,
        "market_history_path": display_market,
        "raw_price_history_path": display_raw_price,
        "identity": identity_payload,
        "canonical_source": canonical_source,
        "has_contract": has_contract,
        "has_market_history": has_market_history,
        "available_for_pricing": has_contract and has_market_history,
        "selectable_for_review": has_contract,
        "risk_free_curve_currency": risk_free_curve_currency,
        "source": source,
        "readiness": _pricing_readiness_payload({"has_contract": has_contract, "has_market_history": has_market_history, "contract_path": display_contract, "market_history_path": display_market, "raw_price_history_path": display_raw_price}),
        "data_readiness": _contract_data_readiness_payload({"has_contract": has_contract, "has_market_history": has_market_history, "contract_path": display_contract, "market_history_path": display_market, "raw_price_history_path": display_raw_price, "status": contract_status}),
    }


def build_sources_payload(*, include_hashes: bool = False) -> dict[str, Any]:
    """Return a project-relative inventory of uploaded raw files and canonical data sources."""

    _reconcile_raw_prospectus_indexes()
    sources: dict[str, dict[str, Any]] = {}
    contracts = _load_contract_source_summaries()
    review_items = _load_json_list(resolve_project_path(f"{COVERAGE_DIR}/review_queue.json"), default=[])
    prospectus_items = _load_json_list(resolve_project_path(f"{COVERAGE_DIR}/prospectus_inventory.json"), default=[])
    universe_items = _load_json_list(resolve_project_path(DEFAULT_UNIVERSE), default=[])
    contract_links = _contract_links_by_path(contracts)
    universe_links = _universe_links_by_path(universe_items, contracts=contracts)
    review_links = _review_links_by_path(review_items)
    prospectus_links = _prospectus_inventory_links_by_path(prospectus_items)

    for kind, config in SOURCE_INVENTORY_KINDS.items():
        root = resolve_project_path(config["directory"])
        if not root.exists():
            continue
        for path in sorted(root.iterdir(), key=lambda item: item.name.lower()):
            if not path.is_file() or path.suffix.lower() not in config["extensions"]:
                continue
            rel = _display_path(path)
            sources[rel] = _source_record_from_file(
                kind,
                path,
                include_hashes=include_hashes,
                contracts=contract_links.get(rel, []),
                review_items=review_links.get(rel, []),
                prospectus_items=prospectus_links.get(rel, []),
                universe_items=universe_links.get(rel, []),
                contract_summary=contracts.get(rel),
            )

    # Surface index rows whose referenced file is missing instead of silently hiding stale state.
    for rel, linked_contracts in contract_links.items():
        if rel not in sources:
            sources[rel] = _missing_source_record("raw_prospectus", rel, contracts=linked_contracts)
    for rel, linked_universe in universe_links.items():
        if rel not in sources:
            kind = "generated_market_history" if rel.startswith(f"{GENERATED_MARKET_HISTORY_DIR}/") else "raw_price_history"
            sources[rel] = _missing_source_record(kind, rel, universe_items=linked_universe)

    canonical_sync = _safe_sync_canonical_catalog()
    return {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "sources": sorted((_enrich_source_with_canonical_store(item) for item in sources.values()), key=lambda item: (item["role"] != "raw_input", item["kind"], item["filename"])),
        "summary": _source_inventory_summary(list(sources.values())),
        "indexes": {
            "review_queue_path": f"{COVERAGE_DIR}/review_queue.json",
            "prospectus_inventory_path": f"{COVERAGE_DIR}/prospectus_inventory.json",
            "universe_path": DEFAULT_UNIVERSE,
            "contracts_dir": CONTRACTS_DIR,
            "canonical_store_path": _display_path(_canonical_store_path()),
        },
        "canonical_store": {"path": _display_path(_canonical_store_path()), "sync": canonical_sync},
    }


def _enrich_source_with_canonical_store(item: dict[str, Any]) -> dict[str, Any]:
    item = dict(item)
    item.setdefault("canonical_source_id", "")
    item.setdefault("source_scope", "contract" if item.get("contracts") or item.get("universe_items") else "global")
    path = item.get("path")
    if path and item.get("exists"):
        try:
            source = canonical_store().register_source_file(resolve_project_path(str(path)), artifact_kind=str(item.get("kind") or "source"), canonical_path=str(path))
            item["canonical_source_id"] = source["id"]
            item["canonical_source"] = {
                "source_of_truth": "data/cb_terminal.sqlite:source_files",
                "canonical_store": _display_path(_canonical_store_path()),
                "canonical_source_id": source["id"],
                "canonical_record_id": source["id"],
                "artifact_kind": source.get("artifact_kind", item.get("kind", "")),
                "path": str(item.get("path") or ""),
                "canonical_path": source.get("canonical_path", ""),
                "sha256": source.get("sha256", ""),
                "status": source.get("status", ""),
            }
            item["content_hash_status"] = "known"
            if not item.get("sha256"):
                item["sha256"] = source["sha256"]
        except Exception as exc:
            item.setdefault("warnings", []).append(f"canonical source registration failed: {exc}")
            item["content_hash_status"] = "error"
    else:
        item["content_hash_status"] = "missing"
    if item.get("kind") == "generated_market_history":
        item["can_link"] = True
        item["link_targets"] = ["market_history_path"]
    else:
        item["can_link"] = False
        item["link_targets"] = []
    return item


def source_action_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Rename/remove safe source files, or route contract edits through the term editor."""

    action = str(payload.get("action") or "").strip()
    if action not in {"rename", "remove", "edit", "link"}:
        raise ValueError("source action must be rename, remove, edit, or link")
    kind = str(payload.get("kind") or "").strip()
    source_path = str(payload.get("source_path") or "").strip()
    if action == "edit" and kind == "contract":
        contract_path = source_path or str(payload.get("contract_path") or "")
        edits = payload.get("edits")
        if edits:
            next_payload = dict(payload)
            next_payload["contract_path"] = contract_path
            return {"action": action, "kind": kind, "result": edit_contract_terms_payload(next_payload)}
        return {"action": action, "kind": kind, "contract_path": _display_path(_resolve_contract_json_path(contract_path)), "open_contract_review": True}
    if action == "link":
        if not _mapping_bool(payload, "confirm", False):
            raise ValueError("source link requires confirm=true")
        if kind != "generated_market_history":
            raise ValueError("source link supports generated valuation histories only; raw CB, stock, and FX prices must be uploaded first, then joined with Build valuation history")
        source = _resolve_source_file_path(kind, source_path)
        if not source.exists():
            raise ValueError(f"source file not found: {_display_path(source)}")
        return _link_source_to_universe(kind, source, payload)
    if kind not in SOURCE_INVENTORY_KINDS or kind == "contract":
        raise ValueError("file source action supports raw_prospectus, raw_price_history, or generated_market_history")
    if not _mapping_bool(payload, "confirm", False):
        raise ValueError("source action requires confirm=true")
    source = _resolve_source_file_path(kind, source_path)
    if not source.exists():
        if action == "remove" and kind == "raw_prospectus":
            typed = str(payload.get("typed_confirmation") or "").strip()
            if typed != source.name:
                raise ValueError("remove requires typed_confirmation matching the source filename")
            return _remove_missing_raw_prospectus_source(source)
        raise ValueError(f"source file not found: {_display_path(source)}")
    expected_sha = str(payload.get("expected_sha256") or "").strip()
    digest = ""
    if expected_sha:
        digest = sha256_file(source)
        if digest != expected_sha:
            raise ValueError("expected_sha256 does not match current file")
    if action == "rename":
        return _rename_source_file(kind, source, str(payload.get("new_filename") or ""), digest=digest)
    typed = str(payload.get("typed_confirmation") or "").strip()
    if typed != source.name:
        raise ValueError("remove requires typed_confirmation matching the source filename")
    return _remove_source_file(kind, source, digest=digest or sha256_file(source))


def _resolve_source_file_path(kind: str, value: str | Path) -> Path:
    if kind == "raw_prospectus":
        return _resolve_raw_prospectus_path(value)
    config = SOURCE_INVENTORY_KINDS[kind]
    path = resolve_project_path(value)
    root = resolve_project_path(config["directory"])
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"source_path must be under {config['directory']}") from exc
    if path.suffix.lower() not in config["extensions"]:
        raise ValueError(f"source_path extension is not valid for {kind}")
    return path


def _rename_source_file(kind: str, source: Path, new_filename: str, *, digest: str = "") -> dict[str, Any]:
    if kind == "raw_prospectus":
        return _raw_prospectus_lifecycle().rename_pending(source, new_filename, digest=digest)
    filename = _safe_upload_filename(new_filename)
    if Path(filename).suffix.lower() != source.suffix.lower():
        raise ValueError("new_filename must preserve the file extension")
    destination = (source.parent / filename).resolve()
    destination.relative_to(source.parent.resolve())
    if destination.exists():
        raise ValueError(f"destination already exists: {filename}")
    source.rename(destination)
    database_sync: dict[str, int] = {}
    if kind == "raw_price_history":
        try:
            database_sync = _price_history_store().rename_source_data(source, destination)
        except Exception:
            # Keep the file and its imported provenance together if the DB
            # update fails; the source action can then be retried safely.
            destination.rename(source)
            raise
    updated_indexes: list[str] = []
    if kind == "raw_prospectus":
        if _rewrite_review_queue_source(source, new_path=destination):
            updated_indexes.append(f"{COVERAGE_DIR}/review_queue.json")
        if _rewrite_prospectus_inventory_source(source, new_path=destination):
            updated_indexes.append(f"{COVERAGE_DIR}/prospectus_inventory.json")
    elif kind in {"raw_price_history", "generated_market_history"}:
        fields = ["raw_price_history_path"] if kind == "raw_price_history" else ["market_history_path"]
        if _rewrite_universe_source_path(source, destination, fields=fields):
            updated_indexes.append(DEFAULT_UNIVERSE)
    return {
        "action": "rename",
        "kind": kind,
        "old_path": _display_path(source),
        "new_path": _display_path(destination),
        "filename": destination.name,
        "source_sha256": digest,
        "database_sync": database_sync,
        "updated_indexes": updated_indexes,
    }


def _raw_prospectus_lifecycle() -> RawProspectusLifecycle:
    return RawProspectusLifecycle(
        PROJECT_ROOT,
        raw_dir=RAW_PROSPECTUS_DIR,
        is_linked_to_contract=_raw_prospectus_is_linked_to_contract,
        safe_filename=_safe_upload_filename,
    )


def _remove_missing_raw_prospectus_source(source: Path) -> dict[str, Any]:
    return _raw_prospectus_lifecycle().delete_pending(source, typed_confirmation=source.name, allow_missing=True)


def _remove_source_file(kind: str, source: Path, *, digest: str) -> dict[str, Any]:
    if kind == "raw_prospectus":
        return _raw_prospectus_lifecycle().delete_pending(source, typed_confirmation=source.name, digest=digest)
    universe_refs = _path_references_in_universe(source)
    if universe_refs:
        raise ValueError("source file is referenced by the coverage universe; unlink or rename it before removal")
    source.unlink()
    database_sync = _price_history_store().remove_source_data(source) if kind == "raw_price_history" else {}
    return {"action": "remove", "kind": kind, "source_path": _display_path(source), "source_sha256": digest, "raw_deleted": True, "database_sync": database_sync, "updated_indexes": []}


def _link_source_to_universe(kind: str, source: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Serialize coverage-universe link updates across threaded requests."""

    with _UNIVERSE_UPDATE_LOCK:
        return _link_source_to_universe_locked(kind, source, payload)


def _link_source_to_universe_locked(kind: str, source: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Persist a selected raw/canonical market source on a specific CB row.

    Links are contract-scoped, never issuer-scoped, because a single issuer may
    have multiple CBs. The coverage universe remains the source of truth for
    market-history paths used by the GUI and API.  A contract can be visible in
    the PM dropdown before it has a coverage-universe row, so a manual selected
    contract may promote a minimal CB-scoped row before writing the link.
    """

    field = "raw_price_history_path" if kind == "raw_price_history" else "market_history_path"
    contract_path_value = str(payload.get("contract_path") or "").strip()
    universe_id = str(payload.get("universe_id") or payload.get("id") or "").strip()
    contract_display = ""
    if contract_path_value:
        contract_display = _display_path(_resolve_contract_json_path(contract_path_value))
    universe_path = resolve_project_path(DEFAULT_UNIVERSE)
    items = _load_json_list(universe_path, default=[])
    source_instrument_ids = _source_price_history_instrument_ids(source) if kind == "raw_price_history" else set()
    if not contract_display and not universe_id:
        if kind != "raw_price_history":
            raise ValueError("source link requires contract_path or universe_id")
        if len(source_instrument_ids) > 1:
            return _auto_link_raw_price_history_source(source, source_instrument_ids, items, payload)
        contract_display = _auto_contract_path_for_raw_price_history_source(source_instrument_ids, items)
    matches: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_contract = _safe_display_optional_path(item.get("contract_path")) if item.get("contract_path") else ""
        if contract_display and item_contract == contract_display:
            matches.append(item)
            continue
        if universe_id and str(item.get("id") or "") == universe_id:
            matches.append(item)
    if not matches and contract_display:
        matches.append(_append_universe_row_for_contract(items, contract_display))
    if not matches:
        raise ValueError("no coverage-universe CB row matches the selected contract")
    if len(matches) > 1:
        raise ValueError("source link matched multiple coverage-universe rows; select a specific CB")
    row = matches[0]
    if source_instrument_ids:
        row_ids = _universe_row_instrument_ids(row)
        if row_ids and source_instrument_ids.isdisjoint(row_ids):
            source_ids = ", ".join(sorted(source_instrument_ids))
            selected_ids = ", ".join(sorted(row_ids))
            raise ValueError(f"raw price-history ISIN/instrument id {source_ids} does not match selected CB {selected_ids}")
    if kind == "generated_market_history":
        contract_for_validation = load_contract_json(_resolve_contract_json_path(str(row.get("contract_path") or contract_display)))
        validate_market_history_file_for_contract(source, contract_for_validation).raise_for_errors()
    next_path = _display_path(source)
    current_path = str(row.get(field) or "").strip()
    if current_path and current_path != next_path and not _mapping_bool(payload, "confirm_overwrite", False):
        raise ValueError(f"selected CB already has {field}; pass confirm_overwrite=true to replace it")
    row[field] = next_path
    _write_json_atomic(universe_path, items)
    result = {
        "action": "link",
        "kind": kind,
        "source_path": next_path,
        "contract_path": _safe_display_optional_path(row.get("contract_path")),
        "universe_id": str(row.get("id") or ""),
        "linked_field": field,
        "source_instrument_ids": sorted(source_instrument_ids),
        "updated_indexes": [DEFAULT_UNIVERSE],
    }
    return _sync_source_link_to_canonical_store(result)


def _source_price_history_instrument_ids(source: Path) -> set[str]:
    try:
        rows = load_price_history_file(source)
    except Exception:
        return set()
    return {_normalize_identifier(row.instrument_id) for row in rows if _normalize_identifier(row.instrument_id)}


def _normalize_identifier(value: Any) -> str:
    text = str(value or "").strip().upper()
    return re.sub(r"\s+", "", text)


def _auto_link_raw_price_history_source(source: Path, source_instrument_ids: set[str], universe_items: list[Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    """Link a multi-ISIN raw quote file to every matching CB row by contract ISIN."""

    id_to_contracts: dict[str, set[str]] = {instrument_id: set() for instrument_id in source_instrument_ids}
    for item in universe_items:
        if not isinstance(item, Mapping):
            continue
        contract_path = _safe_display_optional_path(item.get("contract_path")) if item.get("contract_path") else ""
        if not contract_path:
            continue
        for row_id in _universe_row_instrument_ids(item):
            if row_id in id_to_contracts:
                id_to_contracts[row_id].add(contract_path)
    for contract_path, raw in _iter_contract_json_files():
        display = _display_path(contract_path)
        for contract_id in _contract_instrument_ids(raw):
            if contract_id in id_to_contracts:
                id_to_contracts[contract_id].add(display)
    missing = sorted(instrument_id for instrument_id, contracts in id_to_contracts.items() if not contracts)
    ambiguous = {instrument_id: sorted(contracts) for instrument_id, contracts in id_to_contracts.items() if len(contracts) > 1}
    if missing:
        raise ValueError("raw price-history ISIN does not match known CB contracts: " + ", ".join(missing))
    if ambiguous:
        raise ValueError("raw price-history ISIN matches multiple CB contracts: " + "; ".join(f"{instrument_id} -> {', '.join(paths)}" for instrument_id, paths in sorted(ambiguous.items())))
    source_rel = _display_path(source)
    linked: list[dict[str, str]] = []
    for instrument_id, contracts in sorted(id_to_contracts.items()):
        contract_display = next(iter(contracts))
        row = next(
            (item for item in universe_items if isinstance(item, dict) and _safe_display_optional_path(item.get("contract_path")) == contract_display),
            None,
        )
        if row is None:
            row = _append_universe_row_for_contract(universe_items, contract_display)
        current_path = str(row.get("raw_price_history_path") or "").strip()
        if current_path and current_path != source_rel and not _mapping_bool(payload, "confirm_overwrite", False):
            raise ValueError(f"{contract_display} already has raw_price_history_path; pass confirm_overwrite=true to replace it")
        row["raw_price_history_path"] = source_rel
        linked.append({"instrument_id": instrument_id, "contract_path": _safe_display_optional_path(row.get("contract_path")), "universe_id": str(row.get("id") or "")})
    _write_json_atomic(resolve_project_path(DEFAULT_UNIVERSE), universe_items)
    result = {
        "action": "link",
        "kind": "raw_price_history",
        "source_path": source_rel,
        "linked_field": "raw_price_history_path",
        "linked_count": len(linked),
        "links": linked,
        "source_instrument_ids": sorted(source_instrument_ids),
        "updated_indexes": [DEFAULT_UNIVERSE],
    }
    for link in result["links"]:
        _sync_source_link_to_canonical_store({**result, "contract_path": link.get("contract_path", ""), "universe_id": link.get("universe_id", "")})
    return result


def _sync_source_link_to_canonical_store(result: dict[str, Any]) -> dict[str, Any]:
    try:
        source_path = result.get("source_path")
        contract_path = result.get("contract_path")
        if not source_path or not contract_path:
            return result
        source_abs = resolve_project_path(str(source_path))
        contract_abs = _resolve_contract_json_path(str(contract_path))
        if not source_abs.exists() or not contract_abs.exists():
            return result
        raw = json.loads(contract_abs.read_text(encoding="utf-8"))
        store = canonical_store()
        contract = store.upsert_contract(raw, source_path=_display_path(contract_abs))
        source = store.register_source_file(source_abs, artifact_kind=str(result.get("kind") or "source"), canonical_path=_display_path(source_abs))
        field = str(result.get("linked_field") or "")
        store.upsert_coverage_member(
            universe_name="default",
            contract_id=contract["contract_id"],
            instrument_key=contract["instrument_key"],
            status=str(raw.get("status") or "active"),
            market_history_path=str(source_path) if field == "market_history_path" else "",
            raw_price_history_path=str(source_path) if field == "raw_price_history_path" else "",
            metadata={"source_link": result},
        )
        result["canonical_source_id"] = source["id"]
        result["canonical_record_id"] = contract["id"]
        result["persisted_to_store"] = True
    except Exception as exc:
        result.setdefault("warnings", []).append(f"canonical store sync failed: {exc}")
        result["persisted_to_store"] = False
    return result


def _auto_contract_path_for_raw_price_history_source(source_instrument_ids: set[str], universe_items: list[Any]) -> str:
    if len(source_instrument_ids) != 1:
        raise ValueError("source link requires contract_path or universe_id; raw file has no unique ISIN/instrument id to auto-link")
    candidates: dict[str, str] = {}
    for item in universe_items:
        if not isinstance(item, Mapping):
            continue
        row_ids = _universe_row_instrument_ids(item)
        contract_path = _safe_display_optional_path(item.get("contract_path")) if item.get("contract_path") else ""
        if contract_path and row_ids and not source_instrument_ids.isdisjoint(row_ids):
            candidates[contract_path] = contract_path
    for contract_path, raw in _iter_contract_json_files():
        contract_ids = _contract_instrument_ids(raw)
        if contract_ids and not source_instrument_ids.isdisjoint(contract_ids):
            candidates[_display_path(contract_path)] = _display_path(contract_path)
    if len(candidates) == 1:
        return next(iter(candidates.values()))
    if len(candidates) > 1:
        raise ValueError("raw price-history ISIN matches multiple CB contracts; select a specific CB")
    raise ValueError("raw price-history ISIN does not match any known CB contract; select or create the CB contract before linking")


def _append_universe_row_for_contract(items: list[Any], contract_display: str) -> dict[str, Any]:
    contract_path = _resolve_contract_json_path(contract_display)
    if not contract_path.exists():
        raise ValueError(f"selected contract file not found: {contract_display}")
    try:
        raw = json.loads(contract_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"selected contract JSON cannot be read: {contract_display}") from exc
    if not isinstance(raw, Mapping):
        raise ValueError(f"selected contract JSON is not an object: {contract_display}")
    row = _universe_row_from_contract(contract_path, raw)
    existing_ids = {str(item.get("id") or "") for item in items if isinstance(item, Mapping)}
    base_id = row["id"]
    if base_id in existing_ids:
        suffix = 2
        while f"{base_id}_{suffix}" in existing_ids:
            suffix += 1
        row["id"] = f"{base_id}_{suffix}"
    items.append(row)
    return row


def _universe_row_from_contract(contract_path: Path, raw: Mapping[str, Any]) -> dict[str, Any]:
    fallback_id = str(raw.get("id") or contract_path.stem).strip() or contract_path.stem
    issuer = raw.get("issuer") if isinstance(raw.get("issuer"), Mapping) else {}
    instrument = raw.get("instrument") if isinstance(raw.get("instrument"), Mapping) else {}
    conversion = raw.get("conversion") if isinstance(raw.get("conversion"), Mapping) else {}
    display_parts = _instrument_display_parts(raw, fallback_id)
    canonical_id = str(raw.get("isin") or instrument.get("canonical_id") or "").strip()
    identity = cb_identity_from_contract(raw, fallback_id=fallback_id)
    issuer_name = str(instrument.get("issuer_legal_name") or issuer.get("name") or display_parts.get("instrument_legal_name") or _issuer_short_for_display(raw)).strip()
    underlying_ticker = str(conversion.get("underlying_ticker") or issuer.get("ticker") or instrument.get("underlying_ticker") or "").strip()
    if not underlying_ticker:
        raise ValueError("selected contract is missing underlying_ticker; add it before linking market data")
    return {
        "id": fallback_id,
        "canonical_id_type": str(instrument.get("canonical_id_type") or ("ISIN" if canonical_id else "") or "").strip(),
        "canonical_id": canonical_id,
        "isin": canonical_id if canonical_id and canonical_id != "PENDING_ISIN" else str(raw.get("isin") or "").strip(),
        "display_name": display_parts["instrument_display_name"],
        "issuer": issuer_name,
        "issuer_short_name": _issuer_short_for_display(raw),
        "underlying_ticker": underlying_ticker,
        "cb_instrument_id": canonical_id,
        "identity": identity.to_payload(),
        "contract_path": _display_path(contract_path),
        "market_history_path": "",
        "raw_price_history_path": "",
        "status": str(raw.get("status") or "needs_market_history"),
    }


def _universe_row_instrument_ids(row: Mapping[str, Any]) -> set[str]:
    ids = {
        _normalize_identifier(row.get("isin")),
        _normalize_identifier(row.get("canonical_id")),
        _normalize_identifier(row.get("cb_instrument_id")),
    }
    contract_path = _safe_display_optional_path(row.get("contract_path")) if row.get("contract_path") else ""
    if contract_path:
        try:
            contract_raw = json.loads(_resolve_contract_json_path(contract_path).read_text(encoding="utf-8"))
        except Exception:
            contract_raw = None
        if isinstance(contract_raw, Mapping):
            ids.update(_contract_instrument_ids(contract_raw))
    ids.discard("")
    ids.discard("PENDING_ISIN")
    return ids


def _contract_instrument_ids(raw: Mapping[str, Any]) -> set[str]:
    instrument = raw.get("instrument") if isinstance(raw.get("instrument"), Mapping) else {}
    ids = {
        _normalize_identifier(raw.get("isin")),
        _normalize_identifier(instrument.get("canonical_id")),
        _normalize_identifier(instrument.get("instrument_id")),
    }
    aliases = instrument.get("aliases")
    if isinstance(aliases, list):
        ids.update(_normalize_identifier(alias) for alias in aliases)
    ids.discard("")
    ids.discard("PENDING_ISIN")
    return ids


def _iter_contract_json_files() -> list[tuple[Path, Mapping[str, Any]]]:
    root = resolve_project_path(CONTRACTS_DIR)
    results: list[tuple[Path, Mapping[str, Any]]] = []
    if not root.exists():
        return results
    for path in sorted(root.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(raw, Mapping):
            results.append((path, raw))
    return results


def _source_record_from_file(
    kind: str,
    path: Path,
    *,
    include_hashes: bool,
    contracts: list[dict[str, Any]],
    review_items: list[dict[str, Any]],
    prospectus_items: list[dict[str, Any]],
    universe_items: list[dict[str, Any]],
    contract_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    rel = _display_path(path)
    stat = path.stat()
    linked = bool(contracts or review_items or universe_items)
    status = _source_status(kind, linked=linked, contract_summary=contract_summary, review_items=review_items, universe_items=universe_items)
    market_source_matches: dict[str, Any] = {}
    source_scope = "contract" if linked else "global"
    if kind == "raw_price_history":
        market_source_matches = _market_source_matches_for_inventory_file(path)
        if market_source_matches.get("status") != "no_imports":
            status = _source_status_from_market_matches(market_source_matches)
            source_scope = _source_scope_from_market_matches(market_source_matches)
    reason = _source_action_block_reason(kind, linked=linked, universe_items=universe_items)
    metadata = contract_summary or {}
    record = {
        "source_id": _source_id(kind, rel),
        "kind": kind,
        "type_label": SOURCE_INVENTORY_KINDS[kind]["label"],
        "role": SOURCE_INVENTORY_KINDS[kind]["role"],
        "path": rel,
        "filename": path.name,
        "directory": _display_path(path.parent),
        "extension": path.suffix.lower(),
        "exists": True,
        "size_bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).replace(microsecond=0).isoformat(),
        "sha256": sha256_file(path) if include_hashes else _known_sha_for_source(rel, contracts, review_items, prospectus_items),
        "status": status,
        "linked_reference_count": len(contracts) + len(review_items) + len(prospectus_items) + len(universe_items),
        "contracts": contracts,
        "review_queue_items": review_items,
        "prospectus_inventory_items": prospectus_items,
        "universe_items": universe_items,
        "metadata": metadata,
        "source_scope": source_scope,
        "editable": {
            "rename": reason == "",
            "remove": reason == "" or (kind == "raw_prospectus" and not linked),
            "edit": kind == "contract",
            "reason": reason,
        },
        "warnings": [],
    }
    if market_source_matches:
        record["market_source_matches"] = market_source_matches
    return record


def _missing_source_record(kind: str, rel: str, *, contracts: list[dict[str, Any]] | None = None, universe_items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    contracts = contracts or []
    universe_items = universe_items or []
    return {
        "source_id": _source_id(kind, rel),
        "kind": kind,
        "type_label": SOURCE_INVENTORY_KINDS.get(kind, {}).get("label", kind),
        "role": SOURCE_INVENTORY_KINDS.get(kind, {}).get("role", "unknown"),
        "path": rel,
        "filename": Path(rel).name,
        "directory": str(Path(rel).parent),
        "extension": Path(rel).suffix.lower(),
        "exists": False,
        "size_bytes": None,
        "modified_at": "",
        "sha256": "",
        "status": "missing_source",
        "linked_reference_count": len(contracts) + len(universe_items),
        "contracts": contracts,
        "review_queue_items": [],
        "prospectus_inventory_items": [],
        "universe_items": universe_items,
        "metadata": {},
        "editable": {"rename": False, "remove": False, "edit": False, "reason": "source file is missing"},
        "warnings": ["Referenced by an index or contract, but the file is missing."],
    }


def _source_id(kind: str, rel: str) -> str:
    return f"src:{kind}:path:{hashlib.sha256(rel.encode('utf-8')).hexdigest()[:16]}"


def _source_status(kind: str, *, linked: bool, contract_summary: dict[str, Any] | None, review_items: list[dict[str, Any]], universe_items: list[dict[str, Any]]) -> str:
    if kind == "contract":
        return str((contract_summary or {}).get("status") or "contract_json")
    if universe_items:
        return "valuation_ready" if kind == "generated_market_history" else "in_universe"
    if review_items:
        return str(review_items[0].get("status") or review_items[0].get("review_status") or "in_review_queue")
    if linked:
        return "linked"
    return "uploaded_unlinked"


def _market_source_matches_for_inventory_file(path: Path) -> dict[str, Any]:
    """Summarize DB-imported instruments carried by a raw market-data source file."""

    db_path = resolve_project_path(DEFAULT_PRICE_HISTORY_DB)
    if not db_path.exists():
        return {"status": "no_imports", "cb_quotes": [], "equities": [], "fx": {"global_scope": True, "instrument_ids": []}}
    candidates = {_display_path(path), str(path), str(path.resolve())}
    placeholders = ",".join("?" for _ in candidates)
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            quote_rows = conn.execute(
                f"""
                SELECT instrument_id, primary_id, contract_id, COUNT(*) AS row_count
                FROM cb_price_quotes
                WHERE source_file IN ({placeholders})
                GROUP BY instrument_id, primary_id, contract_id
                ORDER BY instrument_id, contract_id
                """,
                tuple(candidates),
            ).fetchall()
            market_rows = conn.execute(
                f"""
                SELECT instrument_id, instrument_type, COUNT(*) AS row_count
                FROM market_data_points
                WHERE source_file IN ({placeholders})
                GROUP BY instrument_id, instrument_type
                ORDER BY instrument_type, instrument_id
                """,
                tuple(candidates),
            ).fetchall()
    except sqlite3.Error as exc:
        return {"status": "match_failed", "warning": str(exc), "cb_quotes": [], "equities": [], "fx": {"global_scope": True, "instrument_ids": []}}
    if not quote_rows and not market_rows:
        return {"status": "no_imports", "cb_quotes": [], "equities": [], "fx": {"global_scope": True, "instrument_ids": []}}

    cb_matches: list[dict[str, Any]] = []
    for row in quote_rows:
        instrument_id = _normalize_identifier(row["instrument_id"] or row["primary_id"])
        matched = _matching_contract_paths_for_cb_id(instrument_id)
        cb_matches.append({
            "instrument_id": instrument_id,
            "contract_id": str(row["contract_id"] or ""),
            "row_count": int(row["row_count"] or 0),
            "matched_contract_paths": matched,
            "availability": "available" if matched else "not_in_database",
        })
    equities = [
        {"instrument_id": str(row["instrument_id"] or ""), "row_count": int(row["row_count"] or 0), "matching_rule": "underlying ticker / Bloomberg equity id"}
        for row in market_rows
        if str(row["instrument_type"] or "") == "equity"
    ]
    fx_ids = [str(row["instrument_id"] or "") for row in market_rows if str(row["instrument_type"] or "") == "fx"]
    fx_pairs = sorted({pair for pair in (_fx_pair_key(value) for value in fx_ids) if pair})
    return {
        "status": "matched" if (cb_matches or equities or fx_ids) else "no_identifiers",
        "cb_quotes": cb_matches,
        "equities": equities,
        "fx": {"global_scope": True, "instrument_ids": sorted(fx_ids), "pairs": fx_pairs, "note": "FX histories are currency-pair sources, not CB-linked sources."},
    }


def _matching_contract_paths_for_cb_id(instrument_id: str) -> list[str]:
    matches: set[str] = set()
    for item in _load_json_list(resolve_project_path(DEFAULT_UNIVERSE), default=[]):
        if isinstance(item, Mapping) and instrument_id in _universe_row_instrument_ids(item):
            contract_path = _safe_display_optional_path(item.get("contract_path"))
            if contract_path:
                matches.add(contract_path)
    for contract_path, raw in _iter_contract_json_files():
        if instrument_id in _contract_instrument_ids(raw):
            matches.add(_display_path(contract_path))
    return sorted(matches)


def _source_status_from_market_matches(matches: Mapping[str, Any]) -> str:
    cb_matches = [row for row in matches.get("cb_quotes", []) if isinstance(row, Mapping)]
    has_equity = bool(matches.get("equities"))
    has_fx = bool((matches.get("fx") if isinstance(matches.get("fx"), Mapping) else {}).get("instrument_ids"))
    if cb_matches:
        available = sum(1 for row in cb_matches if row.get("matched_contract_paths"))
        unavailable = len(cb_matches) - available
        if available and unavailable:
            return "partially_matched_market_source"
        if available:
            return "matched_multiple_cbs" if len(cb_matches) > 1 else "matched_market_source"
        return "unmatched_market_source"
    if has_fx and not has_equity:
        return "global_fx_source"
    if has_equity or has_fx:
        return "market_data_source"
    return "uploaded_unlinked"


def _source_scope_from_market_matches(matches: Mapping[str, Any]) -> str:
    cb_matches = [row for row in matches.get("cb_quotes", []) if isinstance(row, Mapping)]
    if len(cb_matches) > 1:
        return "multi_contract"
    if cb_matches:
        return "contract"
    return "global"


def _source_action_block_reason(kind: str, *, linked: bool, universe_items: list[dict[str, Any]]) -> str:
    if kind == "contract":
        return "contract files are edited through the Terms view; rename/remove is blocked"
    if kind == "raw_prospectus" and linked:
        return "linked to extracted contract or review queue; use guarded prospectus actions"
    if kind in {"raw_price_history", "generated_market_history"} and universe_items:
        return "referenced by coverage universe; rename will sync universe, remove is blocked"
    return ""


def _known_sha_for_source(rel: str, contracts: list[dict[str, Any]], review_items: list[dict[str, Any]], prospectus_items: list[dict[str, Any]]) -> str:
    for item in [*review_items, *prospectus_items]:
        digest = str(item.get("source_sha256") or "")
        if digest:
            return digest
    for item in contracts:
        digest = str(item.get("raw_prospectus_sha256") or "")
        if digest:
            return digest
    return ""


def _load_contract_source_summaries() -> dict[str, dict[str, Any]]:
    summaries: dict[str, dict[str, Any]] = {}
    root = resolve_project_path(CONTRACTS_DIR)
    if not root.exists():
        return summaries
    for path in root.glob("*.json"):
        rel = _display_path(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            summaries[rel] = {"contract_path": rel, "status": "invalid_json"}
            continue
        if not isinstance(raw, Mapping):
            continue
        display_parts = _instrument_display_parts(raw, path.stem)
        issuer = raw.get("issuer") if isinstance(raw.get("issuer"), Mapping) else {}
        conversion = raw.get("conversion") if isinstance(raw.get("conversion"), Mapping) else {}
        instrument = raw.get("instrument") if isinstance(raw.get("instrument"), Mapping) else {}
        review = raw.get("source_review") if isinstance(raw.get("source_review"), Mapping) else {}
        summaries[rel] = {
            "contract_id": str(raw.get("id") or path.stem),
            "contract_path": rel,
            "status": str(raw.get("status") or ""),
            **display_parts,
            "issuer_legal_name": display_parts["instrument_legal_name"],
            "canonical_id": str(raw.get("isin") or instrument.get("canonical_id") or ""),
            "underlying_ticker": str(conversion.get("underlying_ticker") or issuer.get("ticker") or ""),
            "source_file": _safe_display_optional_path(raw.get("source_file")),
            "raw_prospectus_path": _safe_display_optional_path(review.get("raw_prospectus_path")),
            "raw_prospectus_sha256": str(review.get("raw_prospectus_sha256") or ""),
            "review_status": str(review.get("review_status") or ""),
            "last_gui_edit": review.get("last_gui_edit") if isinstance(review.get("last_gui_edit"), Mapping) else {},
        }
    return summaries


def _safe_display_optional_path(value: Any) -> str:
    if not value:
        return ""
    try:
        return _display_path(resolve_project_path(str(value)))
    except ValueError:
        return "[REDACTED path outside project]"


def _contract_links_by_path(contracts: Mapping[str, dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    links: dict[str, list[dict[str, Any]]] = {}
    for contract in contracts.values():
        for key in ("source_file", "raw_prospectus_path"):
            rel = contract.get(key)
            if rel and rel != "[REDACTED path outside project]":
                links.setdefault(str(rel), []).append(contract)
    return links


def _review_links_by_path(items: list[Any]) -> dict[str, list[dict[str, Any]]]:
    links: dict[str, list[dict[str, Any]]] = {}
    for raw in items:
        if not isinstance(raw, Mapping):
            continue
        rel = _safe_display_optional_path(raw.get("source_path"))
        if rel:
            links.setdefault(rel, []).append(_sanitize_review_queue_item(raw))
    return links


def _prospectus_inventory_links_by_path(items: list[Any]) -> dict[str, list[dict[str, Any]]]:
    links: dict[str, list[dict[str, Any]]] = {}
    for raw in items:
        if not isinstance(raw, Mapping):
            continue
        rel = _safe_display_optional_path(raw.get("source_path") or raw.get("path"))
        if not rel:
            continue
        clean = {str(k): v for k, v in raw.items() if k in {"status", "source_sha256", "source_filename", "contract_path", "message"}}
        if clean.get("contract_path"):
            clean["contract_path"] = _safe_display_optional_path(clean["contract_path"])
        links.setdefault(rel, []).append(clean)
    return links


def _universe_links_by_path(
    items: list[Any],
    *,
    contracts: Mapping[str, dict[str, Any]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    links: dict[str, list[dict[str, Any]]] = {}
    contracts = contracts or {}
    for raw in items:
        if not isinstance(raw, Mapping):
            continue
        contract_path = _safe_display_optional_path(raw.get("contract_path"))
        contract_summary = contracts.get(contract_path, {}) if contract_path else {}
        clean = {
            "id": raw.get("id"),
            "issuer": raw.get("issuer"),
            "status": raw.get("status"),
            "contract_path": contract_path or raw.get("contract_path"),
        }
        for key in (
            "contract_id",
            "instrument_display_name",
            "instrument_short_name",
            "instrument_legal_name",
            "instrument_raw_display_name",
            "canonical_id",
            "underlying_ticker",
        ):
            if contract_summary.get(key):
                clean[key] = contract_summary[key]
        for key in ("contract_path", "market_history_path", "raw_price_history_path"):
            rel = contract_path if key == "contract_path" else _safe_display_optional_path(raw.get(key))
            if rel:
                links.setdefault(rel, []).append({**clean, "linked_field": key})
    return links


def _path_references_in_universe(path: Path) -> list[dict[str, Any]]:
    return _universe_links_by_path(_load_json_list(resolve_project_path(DEFAULT_UNIVERSE), default=[])).get(_display_path(path), [])


def _rewrite_universe_source_path(old_path: Path, new_path: Path, *, fields: list[str]) -> bool:
    universe_path = resolve_project_path(DEFAULT_UNIVERSE)
    items = _load_json_list(universe_path, default=[])
    changed = False
    for item in items:
        if not isinstance(item, dict):
            continue
        for field in fields:
            if item.get(field):
                try:
                    matches = resolve_project_path(str(item[field])) == old_path
                except ValueError:
                    matches = False
                if matches or item.get(field) == _display_path(old_path):
                    item[field] = _display_path(new_path)
                    changed = True
    if changed:
        _write_json_atomic(universe_path, items)
    return changed


def _rewrite_prospectus_inventory_source(old_path: Path, *, new_path: Path | None = None, delete: bool = False) -> bool:
    inventory_path = resolve_project_path(f"{COVERAGE_DIR}/prospectus_inventory.json")
    if not inventory_path.exists():
        return False
    items = _load_json_list(inventory_path, default=[])
    next_items: list[Any] = []
    changed = False
    old_display = _display_path(old_path)
    for raw in items:
        if not isinstance(raw, dict):
            next_items.append(raw)
            continue
        rel = _safe_display_optional_path(raw.get("source_path") or raw.get("path"))
        matches = rel == old_display or raw.get("source_filename") == old_path.name
        if matches and delete:
            changed = True
            continue
        if matches and new_path is not None:
            raw = dict(raw)
            if "source_path" in raw:
                raw["source_path"] = _display_path(new_path)
            if "path" in raw:
                raw["path"] = _display_path(new_path)
            raw["source_filename"] = new_path.name
            raw["prospectus_id"] = re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", new_path.stem.lower())).strip("_") or "prospectus"
            changed = True
        next_items.append(raw)
    if changed:
        _write_json_atomic(inventory_path, next_items)
    return changed


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    candidate = dumps_json(value, indent=2, ensure_ascii=False) + "\n"
    json.loads(candidate)
    backup = path.with_name(f"{path.name}.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.bak")
    if path.exists():
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp") as handle:
        handle.write(candidate)
        tmp_path = Path(handle.name)
    tmp_path.replace(path)


def _source_inventory_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    by_kind: dict[str, int] = {}
    needs_attention = 0
    for item in items:
        by_kind[item["kind"]] = by_kind.get(item["kind"], 0) + 1
        if not item.get("exists") or item.get("warnings") or "missing" in str(item.get("status", "")).lower():
            needs_attention += 1
    return {"source_count": len(items), "by_kind": by_kind, "needs_attention": needs_attention}


def build_batch_payload(
    *,
    contract_path: str | Path = DEFAULT_CONTRACT,
    market_history_path: str | Path = DEFAULT_MARKET_HISTORY,
    raw_price_history_path: str | Path | None = None,
    volatility: float = DEFAULT_VOLATILITY,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
    credit_spread: float = DEFAULT_CREDIT_SPREAD,
    borrow_rate: float = DEFAULT_BORROW_RATE,
    dividend_yield: float = DEFAULT_DIVIDEND_YIELD,
    steps: int = DEFAULT_STEPS,
    use_yield_curve: bool = DEFAULT_USE_YIELD_CURVE,
    yield_curve_currency: str = "",
    yield_curve: YieldCurve | None = None,
    use_history_assumptions: bool = True,
    assumption_set_id: int | None = None,
    db_path: str | Path | None = None,
    assumption_source_label: str | None = None,
    model_mode: str = DEFAULT_MODEL_MODE,
) -> dict[str, Any]:
    """Run the pricing core and return chart-ready JSON data."""

    contract_file = resolve_project_path(contract_path)
    canonical_source = _canonical_source_for_contract(contract_file)
    if canonical_source.get("market_history_path") and _is_default_market_history_path(market_history_path):
        market_history_path = canonical_source["market_history_path"]
    if raw_price_history_path in (None, "") or str(raw_price_history_path) == DEFAULT_RAW_PRICE_HISTORY:
        raw_price_history_path = canonical_source.get("raw_price_history_path") or None
    history_file = resolve_project_path(market_history_path)
    saved_assumption = None
    if assumption_set_id is not None:
        saved_assumption = _store(db_path).get_assumption_set(int(assumption_set_id))
        if saved_assumption is None:
            raise ValueError(f"assumption_set_id not found: {assumption_set_id}")
        volatility = saved_assumption.assumptions.volatility
        risk_free_rate = saved_assumption.assumptions.risk_free_rate
        credit_spread = saved_assumption.assumptions.credit_spread
        borrow_rate = saved_assumption.assumptions.borrow_rate
        dividend_yield = saved_assumption.assumptions.dividend_yield
        steps = saved_assumption.assumptions.steps
        use_yield_curve = saved_assumption.use_yield_curve
        yield_curve_currency = saved_assumption.yield_curve_currency
        use_history_assumptions = False
    contract = load_contract_json(contract_file)
    if saved_assumption is not None and saved_assumption.contract_id != contract.id:
        raise ValueError(
            f"assumption_set_id {saved_assumption.id} belongs to {saved_assumption.contract_id}, not {contract.id}"
        )
    validate_market_history_file_for_contract(history_file, contract).raise_for_errors()
    rows = load_market_history_csv(history_file)
    if not use_history_assumptions:
        rows = [replace(row, assumption_overrides={}) for row in rows]
    curve_matches: list[dict[str, Any]] = []
    curve_metadata: dict[str, Any] | None = None
    if use_yield_curve:
        selected_curve_currency = (
            str(yield_curve_currency or "").strip().upper()
            or curve_currency_for_contract(contract)
        )
        curve = yield_curve or _cached_worldgovernmentbonds_curve(selected_curve_currency)
        adjusted_rows = []
        for row in rows:
            matched = match_curve_for_contract(contract, row.as_of_date, curve)
            overrides = dict(row.assumption_overrides)
            overrides["risk_free_rate"] = matched.rate
            adjusted_rows.append(replace(row, assumption_overrides=overrides))
            curve_matches.append(
                {
                    "date": row.as_of_date.isoformat(),
                    "target_date": matched.target_date.isoformat(),
                    "target_years": matched.target_years,
                    "risk_free_rate": matched.rate,
                    "matched_label": matched.matched_label,
                }
            )
        rows = adjusted_rows
        curve_metadata = {
            "enabled": True,
            "currency": curve.currency,
            "source": curve.source,
            "as_of": curve.as_of,
            "points": [{"years": point.years, "rate": point.rate, "label": point.label} for point in curve.points],
            "matches": curve_matches,
        }
    assumptions = Assumptions(
        volatility=float(volatility),
        risk_free_rate=float(risk_free_rate),
        credit_spread=float(credit_spread),
        borrow_rate=float(borrow_rate),
        dividend_yield=float(dividend_yield),
        steps=_bounded_steps(steps),
    )
    results = price_history(contract, rows, assumptions, engine=PricingEngine(model_mode=model_mode))
    if use_yield_curve:
        results = [replace(result, assumption_source="yield_curve") for result in results]
    if saved_assumption is not None or assumption_source_label:
        source = assumption_source_label or "saved_assumption_set"
        results = [replace(result, assumption_source=source) for result in results]
    try:
        issue_yield_validation = issuance_yield_checks(contract)
    except (TypeError, ValueError, OverflowError):
        issue_yield_validation = {
            "yield_to_maturity": {},
            "yield_to_puts": [],
        }
    yield_summary = _batch_yield_summary(
        contract,
        results,
        issue_yield_validation,
    )
    series = [_result_to_api_row(row) for row in results]
    raw_quote_history = _raw_quote_history_payload(raw_price_history_path, contract)
    priced = [row for row in results if row.fair_value is not None]
    iv_values = [row.implied_volatility for row in results if row.implied_volatility is not None]
    cheapness_values = [row.cheapness for row in results if row.cheapness is not None]
    warnings = [row.warnings for row in results if row.warnings]
    contract_identity = cb_identity_from_contract(contract.raw if hasattr(contract, "raw") else json.loads(contract_file.read_text(encoding="utf-8")), fallback_id=contract.id).to_payload()
    source_ids = _canonical_source_ids_for_paths(contract_file, history_file, raw_price_history_path)
    return {
        "contract": {
            "id": contract.id,
            "contract_id": contract.id,
            "instrument_key": contract_identity.get("instrument_key", ""),
            "display_id": contract_identity.get("display_id", contract_identity.get("display_name", "")),
            "issuer": contract.issuer,
            "description": contract.description,
            "currency": contract.currency,
            "stock_currency": contract.stock_currency,
            "underlying_ticker": contract.conversion.underlying_ticker,
            "maturity_date": contract.maturity_date.isoformat(),
            "conversion_price": contract.conversion.conversion_price,
            "quoted_yield_to_maturity": contract.yield_to_maturity,
        },
        "inputs": {
            "contract_path": _display_path(contract_file),
            "market_history_path": _display_path(history_file),
            "canonical_source": canonical_source,
            "raw_price_history_path": _display_path(resolve_project_path(raw_price_history_path)) if raw_price_history_path else "",
            "source_ids": source_ids,
            "canonical_store_path": _display_path(_canonical_store_path()),
            "volatility": volatility,
            "risk_free_rate": risk_free_rate,
            "credit_spread": credit_spread,
            "borrow_rate": borrow_rate,
            "dividend_yield": dividend_yield,
            "steps": _bounded_steps(steps),
            "use_history_assumptions": bool(use_history_assumptions),
            "model_mode": model_mode,
        },
        "summary": {
            "row_count": len(results),
            "priced_row_count": len(priced),
            "warning_count": len(warnings),
            "min_implied_volatility": min(iv_values) if iv_values else None,
            "max_implied_volatility": max(iv_values) if iv_values else None,
            "latest_implied_volatility": iv_values[-1] if iv_values else None,
            "latest_cheapness": cheapness_values[-1] if cheapness_values else None,
            **yield_summary,
            "output_currency": priced[-1].output_currency if priced else contract.currency,
        },
        "issue_yield_validation": issue_yield_validation,
        "yield_curve": curve_metadata or {"enabled": False},
        "raw_quote_history": raw_quote_history,
        "assumption_set": _assumption_set_to_api(saved_assumption) if saved_assumption else None,
        "model_version": f"{model_mode}:{PRICING_MODEL_VERSION}",
        "series": series,
    }


def _canonical_source_ids_for_paths(contract_file: Path, history_file: Path, raw_price_history_path: str | Path | None) -> dict[str, Any]:
    result = {"contract_source_id": "", "market_history_source_id": "", "raw_price_history_source_id": ""}
    try:
        sync_canonical_catalog()
        store = canonical_store()
        mappings = [
            ("contract_source_id", contract_file, "contract_json"),
            ("market_history_source_id", history_file, "generated_market_history"),
        ]
        if raw_price_history_path:
            raw_path = resolve_project_path(raw_price_history_path)
            if raw_path.exists():
                mappings.append(("raw_price_history_source_id", raw_path, "raw_price_history"))
        for key, path, kind in mappings:
            if path.exists():
                result[key] = store.register_source_file(path, artifact_kind=kind, canonical_path=_display_path(path))["id"]
    except Exception as exc:
        result["warning"] = f"canonical source id lookup failed: {exc}"
    return result


def _canonical_source_for_contract(contract_path: Path) -> dict[str, str]:
    """Return the single CB-scoped source record, preferring the canonical SQLite catalog."""

    contract_display = _display_path(contract_path)
    try:
        sync_canonical_catalog()
        raw_contract = json.loads(contract_path.read_text(encoding="utf-8"))
        contract_record = canonical_store().upsert_contract(raw_contract, source_path=contract_display)
        coverage = canonical_store().coverage_member_for_contract(contract_record["contract_id"])
        if coverage:
            return {
                "contract_path": contract_display,
                "market_history_path": str(coverage.get("market_history_path") or ""),
                "raw_price_history_path": str(coverage.get("raw_price_history_path") or ""),
                "source_of_truth": f"{DEFAULT_DB}:coverage_universe",
                "canonical_record_id": str(contract_record.get("id") or ""),
                "canonical_coverage_id": str(coverage.get("id") or ""),
            }
    except Exception:
        pass
    for raw in _load_json_list(resolve_project_path(DEFAULT_UNIVERSE), default=[]):
        if not isinstance(raw, Mapping):
            continue
        if _safe_display_optional_path(raw.get("contract_path")) != contract_display:
            continue
        market_history_path = _safe_display_optional_path(raw.get("market_history_path"))
        raw_price_history_path = _safe_display_optional_path(raw.get("raw_price_history_path"))
        return {
            "contract_path": contract_display,
            "market_history_path": market_history_path,
            "raw_price_history_path": raw_price_history_path,
            "source_of_truth": DEFAULT_UNIVERSE,
        }
    return {
        "contract_path": contract_display,
        "market_history_path": "",
        "raw_price_history_path": "",
        "source_of_truth": DEFAULT_UNIVERSE,
    }


def _is_default_market_history_path(value: str | Path) -> bool:
    try:
        return _display_path(resolve_project_path(value)) == DEFAULT_MARKET_HISTORY
    except ValueError:
        return str(value) == DEFAULT_MARKET_HISTORY


def _store(db_path: str | Path | None = None) -> CbTerminalStore:
    if db_path is None or str(db_path) == "":
        return CbTerminalStore(resolve_project_path(DEFAULT_DB))
    return CbTerminalStore(Path(db_path))


def build_assumptions_payload(contract_id: str, scenario_name: str = "base", *, db_path: str | Path | None = None) -> dict[str, Any]:
    record = _store(db_path).latest_assumption_set(contract_id, scenario_name or "base")
    return {"contract_id": contract_id, "scenario_name": scenario_name or "base", "assumption_set": _assumption_set_to_api(record) if record else None}


def save_assumptions_payload(payload: Mapping[str, Any], *, db_path: str | Path | None = None) -> dict[str, Any]:
    _require_interactive_assumptions(payload)
    contract_id = str(payload.get("contract_id") or "").strip()
    if not contract_id:
        raise ValueError("Select a convertible bond before saving assumptions.")
    assumptions = _assumptions_from_mapping(payload)
    record = _store(db_path).save_assumption_set(
        contract_id=contract_id,
        scenario_name=str(payload.get("scenario_name") or "base"),
        assumptions=assumptions,
        use_yield_curve=_mapping_bool(payload, "use_yield_curve", False),
        yield_curve_currency=str(payload.get("yield_curve_currency") or ""),
        notes=str(payload.get("notes") or ""),
        created_by=str(payload.get("created_by") or "local_gui"),
    )
    return {"assumption_set": _assumption_set_to_api(record)}


def preview_pricing_payload(payload: Mapping[str, Any], *, db_path: str | Path | None = None) -> dict[str, Any]:
    _require_interactive_assumptions(payload)
    priced = build_batch_payload(
        contract_path=str(payload.get("contract_path") or DEFAULT_CONTRACT),
        market_history_path=str(payload.get("market_history_path") or DEFAULT_MARKET_HISTORY),
        raw_price_history_path=str(payload.get("raw_price_history_path") or "") or None,
        volatility=_mapping_rate_decimal(payload, "volatility", DEFAULT_VOLATILITY, unit="percent"),
        risk_free_rate=_mapping_rate_decimal(payload, "risk_free_rate", DEFAULT_RISK_FREE_RATE, unit="percent"),
        credit_spread=_mapping_rate_decimal(payload, "credit_spread", DEFAULT_CREDIT_SPREAD, unit="bps"),
        borrow_rate=_mapping_rate_decimal(payload, "borrow_rate", DEFAULT_BORROW_RATE, unit="percent"),
        dividend_yield=_mapping_rate_decimal(payload, "dividend_yield", DEFAULT_DIVIDEND_YIELD, unit="percent"),
        steps=_bounded_steps(_mapping_float(payload, "steps", DEFAULT_STEPS)),
        use_yield_curve=_mapping_bool(payload, "use_yield_curve", DEFAULT_USE_YIELD_CURVE),
        yield_curve_currency=str(payload.get("yield_curve_currency") or ""),
        use_history_assumptions=_mapping_bool(payload, "use_history_assumptions", False),
        model_mode=str(payload.get("model_mode") or DEFAULT_MODEL_MODE),
        assumption_source_label="preview",
    )
    if _mapping_bool(payload, "record_run", False):
        store = _store(db_path)
        run = store.create_valuation_run(
            contract_id=priced["contract"]["id"],
            assumption_set_id=None,
            model_version=priced["model_version"],
            run_type="preview",
            inputs={k: payload.get(k) for k in sorted(payload)},
        )
        store.save_valuation_results(run.id, [_api_row_to_store_result(row) for row in priced["series"]])
        priced["valuation_run"] = _valuation_run_to_api(run)
    return priced


def build_nuke_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a dollar-neutral linear reprice from explicit anchor inputs."""

    inputs = {
        "anchor_bond_price": float(payload["anchor_bond_price"]),
        "anchor_stock_price": float(payload["anchor_stock_price"]),
        "anchor_fx": float(payload["anchor_fx"]),
        "current_stock_price": float(payload["current_stock_price"]),
        "current_fx": float(payload["current_fx"]),
        "delta": float(payload["delta"]),
    }
    nuked_bond_price = nuke(**inputs)
    anchor_stock_in_bond_currency = inputs["anchor_stock_price"] / inputs["anchor_fx"]
    current_stock_in_bond_currency = inputs["current_stock_price"] / inputs["current_fx"]
    return {
        **inputs,
        "anchor_stock_in_bond_currency": anchor_stock_in_bond_currency,
        "current_stock_in_bond_currency": current_stock_in_bond_currency,
        "stock_move_in_bond_currency": current_stock_in_bond_currency - anchor_stock_in_bond_currency,
        "bond_price_change": nuked_bond_price - inputs["anchor_bond_price"],
        "nuked_bond_price": nuked_bond_price,
    }


def _raw_quote_history_payload(path: str | Path | None, contract: Any) -> dict[str, Any]:
    """Return raw detailed quote rows for display; not used as model-ready pricing input."""

    if not path:
        return {"enabled": False, "rows": [], "row_count": 0}
    raw_path = resolve_project_path(path)
    if not raw_path.exists():
        return {"enabled": False, "path": _display_path(raw_path), "rows": [], "row_count": 0, "warning": "raw price-history file not found"}
    instrument = contract.metadata.get("instrument", {}) if getattr(contract, "metadata", None) else {}
    isin = str(instrument.get("canonical_id") or "").strip().upper()
    rows = []
    total_count = 0
    truncated = False
    for row in load_price_history_file(raw_path, contract_id=contract.id):
        if isin and (row.instrument_id or "").strip().upper() != isin:
            continue
        total_count += 1
        if len(rows) >= RAW_QUOTE_DISPLAY_LIMIT:
            truncated = True
            continue
        rows.append(
            {
                "date": row.as_of_date.isoformat(),
                "time": row.as_of_time.isoformat(timespec="minutes") if row.as_of_time else "",
                "dealer": row.dealer,
                "source_type": row.source_type,
                "security": row.security,
                "instrument_id": row.instrument_id,
                "reference_security": row.reference_security,
                "bid_price": row.bid_price,
                "ask_price": row.ask_price,
                "mid_price": row.mid_price,
                "stock_price": row.stock_price,
                "price_currency": row.price_currency,
                "source_row": row.source_row,
            }
        )
    rows.sort(key=lambda r: (r["date"], r["time"], r["source_row"]))
    warning = f"raw quote display truncated to {RAW_QUOTE_DISPLAY_LIMIT} rows" if truncated else ""
    return {
        "enabled": True,
        "path": _display_path(raw_path),
        "isin": isin,
        "row_count": total_count,
        "display_row_count": len(rows),
        "truncated": truncated,
        "warning": warning,
        "rows": rows,
    }


def _assumptions_from_mapping(payload: Mapping[str, Any]) -> Assumptions:
    return Assumptions(
        volatility=_mapping_rate_decimal(payload, "volatility", DEFAULT_VOLATILITY, unit="percent"),
        risk_free_rate=_mapping_rate_decimal(payload, "risk_free_rate", DEFAULT_RISK_FREE_RATE, unit="percent"),
        credit_spread=_mapping_rate_decimal(payload, "credit_spread", DEFAULT_CREDIT_SPREAD, unit="bps"),
        borrow_rate=_mapping_rate_decimal(payload, "borrow_rate", DEFAULT_BORROW_RATE, unit="percent"),
        dividend_yield=_mapping_rate_decimal(payload, "dividend_yield", DEFAULT_DIVIDEND_YIELD, unit="percent"),
        steps=_bounded_steps(_mapping_float(payload, "steps", DEFAULT_STEPS)),
    )


def _require_interactive_assumptions(payload: Mapping[str, Any]) -> None:
    """Fail closed when the GUI has not collected economic assumptions.

    Zero is a valid explicit assumption for fields such as borrow or dividend.
    The distinction here is presence: an empty form must never be converted
    into a silent zero-rate/zero-spread valuation.
    """

    labels = {
        "volatility": "volatility",
        "credit_spread": "credit spread",
        "borrow_rate": "borrow cost",
        "dividend_yield": "dividend yield",
    }
    missing = [
        label
        for name, label in labels.items()
        if payload.get(name) is None or str(payload.get(name)).strip() == ""
    ]
    if not _mapping_bool(payload, "use_yield_curve", False):
        risk_free_value = payload.get("risk_free_rate")
        if risk_free_value is None or str(risk_free_value).strip() == "":
            missing.append("risk-free source (select the economic-currency curve or enter a manual rate)")
    if missing:
        raise ValueError(
            "Pricing assumptions required before valuation: " + ", ".join(missing) + "."
        )


def _assumption_set_to_api(record: AssumptionSetRecord | None) -> dict[str, Any] | None:
    if record is None:
        return None
    return {
        "id": record.id,
        "contract_id": record.contract_id,
        "scenario_name": record.scenario_name,
        "volatility": record.assumptions.volatility,
        "risk_free_rate": record.assumptions.risk_free_rate,
        "credit_spread": record.assumptions.credit_spread,
        "borrow_rate": record.assumptions.borrow_rate,
        "dividend_yield": record.assumptions.dividend_yield,
        "steps": record.assumptions.steps,
        "use_yield_curve": record.use_yield_curve,
        "yield_curve_currency": record.yield_curve_currency,
        "notes": record.notes,
        "created_by": record.created_by,
        "created_at": record.created_at,
        "supersedes_assumption_set_id": record.supersedes_assumption_set_id,
    }


def _valuation_run_to_api(record: ValuationRunRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "contract_id": record.contract_id,
        "assumption_set_id": record.assumption_set_id,
        "model_version": record.model_version,
        "run_type": record.run_type,
        "inputs": record.inputs,
        "created_at": record.created_at,
    }


def _api_row_to_store_result(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "as_of_date": row.get("date"),
        "fair_value": row.get("fair_value"),
        "market_price": row.get("bond_price"),
        "cheapness": row.get("cheapness"),
        "parity": row.get("parity"),
        "bond_floor": row.get("bond_floor"),
        "implied_volatility": row.get("implied_volatility"),
        "warnings": row.get("warnings") or row.get("error") or "",
        "diagnostics": {
            "assumption_source": row.get("assumption_source"),
            "yield_to_maturity": row.get("yield_to_maturity"),
            "yield_to_put": row.get("yield_to_put"),
            "yield_to_put_date": row.get("yield_to_put_date"),
            "yield_accrued_interest": row.get("yield_accrued_interest"),
            "yield_dirty_price": row.get("yield_dirty_price"),
            "yield_price_basis": row.get("yield_price_basis"),
            "yield_warning": row.get("yield_warning"),
        },
    }


def _bounded_steps(value: float | int) -> int:
    steps = int(value)
    if steps < 1 or steps > MAX_TREE_STEPS:
        raise ValueError(f"steps must be between 1 and {MAX_TREE_STEPS}")
    return steps


def _mapping_float(payload: Mapping[str, Any], name: str, default: float) -> float:
    value = payload.get(name, default)
    if value in (None, ""):
        value = default
    return float(value)


def _rate_input_to_decimal(value: float, *, unit: str) -> float:
    """Accept GUI display units while preserving decimal API compatibility.

    GUI inputs are PM-friendly: credit spread in bps, volatility/borrow/dividend/RF
    in percent. Existing API/tests may still send decimals, so only values whose
    absolute magnitude is above 1 are interpreted as display-unit entries.
    """

    number = float(value)
    if abs(number) <= 1:
        return number
    if unit == "bps":
        return number / 10_000.0
    if unit == "percent":
        return number / 100.0
    raise ValueError(f"unsupported rate input unit: {unit}")


def _mapping_rate_decimal(payload: Mapping[str, Any], name: str, default: float, *, unit: str) -> float:
    raw_value = payload.get(name)
    if raw_value is None or str(raw_value).strip() == "":
        return float(default)
    value = float(raw_value)
    if str(payload.get("input_units") or "").strip().lower() == "display":
        if unit == "bps":
            return value / 10_000.0
        if unit == "percent":
            return value / 100.0
    return _rate_input_to_decimal(value, unit=unit)


def _mapping_bool(payload: Mapping[str, Any], name: str, default: bool = False) -> bool:
    value = payload.get(name, default)
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _cached_worldgovernmentbonds_curve(currency: str) -> YieldCurve:
    normalized = currency.strip().upper()
    if normalized not in _YIELD_CURVE_CACHE:
        _YIELD_CURVE_CACHE[normalized] = fetch_worldgovernmentbonds_curve(normalized)
    return _YIELD_CURVE_CACHE[normalized]


def _display_path(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _load_json_list(path: Path, *, default: list[Any] | None = None) -> list[Any]:
    """Read a JSON list from disk, returning a copy of default when absent/invalid."""

    if default is None:
        default = []
    if not path.exists():
        return list(default)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return list(default)
    return raw if isinstance(raw, list) else list(default)


def _load_json_mapping(path: Path, *, default: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read a JSON object from disk, returning a shallow copy of default when absent/invalid."""

    if default is None:
        default = {}
    if not path.exists():
        return dict(default)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return dict(default)
    return raw if isinstance(raw, dict) else dict(default)


def _issuer_short_for_display(raw: Mapping[str, Any]) -> str:
    instrument = raw.get("instrument") if isinstance(raw.get("instrument"), Mapping) else {}
    issuer = raw.get("issuer") if isinstance(raw.get("issuer"), Mapping) else {}
    issuer_short = str(instrument.get("issuer_short_name") or issuer.get("short_name") or issuer.get("name") or "").strip()
    issuer_legal = str(instrument.get("issuer_legal_name") or issuer.get("name") or "").strip()
    issuer_short = issuer_short or issuer_legal
    normalized = issuer_short.lower()
    if normalized.startswith("china "):
        issuer_short = issuer_short[6:].strip()
        normalized = issuer_short.lower()
    first_word = issuer_short.split()[0] if issuer_short.split() else ""
    if len(issuer_short.split()) > 1 and re.fullmatch(r"[A-Z0-9&]{1,4}", first_word):
        issuer_short = first_word
    return issuer_short or "CB"


def _format_coupon_for_display(value: Any) -> str:
    try:
        coupon = float(value)
    except (TypeError, ValueError):
        return "?"
    pct = coupon * 100 if abs(coupon) <= 1 else coupon
    if abs(pct - round(pct)) < 1e-9:
        return str(int(round(pct)))
    return (f"{pct:.3f}".rstrip("0").rstrip("."))


def _maturity_suffix_for_display(value: Any) -> str:
    text = str(value or "").strip()
    match = re.search(r"(20\d{2}|19\d{2})", text)
    if match:
        return match.group(1)[-2:]
    return "??"


def _instrument_display_parts(raw: Mapping[str, Any], fallback: str = "") -> dict[str, str]:
    instrument = raw.get("instrument") if isinstance(raw.get("instrument"), Mapping) else {}
    issuer = raw.get("issuer") if isinstance(raw.get("issuer"), Mapping) else {}
    bond = raw.get("bond") if isinstance(raw.get("bond"), Mapping) else {}
    issuer_short = _issuer_short_for_display(raw)
    coupon = _format_coupon_for_display(bond.get("coupon_rate"))
    maturity = _maturity_suffix_for_display(instrument.get("maturity_year") or bond.get("maturity_date"))
    short_label = re.sub(r"\s+", " ", f"{issuer_short} {coupon} {maturity}").strip() or fallback or "CB"
    legal_name = str(instrument.get("issuer_legal_name") or issuer.get("name") or "").strip()
    raw_display = str(instrument.get("display_name") or fallback or short_label).strip()
    return {
        "instrument_display_name": short_label,
        "instrument_short_name": short_label,
        "instrument_legal_name": legal_name,
        "instrument_raw_display_name": raw_display,
    }


def _short_instrument_display_name(raw: Mapping[str, Any], fallback: str = "") -> str:
    return _instrument_display_parts(raw, fallback)["instrument_display_name"]


def _contract_summary_for_review_queue(contract_path: str | Path) -> dict[str, Any]:
    try:
        path = _resolve_contract_json_path(contract_path)
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, Mapping):
        return {}
    instrument = raw.get("instrument") if isinstance(raw.get("instrument"), Mapping) else {}
    issuer = raw.get("issuer") if isinstance(raw.get("issuer"), Mapping) else {}
    bond = raw.get("bond") if isinstance(raw.get("bond"), Mapping) else {}
    conversion = raw.get("conversion") if isinstance(raw.get("conversion"), Mapping) else {}
    display_parts = _instrument_display_parts(raw, str(raw.get("id") or Path(str(contract_path)).stem))
    source_review = raw.get("source_review") if isinstance(raw.get("source_review"), Mapping) else {}
    return {
        **display_parts,
        "status": raw.get("status") or source_review.get("review_status") or "",
        "review_status": source_review.get("review_status") or raw.get("status") or "",
        "approved_at": source_review.get("approved_at") or "",
        "approved_by": source_review.get("approved_by") or "",
        "issuer_legal_name": display_parts["instrument_legal_name"],
        "issuer_short_name": _issuer_short_for_display(raw),
        "coupon_rate": bond.get("coupon_rate"),
        "maturity_date": bond.get("maturity_date"),
        "currency": bond.get("currency"),
        "issue_size": bond.get("issue_size"),
        "conversion_price": conversion.get("initial_conversion_price"),
        "underlying_ticker": conversion.get("underlying_ticker") or issuer.get("ticker") or "",
    }


def build_review_queue_payload(review_queue_path: str | Path = "data/coverage/review_queue.json") -> dict[str, Any]:
    path = resolve_project_path(review_queue_path)
    if _display_path(path) == f"{COVERAGE_DIR}/review_queue.json":
        _reconcile_raw_prospectus_indexes()
    if not path.exists():
        return {"queue_path": _display_path(path), "items": [], "status": "missing"}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("review queue must be a JSON list")
    queue_items = [item for item in raw if isinstance(item, dict)]
    items = [_sanitize_review_queue_item(item) for item in queue_items]
    return {"queue_path": _display_path(path), "items": items, "status": "ok"}


def _source_index_keys(item: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    paths: set[str] = set()
    digests: set[str] = set()
    for key in ("source_path", "source_file", "path"):
        value = item.get(key)
        if value:
            paths.add(_safe_display_optional_path(value))
    if item.get("source_filename"):
        paths.add(f"{RAW_PROSPECTUS_DIR}/{item['source_filename']}")
    digest = str(item.get("source_sha256") or item.get("sha256") or "").strip()
    if digest:
        digests.add(digest)
    return paths, digests


def _append_missing_source_records(path: Path, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = _load_json_list(path, default=[])
    if not isinstance(items, list):
        items = []
    indexed_paths: set[str] = set()
    indexed_sha256: set[str] = set()
    for raw in items:
        if not isinstance(raw, Mapping):
            continue
        paths, digests = _source_index_keys(raw)
        indexed_paths.update(paths)
        indexed_sha256.update(digests)

    changed = False
    next_items: list[Any] = list(items)
    for record in records:
        rel = str(record.get("source_path") or "")
        digest = str(record.get("source_sha256") or "")
        if rel in indexed_paths or (digest and digest in indexed_sha256):
            continue
        next_items.append(dict(record))
        indexed_paths.add(rel)
        if digest:
            indexed_sha256.add(digest)
        changed = True
    if changed or not path.exists():
        _write_json_atomic(path, next_items)
    return [item for item in next_items if isinstance(item, dict)]


def _prune_stale_pending_source_records(path: Path, *, linked_paths: set[str], linked_digests: set[str]) -> bool:
    """Remove stale pending raw-inbox rows once a PDF is linked to a contract."""

    items = _load_json_list(path, default=[])
    if not isinstance(items, list):
        return False
    next_items: list[Any] = []
    changed = False
    for raw in items:
        if not isinstance(raw, Mapping):
            next_items.append(raw)
            continue
        status = str(raw.get("review_status") or raw.get("status") or "")
        has_contract = bool(raw.get("contract_path") or raw.get("contract_id"))
        paths, digests = _source_index_keys(raw)
        stale_pending = (
            not has_contract
            and status in {"", "pending_extraction", "needs_extraction_backend", "needs_ocr", "needs_manual_template"}
            and (bool(paths & linked_paths) or bool(digests & linked_digests))
        )
        if stale_pending:
            changed = True
            continue
        next_items.append(raw)
    if changed:
        _write_json_atomic(path, next_items)
    return changed


def _reconcile_raw_prospectus_indexes() -> None:
    """Make raw prospectus file discovery the shared source for intake indexes.

    Data Sources scans the raw prospectus directory directly. Prospectus Intake
    consumes review_queue/prospectus_inventory rows. Reconcile the derivative
    indexes from the raw directory so the two tabs cannot drift when PDFs were
    copied into the workspace or an index was reset.
    """

    raw_root = resolve_project_path(RAW_PROSPECTUS_DIR)
    if not raw_root.exists():
        return
    contract_summaries = _load_contract_source_summaries()
    contract_links = _contract_links_by_path(contract_summaries)
    contract_digests = {str(item.get("raw_prospectus_sha256") or "") for linked in contract_links.values() for item in linked}
    linked_paths = {path for path in contract_links if path}
    linked_digests = {digest for digest in contract_digests if digest}
    for index_name in ("review_queue.json", "prospectus_inventory.json"):
        _prune_stale_pending_source_records(resolve_project_path(f"{COVERAGE_DIR}/{index_name}"), linked_paths=linked_paths, linked_digests=linked_digests)
    detached_sources = _detached_raw_prospectus_paths()
    records = []
    for pdf in sorted(raw_root.iterdir(), key=lambda item: item.name.lower()):
        if not pdf.is_file() or pdf.suffix.lower() != ".pdf":
            continue
        rel = _display_path(pdf)
        digest = sha256_file(pdf)
        if rel in detached_sources:
            continue
        if rel in contract_links or digest in contract_digests:
            continue
        records.append(
            {
                "status": "pending_extraction",
                "review_status": "pending_extraction",
                "source_path": rel,
                "source_filename": pdf.name,
                "source_sha256": digest,
                "message": "Raw prospectus PDF exists on disk; pending conservative extraction.",
            }
        )
    if not records:
        return
    _append_missing_source_records(resolve_project_path(f"{COVERAGE_DIR}/review_queue.json"), records)
    _append_missing_source_records(resolve_project_path(f"{COVERAGE_DIR}/prospectus_inventory.json"), records)


def _detached_raw_prospectus_paths() -> set[str]:
    """Return raw prospectus files explicitly detached from contracts.

    Detaching a reviewed source PDF makes the file an ordinary unlinked upload in
    Data Sources. Do not immediately re-queue it as a new pending extraction just
    because it still exists under data/raw/prospectuses/.
    """

    paths: set[str] = set()
    root = resolve_project_path(CONTRACTS_DIR)
    if not root.exists():
        return paths
    for contract_path in root.glob("*.json"):
        try:
            raw = json.loads(contract_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        review = raw.get("source_review") if isinstance(raw, Mapping) else None
        detached = review.get("detached_prospectus") if isinstance(review, Mapping) else None
        if isinstance(detached, Mapping):
            source = str(detached.get("source_file") or "").strip()
            if source:
                paths.add(source)
    return paths


def _sync_review_queue_contract_status(
    contract_path: Path,
    *,
    status: str,
    review_status: str,
    clear_source_link: bool = False,
) -> None:
    queue_path = resolve_project_path("data/coverage/review_queue.json")
    if not queue_path.exists():
        return
    try:
        raw = json.loads(queue_path.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(raw, list):
        return
    target = _display_path(contract_path)
    changed = False
    for item in raw:
        if not isinstance(item, dict):
            continue
        item_contract = item.get("contract_path")
        if not item_contract:
            continue
        try:
            item_path = _display_path(resolve_project_path(str(item_contract)))
        except ValueError:
            item_path = str(item_contract)
        if item_path == target:
            item["status"] = status
            item["review_status"] = review_status
            if clear_source_link:
                for key in ("source_path", "source_file", "source_filename", "source_sha256"):
                    item.pop(key, None)
            changed = True
    if changed:
        _write_json_atomic(queue_path, raw)


def build_metric_views_payload() -> dict[str, Any]:
    """Return chart groups with one y-axis unit per group."""

    return {"groups": deepcopy(METRIC_GROUPS)}


def _contracts_root() -> Path:
    return resolve_project_path(CONTRACTS_DIR)


def _resolve_contract_json_path(value: str | Path) -> Path:
    path = resolve_project_path(value)
    try:
        path.relative_to(_contracts_root())
    except ValueError as exc:
        raise ValueError("contract_path must be under data/contracts") from exc
    if path.suffix.lower() != ".json":
        raise ValueError("contract_path must be a JSON file")
    return path


def _get_dotted(raw: Mapping[str, Any], dotted: str) -> Any:
    current: Any = raw
    for part in dotted.split("."):
        if part.isdigit():
            if not isinstance(current, list) or int(part) >= len(current):
                return None
            current = current[int(part)]
            continue
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def _set_dotted(raw: dict[str, Any], dotted: str, value: Any) -> None:
    current: Any = raw
    parts = dotted.split(".")
    for index, part in enumerate(parts[:-1]):
        next_part = parts[index + 1]
        if part.isdigit():
            idx = int(part)
            if not isinstance(current, list):
                raise ValueError(f"cannot index non-list path segment {part!r} in {dotted}")
            while len(current) <= idx:
                current.append({})
            current = current[idx]
        else:
            if not isinstance(current, dict):
                raise ValueError(f"cannot set nested field under non-object segment {part!r}")
            if part not in current or current[part] is None:
                current[part] = [] if next_part.isdigit() else {}
            current = current[part]
    leaf = parts[-1]
    if leaf.isdigit():
        idx = int(leaf)
        if not isinstance(current, list):
            raise ValueError(f"cannot index non-list leaf {leaf!r} in {dotted}")
        while len(current) <= idx:
            current.append(None)
        current[idx] = value
    else:
        if not isinstance(current, dict):
            raise ValueError(f"cannot set field {leaf!r} under non-object path")
        current[leaf] = value


def _coerce_contract_edit_value(field: str, value: Any, kind: str) -> Any:
    if kind == "text":
        return str(value).strip()
    if kind == "id_type":
        text = str(value).strip().upper()
        if text not in {"ISIN", "PENDING_ISIN"}:
            raise ValueError(f"{field} must be ISIN or PENDING_ISIN")
        return text
    if kind == "currency":
        text = str(value).strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", text):
            raise ValueError(f"{field} must be a 3-letter currency code")
        return text
    if kind == "date":
        text = str(value).strip()
        date.fromisoformat(text)
        return text
    if kind == "optional_date":
        if value in (None, ""):
            return None
        text = str(value).strip()
        date.fromisoformat(text)
        return text
    if kind == "float":
        return float(value)
    if kind == "optional_float":
        if value in (None, ""):
            return None
        return float(value)
    if kind == "positive_float":
        number = float(value)
        if number <= 0:
            raise ValueError(f"{field} must be positive")
        return number
    if kind == "optional_positive_float":
        if value in (None, ""):
            return None
        number = float(value)
        if number <= 0:
            raise ValueError(f"{field} must be positive when provided")
        return number
    if kind == "optional_nonnegative_float":
        if value in (None, ""):
            return None
        number = float(value)
        if number < 0:
            raise ValueError(f"{field} must be non-negative when provided")
        return number
    if kind == "nonnegative_int":
        number = int(value)
        if number < 0:
            raise ValueError(f"{field} must be non-negative")
        return number
    if kind == "optional_nonnegative_int":
        if value in (None, ""):
            return None
        number = int(value)
        if number < 0:
            raise ValueError(f"{field} must be non-negative when provided")
        return number
    if kind == "fx_convention":
        text = str(value).strip().upper().replace(" ", "_").replace("-", "_")
        if text not in {"CB_PER_STOCK", "STOCK_PER_CB"}:
            raise ValueError(f"{field} must be CB_PER_STOCK or STOCK_PER_CB")
        return text
    raise ValueError(f"unsupported edit type {kind!r} for {field}")


def _review_issues_to_api(issues: list[ReviewIssue]) -> list[dict[str, str]]:
    return [{"severity": issue.severity, "field": issue.field, "message": issue.message} for issue in issues]


def _contract_review_group_for_field(field: str) -> str:
    for group_id, _label, prefixes in CONTRACT_REVIEW_GROUPS:
        if any(field == prefix.rstrip(".") or field.startswith(prefix) for prefix in prefixes):
            return group_id
    return "special_clauses"


def _group_contract_review_fields(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {group_id: [] for group_id, _label, _prefixes in CONTRACT_REVIEW_GROUPS}
    for field in fields:
        grouped.setdefault(_contract_review_group_for_field(str(field.get("field") or "")), []).append(field)
    return [
        {"id": group_id, "label": label, "fields": grouped.get(group_id, [])}
        for group_id, label, _prefixes in CONTRACT_REVIEW_GROUPS
    ]


_PUT_YIELD_FIELD_PATTERN = re.compile(
    r"^puts\.(\d+)\.(?:yield_to_put(?:_frequency)?|"
    r"calculated_yield_to_put|yield_to_put_difference_bps)$"
)


def _put_yield_field_is_editable(raw: Mapping[str, Any], field: str) -> bool:
    match = _PUT_YIELD_FIELD_PATTERN.fullmatch(field)
    if not match:
        return True
    puts = raw.get("puts")
    index = int(match.group(1))
    return (
        isinstance(puts, list)
        and index < len(puts)
        and isinstance(puts[index], Mapping)
        and puts[index].get("model_type") == "scheduled_put"
    )


def _refreshable_economics_missing(raw: Mapping[str, Any]) -> bool:
    bond = raw.get("bond") if isinstance(raw.get("bond"), Mapping) else {}
    redemption = raw.get("redemption") if isinstance(raw.get("redemption"), Mapping) else {}
    missing = lambda value: value in (None, "", "needs_review")
    if (
        missing(bond.get("brokerage"))
        or missing(redemption.get("yield_to_maturity"))
        or missing(redemption.get("yield_to_maturity_frequency"))
    ):
        return True
    if (
        not missing(bond.get("brokerage"))
        and not missing(bond.get("issue_price"))
        and missing(bond.get("investor_offer_price"))
    ):
        return True
    for put in raw.get("puts") or []:
        if isinstance(put, Mapping) and put.get("model_type") == "scheduled_put":
            if missing(put.get("yield_to_put")) or missing(put.get("yield_to_put_frequency")):
                return True
    try:
        checks = issuance_yield_checks(loads_contract_json(dumps_json(raw)))
    except (TypeError, ValueError, OverflowError):
        return False
    if (checks.get("yield_to_maturity") or {}).get("status") == "mismatch":
        return True
    if any(
        isinstance(check, Mapping) and check.get("status") == "mismatch"
        for check in checks.get("yield_to_puts") or []
    ):
        return True
    return False


def build_contract_review_payload(contract_path: str | Path) -> dict[str, Any]:
    path = _resolve_contract_json_path(contract_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("contract JSON must contain an object")
    term_evidence = _get_dotted(raw, "source_review.term_evidence") or {}
    display_parts = _instrument_display_parts(raw, str(raw.get("id") or path.stem))
    issues = validate_contract_dict(raw)
    try:
        yield_validation = issuance_yield_checks(load_contract_json(path))
    except (TypeError, ValueError, OverflowError):
        yield_validation = {
            "yield_to_maturity": {},
            "yield_to_puts": [],
        }
    derived_yield_values: dict[str, Any] = {}
    ytm_check = yield_validation.get("yield_to_maturity")
    if isinstance(ytm_check, Mapping):
        derived_yield_values.update(
            {
                "redemption.calculated_yield_to_maturity": ytm_check.get(
                    "calculated_yield_percent"
                ),
                "redemption.yield_to_maturity_difference_bps": ytm_check.get(
                    "difference_bps"
                ),
            }
        )
    for put_check in yield_validation.get("yield_to_puts") or []:
        if not isinstance(put_check, Mapping):
            continue
        index = put_check.get("put_index")
        if not isinstance(index, int) or index not in {0, 1}:
            continue
        derived_yield_values[f"puts.{index}.calculated_yield_to_put"] = (
            put_check.get("calculated_yield_percent")
        )
        derived_yield_values[f"puts.{index}.yield_to_put_difference_bps"] = (
            put_check.get("difference_bps")
        )
    approval_fields = set(approval_required_evidence_fields(raw))
    fields = []
    for field, kind in CONTRACT_EDIT_ALLOWLIST.items():
        if not _put_yield_field_is_editable(raw, field):
            continue
        evidence_key = re.sub(r"\.(\d+)\.", r"[\1].", field)
        evidence = term_evidence.get(field) or term_evidence.get(evidence_key) or []
        if field == "bond.investor_offer_price":
            issue_evidence = term_evidence.get("bond.issue_price") or []
            brokerage_evidence = term_evidence.get("bond.brokerage") or []
            evidence = list(issue_evidence) + list(brokerage_evidence)
        evidence_list = evidence[:3] if isinstance(evidence, list) else []
        value = _get_dotted(raw, field)
        if field in derived_yield_values:
            value = derived_yield_values[field]
        if field == "instrument.display_name":
            value = display_parts["instrument_display_name"]
        matching_issues = [
            issue
            for issue in issues
            if issue.field == field
            or issue.field.startswith(field + ".")
            or field.startswith(issue.field + ".")
        ]
        evidence_required = evidence_key in approval_fields
        evidence_present = has_valid_page_evidence(evidence)
        attention_required = any(issue.severity == "error" for issue in matching_issues) or (
            evidence_required and not evidence_present
        )
        fields.append(
            {
                "field": field,
                "label": CONTRACT_FIELD_LABELS.get(field, field),
                "kind": kind,
                "value": value,
                "help": CONTRACT_FIELD_HELP.get(field, ""),
                "choices": CONTRACT_FIELD_CHOICES.get(field, []),
                "read_only": field in DERIVED_CONTRACT_FIELDS,
                "derived": field in DERIVED_CONTRACT_FIELDS,
                "primary": field in PRIMARY_CONTRACT_FIELDS,
                "attention_required": attention_required,
                "evidence_required": evidence_required,
                "unit_changing": field in UNIT_CHANGING_CONTRACT_FIELDS,
                "evidence": evidence_list,
                "evidence_count": len(evidence) if isinstance(evidence, list) else 0,
                "evidence_status": "derived" if field in DERIVED_CONTRACT_FIELDS else "present" if evidence_present else "missing" if evidence_required else "not_required",
                "issues": _review_issues_to_api(matching_issues),
            }
        )
    approval_blockers = _contract_approval_blockers(issues)
    return {
        "contract_path": _display_path(path),
        "contract_id": str(raw.get("id") or path.stem),
        **display_parts,
        "issuer_legal_name": display_parts["instrument_legal_name"],
        "status": str(raw.get("status") or ""),
        "source_file": _display_path(resolve_project_path(raw["source_file"])) if raw.get("source_file") else "",
        "editable_fields": fields,
        "editable_groups": _group_contract_review_fields(fields),
        "validation_issues": _review_issues_to_api(issues),
        "approval_blocker_count": len(approval_blockers),
        "attention_field_count": sum(1 for field in fields if field["attention_required"]),
        "refreshable_economics_missing": _refreshable_economics_missing(raw),
        "yield_validation": yield_validation,
    }


def edit_contract_terms_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not _mapping_bool(payload, "confirm", False):
        raise ValueError("contract term edit requires confirm=true")
    contract_path = _resolve_contract_json_path(str(payload.get("contract_path") or ""))
    edits = payload.get("edits")
    if not isinstance(edits, Mapping) or not edits:
        raise ValueError("edits must be a non-empty object")
    if len(edits) > 40:
        raise ValueError("too many contract fields in one edit")
    fields = sorted(str(field) for field in edits)
    forbidden = [field for field in fields if field not in CONTRACT_EDIT_ALLOWLIST]
    if forbidden:
        raise ValueError("unsupported contract edit field(s): " + ", ".join(forbidden))
    derived_edits = [field for field in fields if field in DERIVED_CONTRACT_FIELDS]
    if derived_edits:
        raise ValueError("derived contract field(s) cannot be edited directly: " + ", ".join(derived_edits))
    raw = json.loads(contract_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("contract JSON must contain an object")
    invalid_put_yield_fields = [field for field in fields if not _put_yield_field_is_editable(raw, field)]
    if invalid_put_yield_fields:
        raise ValueError(
            "yield-to-put fields are only editable for scheduled holder puts: "
            + ", ".join(invalid_put_yield_fields)
        )
    before = deepcopy(raw)
    old_values: dict[str, Any] = {}
    new_values: dict[str, Any] = {}
    for field in fields:
        kind = CONTRACT_EDIT_ALLOWLIST[field]
        value = _coerce_contract_edit_value(field, edits[field], kind)
        old_values[field] = _get_dotted(raw, field)
        new_values[field] = value
        _set_dotted(raw, field, value)

    derived_fields: list[str] = []
    if {"bond.issue_price", "bond.brokerage"} & set(fields):
        issue_price = _get_dotted(raw, "bond.issue_price")
        brokerage = _get_dotted(raw, "bond.brokerage")
        offer_field = "bond.investor_offer_price"
        old_values[offer_field] = _get_dotted(before, offer_field)
        offer_price = round(float(issue_price) + float(brokerage), 10) if issue_price not in (None, "") and brokerage not in (None, "") else None
        _set_dotted(raw, offer_field, offer_price)
        new_values[offer_field] = offer_price
        derived_fields.append(offer_field)

    explicit_reviewed = False
    source_review = raw.setdefault("source_review", {})
    if isinstance(source_review, dict):
        historical_human_fields = {
            str(field)
            for field in (source_review.get("human_edited_fields") or [])
        }
        previous_gui_edit = source_review.get("last_gui_edit")
        if isinstance(previous_gui_edit, Mapping):
            historical_human_fields.update(
                str(field)
                for field in (previous_gui_edit.get("fields") or [])
            )
        historical_human_fields.update(fields + derived_fields)
        source_review["human_edited_fields"] = sorted(historical_human_fields)
        source_review["last_gui_edit"] = {
            "edited_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "fields": fields + derived_fields,
            "created_by": str(payload.get("created_by") or "local_gui"),
        }
        if not explicit_reviewed:
            raw["status"] = "needs_review"
            source_review["review_status"] = "edited_needs_human_review"

    issues = validate_contract_dict(raw)
    error_issues = [issue for issue in issues if issue.severity == "error"]
    if error_issues:
        raise ValueError("contract edit failed validation: " + "; ".join(f"{i.field}: {i.message}" for i in error_issues))
    candidate = dumps_json(raw, indent=2, ensure_ascii=False) + "\n"
    loads_contract_json(candidate)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = contract_path.with_name(f"{contract_path.name}.{timestamp}.bak")
    backup_path.write_text(dumps_json(before, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=contract_path.parent, delete=False, suffix=".tmp") as handle:
        handle.write(candidate)
        tmp_path = Path(handle.name)
    tmp_path.replace(contract_path)
    _sync_review_queue_contract_status(contract_path, status="needs_review", review_status="edited_needs_human_review")
    return {
        "contract_path": _display_path(contract_path),
        "backup_path": _display_path(backup_path),
        "contract_id": str(raw.get("id") or contract_path.stem),
        "status": str(raw.get("status") or ""),
        "edited_fields": fields + derived_fields,
        "old_values": old_values,
        "new_values": new_values,
        "validation_issues": _review_issues_to_api(issues),
        "requires_review": raw.get("status") != "reviewed",
    }


def _contract_approval_blockers(issues: list[ReviewIssue]) -> list[ReviewIssue]:
    """Return validation issues that block explicit PM approval.

    Missing evidence remains a warning during draft editing/reporting, but the
    dedicated approval action is the pricing gate and must fail closed unless
    required modeled fields have page-level evidence attached.
    """

    blockers = [issue for issue in issues if issue.severity == "error"]
    blockers.extend(issue for issue in issues if issue.field == "source_review.term_evidence")
    return blockers


def approve_contract_terms_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not _mapping_bool(payload, "confirm", False):
        raise ValueError("contract approval requires confirm=true")
    contract_path = _resolve_contract_json_path(str(payload.get("contract_path") or ""))
    raw = json.loads(contract_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("contract JSON must contain an object")
    before = deepcopy(raw)
    issues = validate_contract_dict(raw)
    approval_blockers = _contract_approval_blockers(issues)
    if approval_blockers:
        raise ValueError("contract approval blocked: " + "; ".join(f"{i.field}: {i.message}" for i in approval_blockers))
    source_review = raw.setdefault("source_review", {})
    if isinstance(source_review, dict):
        source_review["review_status"] = "reviewed"
        source_review["approved_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        source_review["approved_by"] = str(payload.get("created_by") or "local_gui")
        source_review["approval_action"] = "terms_approved_for_pricing"
    raw["status"] = "reviewed"
    candidate = dumps_json(raw, indent=2, ensure_ascii=False) + "\n"
    loads_contract_json(candidate)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = contract_path.with_name(f"{contract_path.name}.{timestamp}.bak")
    backup_path.write_text(dumps_json(before, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=contract_path.parent, delete=False, suffix=".tmp") as handle:
        handle.write(candidate)
        tmp_path = Path(handle.name)
    tmp_path.replace(contract_path)
    _sync_review_queue_contract_status(contract_path, status="reviewed", review_status="reviewed")
    display_parts = _instrument_display_parts(raw, str(raw.get("id") or contract_path.stem))
    return {
        "contract_path": _display_path(contract_path),
        "backup_path": _display_path(backup_path),
        "contract_id": str(raw.get("id") or contract_path.stem),
        **display_parts,
        "issuer_legal_name": display_parts["instrument_legal_name"],
        "status": "reviewed",
        "review_status": "reviewed",
        "validation_issues": _review_issues_to_api(issues),
    }


def prospectus_action_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    action = str(payload.get("action") or "").strip()
    if action not in {"delete_raw", "archive_raw", "detach"}:
        raise ValueError("action must be detach, archive_raw, or delete_raw")
    if not _mapping_bool(payload, "confirm", False):
        raise ValueError("prospectus action requires confirm=true")
    contract_path = _resolve_contract_json_path(str(payload.get("contract_path") or ""))
    raw = json.loads(contract_path.read_text(encoding="utf-8"))
    source_file = raw.get("source_file") or (raw.get("source_review") or {}).get("raw_prospectus_path")
    contract_id = str(raw.get("id") or contract_path.stem)
    if action == "delete_raw":
        typed = str(payload.get("typed_confirmation") or payload.get("confirm_text") or "").strip()
        source_name = Path(str(source_file or "")).name
        if typed not in {contract_id, source_name}:
            raise ValueError("delete_raw requires typed_confirmation matching contract id or source filename")
    if action == "detach":
        if not _mapping_bool(payload, "confirm_detach", False):
            raise ValueError("detach requires confirm_detach=true")
        before = deepcopy(raw)
        source_review = raw.setdefault("source_review", {})
        if isinstance(source_review, dict):
            source_review["detached_prospectus"] = {
                "detached_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "source_file": source_file,
                "created_by": str(payload.get("created_by") or "local_gui"),
            }
            source_review.pop("raw_prospectus_path", None)
            source_review["review_status"] = "detached_needs_human_review"
        raw.pop("source_file", None)
        raw["status"] = "needs_review"
        candidate = dumps_json(raw, indent=2, ensure_ascii=False) + "\n"
        loads_contract_json(candidate)
        backup_path = contract_path.with_name(f"{contract_path.name}.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.bak")
        backup_path.write_text(dumps_json(before, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=contract_path.parent, delete=False, suffix=".tmp") as handle:
            handle.write(candidate)
            tmp_path = Path(handle.name)
        tmp_path.replace(contract_path)
        _sync_review_queue_contract_status(
            contract_path,
            status="needs_review",
            review_status="detached_needs_human_review",
            clear_source_link=True,
        )
        return {"action": action, "contract_path": _display_path(contract_path), "backup_path": _display_path(backup_path), "raw_deleted": False, "raw_archived": False, "status": raw["status"], "review_status": "detached_needs_human_review"}

    archive_dir = resolve_project_path("data/raw/prospectuses/archive") if action == "archive_raw" else None
    result = approve_reviewed_contract(
        contract_path,
        delete_raw=(action == "delete_raw"),
        archive_dir=archive_dir,
        raw_root=resolve_project_path(RAW_PROSPECTUS_DIR),
    )
    response = {"action": action, "contract_path": _display_path(Path(result["contract_path"])), "source_path": _display_path(Path(result["source_path"])), "raw_deleted": bool(result.get("raw_deleted")), "raw_archived": bool(result.get("raw_archived"))}
    if result.get("archive_path"):
        response["archive_path"] = _display_path(Path(result["archive_path"]))
    return response


def run_prospectus_extraction_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Run conservative raw-prospectus extraction/intake from the GUI.

    This scans data/raw/prospectuses, attempts available text extraction/fixtures,
    writes data/coverage/review_queue.json, and returns an auditable summary. It
    does not delete raw PDFs and does not mark contracts reviewed.
    """

    if not _mapping_bool(payload, "confirm", False):
        raise ValueError("prospectus extraction requires confirm=true")
    source_paths = _selected_raw_prospectus_paths(payload.get("source_paths"))
    report = auto_ingest_prospectuses(
        prospectus_dir=resolve_project_path(RAW_PROSPECTUS_DIR),
        contracts_dir=resolve_project_path(CONTRACTS_DIR),
        reviews_dir=resolve_project_path("data/reviews"),
        coverage_dir=resolve_project_path("data/coverage"),
        fixture_dir=resolve_project_path("data/prospectus_text_fixtures"),
        source_paths=source_paths,
    )
    items = _sanitize_review_queue_items(report.items)
    processed_items = items
    if source_paths is not None:
        selected_source_keys = {
            _display_path(path).replace("\\", "/").casefold()
            for path in source_paths
        }
        processed_items = [
            item
            for item in items
            if str(item.get("source_path") or "").replace("\\", "/").casefold()
            in selected_source_keys
        ]
    return {
        "status": "ok",
        "scanned": report.scanned,
        "created_contracts": report.created_contracts,
        "duplicates": report.duplicates,
        "needs_extraction": report.needs_extraction,
        "failed": report.failed,
        "queue_path": _display_path(report.queue_path) if report.queue_path else "",
        "source_paths": [_display_path(path) for path in source_paths] if source_paths is not None else [],
        "selection_mode": "selected" if source_paths is not None else "all",
        "extraction_environment": report.extraction_environment,
        "items": items,
        "processed_items": processed_items,
    }


def refresh_contract_economics_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Fill missing or safely reconcile extracted economics from the linked PDF."""

    if not _mapping_bool(payload, "confirm", False):
        raise ValueError("contract PDF refresh requires confirm=true")
    contract_path = _resolve_contract_json_path(str(payload.get("contract_path") or ""))
    raw = json.loads(contract_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("contract JSON must contain an object")
    source_review = raw.get("source_review") if isinstance(raw.get("source_review"), Mapping) else {}
    source_value = raw.get("source_file") or source_review.get("raw_prospectus_path")
    if not source_value:
        raise ValueError("contract has no linked prospectus PDF")
    source_path = _resolve_raw_prospectus_path(str(source_value))
    result = backfill_missing_issuance_economics(
        contract_path=contract_path,
        source_path=source_path,
        fixture_dir=resolve_project_path("data/prospectus_text_fixtures"),
    )
    if result["updated"]:
        review_status = (
            "reconciled_needs_human_review"
            if result.get("corrected_fields")
            else "backfilled_needs_human_review"
        )
        _sync_review_queue_contract_status(
            contract_path,
            status="needs_review",
            review_status=review_status,
        )
    return {
        "updated": bool(result["updated"]),
        "contract_path": _display_path(contract_path),
        "source_path": _display_path(source_path),
        "backup_path": _display_path(result["backup_path"]) if result.get("backup_path") else "",
        "added_fields": list(result.get("added_fields") or []),
        "corrected_fields": list(result.get("corrected_fields") or []),
        "extraction": result.get("extraction") or {},
    }


def raw_prospectus_action_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Rename or delete a raw, not-yet-linked prospectus PDF from the GUI."""

    action = str(payload.get("action") or "").strip()
    if action not in {"rename", "delete_raw"}:
        raise ValueError("raw prospectus action must be rename or delete_raw")
    if not _mapping_bool(payload, "confirm", False):
        raise ValueError("raw prospectus action requires confirm=true")
    lifecycle = _raw_prospectus_lifecycle()
    source_path = str(payload.get("source_path") or "")
    if action == "rename":
        result = lifecycle.rename_pending(source_path, str(payload.get("new_filename") or ""))
        return {"action": action, "old_path": result["old_path"], "new_path": result["new_path"], "raw_deleted": False, "updated_indexes": result.get("updated_indexes", [])}
    return lifecycle.delete_pending(source_path, typed_confirmation=str(payload.get("typed_confirmation") or ""))


def _selected_raw_prospectus_paths(raw_value: Any) -> list[Path] | None:
    if raw_value in (None, ""):
        return None
    if not isinstance(raw_value, list):
        raise ValueError("source_paths must be a list of raw prospectus PDF paths")
    selected: list[Path] = []
    seen: set[Path] = set()
    for value in raw_value:
        path = _resolve_raw_prospectus_path(str(value or ""))
        if path not in seen:
            selected.append(path)
            seen.add(path)
    if not selected:
        raise ValueError("selected extraction requires at least one prospectus")
    return selected


def _sanitize_review_queue_item(item: Mapping[str, Any]) -> dict[str, Any]:
    clean = {key: item.get(key) for key in REVIEW_QUEUE_ITEM_ALLOWED_KEYS if key in item}
    if clean.get("contract_path"):
        clean.update(_contract_summary_for_review_queue(str(clean["contract_path"])))
    for key in ("source_path", "source_file", "contract_path", "review_path"):
        if clean.get(key):
            try:
                clean[key] = _display_path(resolve_project_path(str(clean[key])))
            except ValueError:
                clean[key] = "[REDACTED path outside project]"
    return clean


def _sanitize_review_queue_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_sanitize_review_queue_item(item) for item in items if isinstance(item, Mapping)]


def _resolve_raw_prospectus_path(value: str | Path) -> Path:
    path = resolve_project_path(value)
    raw_root = resolve_project_path(RAW_PROSPECTUS_DIR)
    try:
        path.relative_to(raw_root)
    except ValueError as exc:
        raise ValueError("source_path must be under data/raw/prospectuses") from exc
    if path.suffix.lower() != ".pdf":
        raise ValueError("source_path must be a PDF")
    return path


def _raw_prospectus_is_linked_to_contract(source: Path) -> bool:
    contracts = _contracts_root()
    if not contracts.exists():
        return False
    source_display = _display_path(source)
    contract_reviews: list[Mapping[str, Any]] = []
    for contract_path in contracts.glob("*.json"):
        try:
            raw = json.loads(contract_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        review = raw.get("source_review") if isinstance(raw.get("source_review"), Mapping) else {}
        linked_path = raw.get("source_file") or review.get("raw_prospectus_path")
        if linked_path:
            try:
                if resolve_project_path(str(linked_path)) == source:
                    return True
            except ValueError:
                pass
            if str(linked_path) == source_display:
                return True
        contract_reviews.append(review)

    # Hashing large PDFs is comparatively expensive; defer it until path checks fail.
    source_digest = sha256_file(source) if source.exists() else ""
    return bool(source_digest and any(review.get("raw_prospectus_sha256") == source_digest for review in contract_reviews))


def _rewrite_review_queue_source(old_path: Path, *, new_path: Path | None = None, delete: bool = False) -> bool:
    queue_path = resolve_project_path("data/coverage/review_queue.json")
    if not queue_path.exists():
        return False
    raw = json.loads(queue_path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        return False
    old_display = _display_path(old_path)
    next_items = []
    changed = False
    for item in raw:
        if not isinstance(item, dict):
            next_items.append(item)
            continue
        matches = False
        if item.get("source_path"):
            try:
                matches = resolve_project_path(str(item["source_path"])) == old_path
            except ValueError:
                matches = False
        matches = matches or item.get("source_path") == old_display or item.get("source_filename") == old_path.name
        if matches and delete:
            changed = True
            continue
        if matches and new_path is not None:
            item = dict(item)
            item["source_path"] = _display_path(new_path)
            item["source_filename"] = new_path.name
            item["prospectus_id"] = re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", new_path.stem.lower())).strip("_") or "prospectus"
            changed = True
        next_items.append(item)
    if changed:
        _write_json_atomic(queue_path, next_items)
    return changed


def upload_file_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    kind = str(payload.get("kind") or "").strip()
    if kind not in UPLOAD_KINDS:
        raise ValueError(f"unsupported upload kind: {kind}")
    if not _mapping_bool(payload, "confirm", False):
        raise ValueError("upload requires confirm=true")
    filename = _safe_upload_filename(str(payload.get("filename") or ""))
    suffix = Path(filename).suffix.lower()
    config = UPLOAD_KINDS[kind]
    if suffix not in config["extensions"]:
        allowed = ", ".join(sorted(config["extensions"]))
        raise ValueError(f"{kind} upload must use one of: {allowed}")
    raw_b64 = str(payload.get("content_base64") or "")
    try:
        content = base64.b64decode(raw_b64.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError) as exc:
        raise ValueError("content_base64 is not valid base64") from exc
    if len(content) > MAX_UPLOAD_FILE_BYTES:
        raise ValueError(f"upload file too large; max {MAX_UPLOAD_FILE_BYTES} bytes")
    directory = resolve_project_path(config["directory"])
    directory.mkdir(parents=True, exist_ok=True)
    destination = _write_unique_upload_file(directory, filename, content)
    result: dict[str, Any] = {
        "kind": kind,
        "path": _display_path(destination),
        "filename": destination.name,
        "size_bytes": len(content),
        "sha256": sha256_bytes(content),
        "parse_status": "not_parsed",
        "message": "Uploaded file saved inside the project workspace.",
    }
    source_record: dict[str, Any] | None = None
    try:
        source_record = canonical_store().register_source_file(destination, artifact_kind=kind, canonical_path=_display_path(destination), metadata={"upload_kind": kind})
        result["canonical_source_id"] = source_record["id"]
        result["persisted_to_store"] = True
    except Exception as exc:
        result["persisted_to_store"] = False
        result.setdefault("warnings", []).append(f"canonical store registration failed: {exc}")
    try:
        if kind == "prospectus":
            _register_uploaded_prospectus(result, destination)
        if config.get("parse"):
            _attach_upload_parse_summary(result, kind, destination, payload)
        _sync_uploaded_source_to_selected_contract(result, kind, destination, payload)
    except Exception:
        if source_record:
            try:
                canonical_store().deactivate_source_file(int(source_record["id"]), reason="upload_rollback")
            except Exception:
                pass
        destination.unlink(missing_ok=True)
        raise
    return result


def _register_uploaded_prospectus(result: dict[str, Any], destination: Path) -> None:
    """Persist a raw-PDF upload in the pending extraction indexes used by the inbox UI."""

    digest = str(result.get("sha256") or sha256_file(destination))
    rel = _display_path(destination)
    item = {
        "status": "pending_extraction",
        "review_status": "pending_extraction",
        "source_path": rel,
        "source_filename": destination.name,
        "source_sha256": digest,
        "message": "Uploaded raw prospectus PDF; pending conservative extraction.",
        "uploaded_at": utc_now_iso(),
    }
    result["prospectus_queue_status"] = "pending_extraction"
    result["updated_indexes"] = upsert_pending_prospectus(PROJECT_ROOT, item)
    result["message"] = "Prospectus PDF saved. Open Data → Upload to review extraction status."


def _sync_uploaded_source_to_selected_contract(result: dict[str, Any], kind: str, path: Path, payload: Mapping[str, Any]) -> None:
    """Keep Data Intake uploads synchronized without forcing raw market data onto the active CB.

    Uploads of raw CB/equity/FX histories are source-library events.  They are
    imported and matched by identifier/currency, but are not written to the
    selected CB merely because that CB happened to be active in the GUI.  Only an
    already valuation-ready CSV is a CB-scoped pricing input and can be linked
    directly.
    """

    if kind == "market_data_auto":
        result["sync_status"] = "market_sources_imported"
        result["market_source_matches"] = _market_source_matches_for_upload(path, result)
        detected_types = set(result.get("detected_market_data_types") or [])
        contract_path = str(payload.get("contract_path") or "").strip()
        if "valuation_market_history" in detected_types and contract_path:
            generated = _promote_uploaded_valuation_history(path, contract_path, payload)
            result["sync_status"] = "valuation_history_linked"
            result["source_link"] = generated["source_link"]
            result["generated_market_history_path"] = generated["output_path"]
            result["valuation_history_merge"] = generated["merge_summary"]
            merge_summary = generated["merge_summary"]
            result["message"] = (
                f"{result.get('message', 'Uploaded file saved.')} Added {merge_summary['added_date_count']} new "
                f"date(s) to {generated['output_path']}; {merge_summary['preserved_date_count']} existing date(s) "
                "were kept. Incoming rows update only matching dates."
            )
        else:
            result["message"] = (
                f"{result.get('message', 'Uploaded file saved.')} Imported CB quote, stock, and FX histories as reusable market sources. "
                "When every requested input is ready, choose Build valuation history under Data."
            )
        return

    contract_path = str(payload.get("contract_path") or "").strip()
    if kind != "market_history_csv" or not contract_path:
        return
    generated = _promote_uploaded_valuation_history(path, contract_path, payload)
    result["sync_status"] = "valuation_history_linked"
    result["source_link"] = generated["source_link"]
    result["generated_market_history_path"] = generated["output_path"]
    result["valuation_history_merge"] = generated["merge_summary"]
    merge_summary = generated["merge_summary"]
    result["message"] = (
        f"{result.get('message', 'Uploaded file saved.')} Added {merge_summary['added_date_count']} new date(s) "
        f"and kept {merge_summary['preserved_date_count']} existing date(s) in the active valuation history."
    )



def _market_history_header_key(value: str) -> str:
    normalized = " ".join(str(value or "").strip().lower().replace("_", " ").replace("-", " ").split())
    for canonical, aliases in MARKET_HISTORY_ALIASES.items():
        if normalized in {
            " ".join(alias.strip().lower().replace("_", " ").replace("-", " ").split())
            for alias in aliases
        }:
            return canonical
    return normalized.replace(" ", "_")


def _normalized_market_history_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Read a valuation history without discarding identity/provenance columns."""

    parsed_rows = load_market_history_csv(path)
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError("market history CSV must include a header row")
        raw_rows = [
            dict(row)
            for row in reader
            if not all(not str(value or "").strip() for value in row.values())
        ]
    if len(raw_rows) != len(parsed_rows):
        raise ValueError("market history row normalization did not preserve the parsed row count")

    fieldnames: list[str] = []
    normalized_rows: list[dict[str, str]] = []
    seen_dates: set[str] = set()
    for parsed, raw in zip(parsed_rows, raw_rows):
        normalized_row: dict[str, str] = {}
        for header, value in raw.items():
            if header is None:
                continue
            key = _market_history_header_key(header)
            if not key:
                continue
            if key not in fieldnames:
                fieldnames.append(key)
            text = str(value or "")
            current = normalized_row.get(key, "")
            if current.strip() and text.strip() and current.strip() != text.strip():
                raise ValueError(f"conflicting values supplied for canonical column {key!r}")
            if key not in normalized_row or not current.strip():
                normalized_row[key] = text
        normalized_date = parsed.as_of_date.isoformat()
        if normalized_date in seen_dates:
            raise ValueError(
                f"duplicate market-history date {normalized_date!r} in {_display_path(path)}; "
                "valuation-ready histories require one row per date"
            )
        seen_dates.add(normalized_date)
        normalized_row["date"] = normalized_date
        normalized_rows.append(normalized_row)
    if "date" in fieldnames:
        fieldnames.remove("date")
    fieldnames.insert(0, "date")
    return fieldnames, normalized_rows


def _write_normalized_market_history_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            newline="",
            encoding="utf-8",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        temporary_path.replace(path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _market_history_update_lock(contract_path: str | Path) -> threading.RLock:
    """Return the process-local lock that serializes one CB's history updates."""

    raw_key = str(contract_path).strip()
    try:
        key = str(_resolve_contract_json_path(raw_key).resolve()).casefold()
    except (OSError, ValueError):
        key = raw_key.replace("\\", "/").casefold()
    with _MARKET_HISTORY_UPDATE_LOCKS_GUARD:
        lock = _MARKET_HISTORY_UPDATE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _MARKET_HISTORY_UPDATE_LOCKS[key] = lock
        return lock


def _next_available_market_history_path(path: Path) -> Path:
    """Choose a versioned sibling so committed histories remain immutable."""

    if not path.exists():
        return path
    for index in range(1, 10_000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise ValueError(f"could not allocate a versioned market-history path beside {_display_path(path)}")


def _merge_valuation_market_history_files(
    existing_path: Path | None,
    incoming_path: Path,
    output_path: Path,
) -> dict[str, int]:
    """Merge dated rows; incoming nonblank fields update only the same date."""

    existing_fields: list[str] = []
    existing_rows: list[dict[str, str]] = []
    if existing_path is not None and existing_path.exists():
        existing_fields, existing_rows = _normalized_market_history_csv(existing_path)
    incoming_fields, incoming_rows = _normalized_market_history_csv(incoming_path)

    existing_dates = {row["date"] for row in existing_rows}
    incoming_dates = {row["date"] for row in incoming_rows}
    merged_by_date = {row["date"]: row for row in existing_rows}
    equivalent_field_groups = ({"credit_spread", "credit_spread_bps"},)
    for incoming_row in incoming_rows:
        as_of_date = incoming_row["date"]
        merged_row = dict(merged_by_date.get(as_of_date, {}))
        incoming_nonblank = {
            key for key, value in incoming_row.items() if str(value or "").strip()
        }
        for field_group in equivalent_field_groups:
            supplied_fields = field_group & incoming_nonblank
            if supplied_fields:
                for sibling in field_group - supplied_fields:
                    if sibling in merged_row:
                        merged_row[sibling] = ""
        for key, value in incoming_row.items():
            if key == "date" or str(value or "").strip() or key not in merged_row:
                merged_row[key] = value
        merged_row["date"] = as_of_date
        merged_by_date[as_of_date] = merged_row
    fieldnames = list(dict.fromkeys([*existing_fields, *incoming_fields]))
    if "date" in fieldnames:
        fieldnames.remove("date")
    fieldnames.insert(0, "date")
    merged_rows = [merged_by_date[as_of_date] for as_of_date in sorted(merged_by_date)]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    merged_candidate: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".csv",
            prefix=".merged-market-history-",
            dir=output_path.parent,
            delete=False,
        ) as handle:
            merged_candidate = Path(handle.name)
        _write_normalized_market_history_csv(merged_candidate, fieldnames, merged_rows)
        load_market_history_csv(merged_candidate)
        merged_candidate.replace(output_path)
        merged_candidate = None
    finally:
        if merged_candidate is not None:
            merged_candidate.unlink(missing_ok=True)
    return {
        "existing_date_count": len(existing_dates),
        "incoming_date_count": len(incoming_dates),
        "added_date_count": len(incoming_dates - existing_dates),
        "updated_date_count": len(incoming_dates & existing_dates),
        "preserved_date_count": len(existing_dates - incoming_dates),
        "merged_date_count": len(merged_rows),
    }


def _merge_generated_rows_with_linked_history(
    existing_path: Path,
    rows: list[dict[str, Any]],
    output_path: Path,
) -> tuple[dict[str, int], list[dict[str, str]]]:
    """Write generated rows as an additive update to a linked valuation history."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    candidate_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".csv",
            prefix=".valuation-candidate-",
            dir=output_path.parent,
            delete=False,
        ) as handle:
            candidate_path = Path(handle.name)
        _write_valuation_market_history_csv(candidate_path, rows)
        merge_summary = _merge_valuation_market_history_files(existing_path, candidate_path, output_path)
        _, merged_rows = _normalized_market_history_csv(output_path)
        return merge_summary, merged_rows
    finally:
        if candidate_path is not None:
            candidate_path.unlink(missing_ok=True)


def _linked_market_history_path_for_contract(contract_path: str | Path) -> Path | None:
    contract_display = _display_path(_resolve_contract_json_path(str(contract_path)))
    for item in _load_json_list(resolve_project_path(DEFAULT_UNIVERSE), default=[]):
        if not isinstance(item, Mapping):
            continue
        item_contract = _safe_display_optional_path(item.get("contract_path"))
        if item_contract != contract_display:
            continue
        path_value = str(item.get("market_history_path") or "").strip()
        if not path_value:
            return None
        try:
            linked_path = resolve_project_path(path_value)
        except ValueError:
            return None
        return linked_path if linked_path.exists() else None
    return None


def _promote_uploaded_valuation_history(path: Path, contract_path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Merge an uploaded valuation-ready CSV into the selected CB's dated history."""

    contract = load_contract_json(resolve_project_path(contract_path))
    validation = validate_market_history_file_for_contract(path, contract)
    validation.raise_for_errors()
    suffix = re.sub(r"[^A-Za-z0-9._-]+", "_", path.stem).strip("_") or "uploaded"
    base_output = _valuation_history_output_path(contract.id, f"data/price_history/generated/{contract.id}_{suffix}_valuation_market_history.csv")
    with _market_history_update_lock(contract_path):
        if base_output.exists() and not _mapping_bool(payload, "confirm_overwrite", False):
            raise ValueError(
                f"generated valuation market-history already exists: {_display_path(base_output)}; "
                "pass confirm_overwrite=true to create a new revision"
            )
        output = _next_available_market_history_path(base_output)
        existing_path = _linked_market_history_path_for_contract(contract_path)
        merge_summary = _merge_valuation_market_history_files(existing_path, path, output)
        merged_validation = validate_market_history_file_for_contract(output, contract)
        merged_validation.raise_for_errors()
        link = _link_source_to_universe(
            "generated_market_history",
            output,
            {"contract_path": contract_path, "confirm": True, "confirm_overwrite": True},
        )
        return {
            "output_path": _display_path(output),
            "row_count": merged_validation.row_count,
            "validation": {
                "row_count": merged_validation.row_count,
                "checked_identity_rows": merged_validation.checked_identity_rows,
            },
            "merge_summary": merge_summary,
            "source_link": link,
        }


def _market_source_matches_for_upload(path: Path, result: Mapping[str, Any]) -> dict[str, Any]:
    """Return identifier-based matches for a raw market-data upload without creating CB links."""

    quote_ids: set[str] = set()
    equity_ids: set[str] = set()
    fx_ids: set[str] = set()
    try:
        if _looks_like_mixed_market_data_csv(path):
            quotes, points = _parse_auto_mixed_market_data_csv(path)
        else:
            quotes = load_price_history_file(path) if "cb_quote_history" in set(result.get("detected_market_data_types") or []) else []
            points = load_market_data_file(path) if ({"equity_price_history", "fx_price_history"} & set(result.get("detected_market_data_types") or [])) else []
        quote_ids = {_normalize_identifier(row.instrument_id) for row in quotes if _normalize_identifier(row.instrument_id)}
        for point in points:
            instrument_id = str(getattr(point, "instrument_id", "") or "").strip()
            if getattr(point, "instrument_type", "") == "fx":
                fx_ids.add(instrument_id)
            elif getattr(point, "instrument_type", "") == "equity":
                equity_ids.add(instrument_id)
    except Exception as exc:
        return {"status": "match_failed", "warning": str(exc), "cb_quotes": [], "equities": [], "fx": {"global_scope": True, "instrument_ids": []}}

    universe_items = _load_json_list(resolve_project_path(DEFAULT_UNIVERSE), default=[])
    cb_matches: list[dict[str, Any]] = []
    for quote_id in sorted(quote_ids):
        contracts: set[str] = set()
        for item in universe_items:
            if isinstance(item, Mapping) and quote_id in _universe_row_instrument_ids(item):
                contract_path = _safe_display_optional_path(item.get("contract_path"))
                if contract_path:
                    contracts.add(contract_path)
        for contract_path, raw in _iter_contract_json_files():
            if quote_id in _contract_instrument_ids(raw):
                contracts.add(_display_path(contract_path))
        cb_matches.append({"instrument_id": quote_id, "matched_contract_paths": sorted(contracts)})
    return {
        "status": "matched" if (quote_ids or equity_ids or fx_ids) else "no_identifiers",
        "cb_quotes": cb_matches,
        "equities": [{"instrument_id": value, "matching_rule": "underlying ticker / Bloomberg equity id"} for value in sorted(equity_ids)],
        "fx": {"global_scope": True, "instrument_ids": sorted(fx_ids), "note": "FX histories are currency-pair sources, not CB-linked sources."},
    }


def _contract_market_requirements(contract_path: str | Path) -> dict[str, Any]:
    contract_file = _resolve_contract_json_path(str(contract_path))
    raw = json.loads(contract_file.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("contract JSON must contain an object")
    contract = load_contract_json(contract_file)
    instrument = raw.get("instrument") if isinstance(raw.get("instrument"), Mapping) else {}
    source_review = raw.get("source_review") if isinstance(raw.get("source_review"), Mapping) else {}
    contract_status = str(raw.get("status") or source_review.get("review_status") or "").strip().lower()
    terms_approved = contract_status in {"reviewed", "approved", "complete"}
    canonical_type = str(instrument.get("canonical_id_type") or "").strip().upper()
    cb_id = str(raw.get("isin") or instrument.get("canonical_id") or "").strip()
    if canonical_type == "PENDING_ISIN" or not cb_id or cb_id.upper() == "PENDING_ISIN":
        raise ValueError("contract needs a final ISIN/canonical CB id before market-history generation")
    equity = str(contract.conversion.underlying_ticker or "").strip()
    if not equity:
        raise ValueError("contract is missing conversion.underlying_ticker")
    equity_id = equity if equity.upper().endswith(" EQUITY") else f"{equity} Equity"
    stock_currency = str(contract.stock_currency or "").strip().upper()
    bond_currency = str(contract.settlement_currency or contract.currency or "").strip().upper()
    requires_fx = bool(stock_currency and bond_currency and stock_currency != bond_currency)
    fx_pair = f"{bond_currency}/{stock_currency}" if requires_fx else ""
    fx_match = _fx_instrument_match_for_pair(fx_pair) if fx_pair else {"instrument_id": "", "fx_convention": ""}
    return {
        "contract": contract,
        "contract_path": _display_path(contract_file),
        "contract_id": contract.id,
        "cb_instrument_id": cb_id,
        "equity_instrument_id": equity_id,
        "fx_instrument_id": str(fx_match.get("instrument_id") or ""),
        "fx_pair": fx_pair,
        "stock_currency": stock_currency,
        "bond_price_currency": bond_currency,
        "fx_convention": str(fx_match.get("fx_convention") or ("STOCK_PER_CB" if requires_fx else "")),
        "requires_fx": requires_fx,
        "risk_free_curve_currency": curve_currency_for_contract(contract),
        "contract_status": contract_status,
        "terms_approved": terms_approved,
    }


def _fx_instrument_match_for_pair(pair: str) -> dict[str, str]:
    """Find an FX source for a currency pair, accepting exact or inverse quotes."""

    if not pair:
        return {"instrument_id": "", "fx_convention": "", "matched_pair": ""}
    store = _price_history_store()
    for identity in store.instrument_identities(instrument_type="fx"):
        identity_id = str(identity.get("primary_id") or "")
        for value in (identity.get("primary_id"), identity.get("display_name"), identity.get("instrument_key")):
            match = _fx_match_for_pair(str(value or ""), pair)
            if match:
                return {"instrument_id": identity_id or str(value or ""), **match}
        try:
            aliases = json.loads(str(identity.get("aliases_json") or "[]"))
        except Exception:
            aliases = []
        for alias in aliases if isinstance(aliases, list) else []:
            match = _fx_match_for_pair(str(alias), pair)
            if match:
                return {"instrument_id": identity_id or str(alias), **match}
    index = _load_json_mapping(resolve_project_path(FX_CANONICAL_SOURCES), default={"pairs": {}})
    pairs = index.get("pairs") if isinstance(index.get("pairs"), Mapping) else {}
    for candidate_pair, source in pairs.items():
        match = _fx_match_for_pair(str(candidate_pair), pair)
        if not match or not isinstance(source, Mapping):
            continue
        ids = source.get("instrument_ids")
        return {"instrument_id": str(ids[0]) if isinstance(ids, list) and ids else "", **match}
    return {"instrument_id": "", "fx_convention": "", "matched_pair": ""}


def _fx_instrument_id_for_pair(pair: str) -> str:
    return _fx_instrument_match_for_pair(pair).get("instrument_id", "")


def _fx_match_for_pair(value: str, required_pair: str) -> dict[str, str]:
    candidate = _fx_pair_key(value)
    if not candidate:
        return {}
    if candidate == required_pair:
        return {"matched_pair": candidate, "fx_convention": "STOCK_PER_CB"}
    if _inverse_fx_pair(candidate) == required_pair:
        return {"matched_pair": candidate, "fx_convention": "CB_PER_STOCK"}
    return {}


def _inverse_fx_pair(pair: str) -> str:
    if "/" not in pair:
        return ""
    left, right = pair.split("/", 1)
    return f"{right}/{left}"


def _valuation_history_output_path(contract_id: str, explicit: Any = None) -> Path:
    if explicit:
        output = resolve_project_path(str(explicit))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.resolve().relative_to(PROJECT_ROOT.resolve())
        return output
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", contract_id).strip("_") or "contract"
    return resolve_project_path(f"data/price_history/generated/{safe}_valuation_market_history.csv")


def _write_valuation_market_history_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "date", "stock_price", "bond_price", "market_fx_rate", "stock_currency", "bond_price_currency", "fx_convention",
        "cb_instrument_id", "cb_reference_security", "cb_contract_id", "cb_quote_time", "cb_quote_dealer", "cb_bid_price",
        "cb_ask_price", "cb_selection_reason", "equity_instrument_id", "fx_instrument_id",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _generation_component_payload(*, expected_identifier: str, date_range: Mapping[str, Any], source_files: list[dict[str, Any]], required: bool = True, global_scope: bool = False) -> dict[str, Any]:
    status_payload = _date_range_status(dict(date_range), required=required)
    return {
        **status_payload,
        "expected_identifier": expected_identifier,
        "source_files": source_files,
        "source_file_count": len(source_files),
        "global_scope": global_scope,
    }


def _overlap_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    dates = [str(row.get("date") or row.get("as_of_date") or "") for row in rows if row.get("date") or row.get("as_of_date")]
    return {
        "row_count": len(rows),
        "first_date": min(dates) if dates else "",
        "latest_date": max(dates) if dates else "",
    }


def _latest_traded_yield_payload(
    requirements: Mapping[str, Any],
    store: PriceHistoryStore,
) -> dict[str, Any] | None:
    """Calculate yields as soon as an observed CB quote exists.

    This is intentionally independent of stock/FX readiness: promised cash-flow
    yields need the reviewed bond terms and a CB market price, not the
    convertible valuation inputs.
    """

    quotes = store.selected_daily_quotes(
        instrument_id=str(requirements.get("cb_instrument_id") or ""),
        equity_instrument_id=str(requirements.get("equity_instrument_id") or ""),
        fx_instrument_id=str(requirements.get("fx_instrument_id") or ""),
        fx_convention=str(requirements.get("fx_convention") or ""),
    )
    for quote in quotes:
        raw_mid = quote.get("mid_price")
        try:
            mid_price = float(raw_mid)
        except (TypeError, ValueError):
            bid = quote.get("bid_price")
            ask = quote.get("ask_price")
            try:
                mid_price = (float(bid) + float(ask)) / 2.0
            except (TypeError, ValueError):
                continue
        try:
            as_of_date = date.fromisoformat(str(quote.get("as_of_date") or ""))
        except ValueError:
            continue
        contract = requirements.get("contract")
        if contract is None:
            return None
        calculated = calculate_market_yields(
            contract,
            price=mid_price,
            settlement_date=as_of_date,
            same_day_settlement_assumed=True,
        )
        return {
            "as_of_date": as_of_date.isoformat(),
            "as_of_time": str(quote.get("as_of_time") or ""),
            "dealer": str(quote.get("dealer") or ""),
            "bid_price": quote.get("bid_price"),
            "ask_price": quote.get("ask_price"),
            "mid_price": mid_price,
            "selection_reason": str(quote.get("selection_reason") or ""),
            **calculated,
        }
    return None


def market_generation_readiness_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Explain whether raw imported market sources can generate valuation-ready CSV rows."""

    requirements = _contract_market_requirements(str(payload.get("contract_path") or DEFAULT_CONTRACT))
    store = _price_history_store()
    cb_component = _generation_component_payload(
        expected_identifier=requirements["cb_instrument_id"],
        date_range=store.quote_date_range(instrument_id=requirements["cb_instrument_id"]),
        source_files=store.quote_source_files(instrument_id=requirements["cb_instrument_id"]),
    )
    equity_component = _generation_component_payload(
        expected_identifier=requirements["equity_instrument_id"],
        date_range=store.market_data_date_range(instrument_id=requirements["equity_instrument_id"], instrument_type="equity"),
        source_files=store.market_data_source_files(instrument_id=requirements["equity_instrument_id"], instrument_type="equity"),
    )
    fx_component = _generation_component_payload(
        expected_identifier=requirements["fx_instrument_id"] or requirements["fx_pair"],
        date_range=store.market_data_date_range(instrument_id=requirements["fx_instrument_id"], instrument_type="fx") if requirements["requires_fx"] else {"count": 0, "first_date": "", "latest_date": ""},
        source_files=store.market_data_source_files(instrument_id=requirements["fx_instrument_id"], instrument_type="fx") if requirements["requires_fx"] else [],
        required=bool(requirements["requires_fx"]),
        global_scope=True,
    )
    components = {"cb_quote_history": cb_component, "stock_history": equity_component, "fx_history": fx_component}
    latest_traded_yields = _latest_traded_yield_payload(requirements, store)
    missing = [name for name, component in components.items() if component.get("status") == "missing"]
    rows: list[dict[str, Any]] = []
    if not missing:
        rows = store.build_valuation_market_rows(
            cb_instrument_id=requirements["cb_instrument_id"],
            equity_instrument_id=requirements["equity_instrument_id"],
            cb_contract_id=requirements["contract_id"],
            fx_instrument_id=requirements["fx_instrument_id"],
            stock_currency=requirements["stock_currency"],
            bond_price_currency=requirements["bond_price_currency"],
            fx_convention=requirements["fx_convention"],
        )
    overlap = _overlap_payload(rows)
    linked_path = _linked_market_history_path_for_contract(requirements["contract_path"])
    candidate_dates = {str(row.get("date") or "") for row in rows if row.get("date")}
    linked_dates: set[str] = set()
    linked_warning = ""
    if linked_path is not None:
        try:
            linked_dates = {row.as_of_date.isoformat() for row in load_market_history_csv(linked_path)}
        except Exception as exc:
            linked_warning = str(exc)
    source_mtimes: list[int] = []
    for component in components.values():
        for source_record in (component.get("source_files", []) if isinstance(component, Mapping) else []):
            source_value = str(source_record.get("source_file") or "")
            if not source_value:
                continue
            source_path = Path(source_value).expanduser()
            if not source_path.is_absolute():
                try:
                    source_path = resolve_project_path(source_value)
                except ValueError:
                    continue
            try:
                source_mtimes.append(source_path.stat().st_mtime_ns)
            except OSError:
                continue
    linked_mtime = 0
    if linked_path is not None:
        try:
            linked_mtime = linked_path.stat().st_mtime_ns
        except OSError:
            linked_mtime = 0
    additional_dates = sorted(candidate_dates - linked_dates)
    linked_only_dates = sorted(linked_dates - candidate_dates)
    source_files_newer = bool(source_mtimes and linked_mtime and max(source_mtimes) > linked_mtime)
    linked_history = {
        "path": _display_path(linked_path) if linked_path is not None else "",
        "date_count": len(linked_dates),
        "additional_date_count": len(additional_dates),
        "additional_first_date": additional_dates[0] if additional_dates else "",
        "additional_latest_date": additional_dates[-1] if additional_dates else "",
        "source_gap_date_count": len(linked_only_dates),
        "source_files_newer": source_files_newer,
        "can_update_from_sources": bool(linked_dates and (additional_dates or source_files_newer)),
        "warning": linked_warning,
    }
    market_status = "missing_inputs" if missing else ("ready" if rows else "no_overlap")
    status = market_status if requirements["terms_approved"] else "needs_terms_approval"
    message = (
        "Approve the extracted terms before building the valuation history."
        if status == "needs_terms_approval"
        else "Imported raw market sources are ready to generate valuation-ready CSV rows."
        if market_status == "ready"
        else "A valuation-ready CSV requires imported CB quote history, stock price history, and FX history when currencies differ."
        if market_status == "missing_inputs"
        else "No overlapping dates across imported CB quote, stock, and FX histories."
    )
    return {
        "status": status,
        "contract_path": requirements["contract_path"],
        "contract_id": requirements["contract_id"],
        "requirements": {k: v for k, v in requirements.items() if k != "contract"},
        "components": components,
        "latest_traded_yields": latest_traded_yields,
        "missing": missing,
        "overlap": overlap,
        "linked_history": linked_history,
        "market_status": market_status,
        "terms_approved": requirements["terms_approved"],
        "contract_status": requirements["contract_status"],
        "source_of_truth": _display_path(resolve_project_path(DEFAULT_PRICE_HISTORY_DB)),
        "message": message,
    }


def _persist_generated_valuation_rows_to_canonical_store(requirements: Mapping[str, Any], output: Path, rows: list[dict[str, Any]], link: Mapping[str, Any]) -> dict[str, Any]:
    """Persist generated valuation rows immediately, not only during later bootstrap."""

    store = canonical_store()
    raw_contract = json.loads(_resolve_contract_json_path(str(requirements["contract_path"])).read_text(encoding="utf-8"))
    contract_record = store.upsert_contract(raw_contract, source_path=str(requirements["contract_path"]))
    source_id = link.get("canonical_source_id")
    if not source_id:
        source_id = store.register_source_file(output, artifact_kind="generated_market_history", canonical_path=_display_path(output))["id"]
    cb_key = instrument_key("convertible_bond", "ISIN", str(requirements["cb_instrument_id"]))
    series = store.create_valuation_market_series(
        contract_id=contract_record["contract_id"],
        cb_instrument_key=cb_key,
        equity_instrument_key=instrument_key("equity", "bloomberg", str(requirements.get("equity_instrument_id") or "")) if requirements.get("equity_instrument_id") else "",
        fx_instrument_key=instrument_key("fx", "bloomberg", str(requirements.get("fx_instrument_id") or "")) if requirements.get("fx_instrument_id") else "",
        stock_currency=str(requirements.get("stock_currency") or ""),
        bond_price_currency=str(requirements.get("bond_price_currency") or ""),
        fx_convention=str(requirements.get("fx_convention") or ""),
        selection_policy="latest_joined_market_sources",
        source_file_id=int(source_id),
        metadata={"generated_path": _display_path(output), "source_link": dict(link)},
    )
    canonical_rows = [{**row, "as_of_date": row.get("as_of_date") or row.get("date"), "raw": row} for row in rows]
    store.save_valuation_market_rows(series["id"], canonical_rows)
    return {"canonical_series_id": series["id"], "canonical_series_key": series.get("series_key", ""), "canonical_row_count": len(rows)}


def generate_valuation_market_history_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """GUI API: join raw CB quote, stock, and FX histories into a pricing CSV."""

    if not _mapping_bool(payload, "confirm", False):
        raise ValueError("valuation market-history generation requires confirm=true")
    requirements = _contract_market_requirements(str(payload.get("contract_path") or DEFAULT_CONTRACT))
    store = _price_history_store()
    readiness = market_generation_readiness_payload(payload)
    input_status = {
        "cb_quote_history": str(readiness["components"]["cb_quote_history"]["status"]),
        "stock_history": str(readiness["components"]["stock_history"]["status"]),
        "fx_history": str(readiness["components"]["fx_history"]["status"]),
    }
    if not requirements["terms_approved"]:
        return {
            "status": "needs_terms_approval",
            "input_status": input_status,
            "missing": list(readiness.get("missing") or []),
            "requirements": readiness["requirements"],
            "readiness": readiness,
            "message": "Approve the extracted terms before building the valuation history.",
        }
    missing = list(readiness.get("missing") or [])
    if missing:
        return {
            "status": "missing_inputs",
            "input_status": input_status,
            "missing": missing,
            "requirements": readiness["requirements"],
            "readiness": readiness,
            "message": readiness["message"],
        }
    rows = store.build_valuation_market_rows(
        cb_instrument_id=requirements["cb_instrument_id"],
        equity_instrument_id=requirements["equity_instrument_id"],
        cb_contract_id=requirements["contract_id"],
        fx_instrument_id=requirements["fx_instrument_id"],
        stock_currency=requirements["stock_currency"],
        bond_price_currency=requirements["bond_price_currency"],
        fx_convention=requirements["fx_convention"],
    )
    if not rows:
        return {"status": "no_overlap", "input_status": input_status, "row_count": 0, "requirements": {k: v for k, v in requirements.items() if k != "contract"}, "readiness": readiness, "message": readiness.get("message") or "No overlapping dates across CB quote, stock, and FX histories."}
    requested_output = payload.get("output_path")
    base_output = _valuation_history_output_path(requirements["cb_instrument_id"], requested_output)
    with _market_history_update_lock(requirements["contract_path"]):
        linked_history_path = _linked_market_history_path_for_contract(requirements["contract_path"])
        output = base_output
        if output.exists():
            if not _mapping_bool(payload, "confirm_overwrite", False):
                raise ValueError(
                    f"output already exists: {_display_path(output)}; "
                    "pass confirm_overwrite=true to create a new revision"
                )
            output = _next_available_market_history_path(output)
        preserve_linked_history = _mapping_bool(
            payload,
            "preserve_linked_history",
            linked_history_path is not None,
        )
        existing_path = linked_history_path if preserve_linked_history else None
        persisted_rows: list[dict[str, Any]] = rows
        merge_summary: dict[str, int] | None = None
        if existing_path is not None:
            merge_summary, merged_rows = _merge_generated_rows_with_linked_history(existing_path, rows, output)
            persisted_rows = list(merged_rows)
        else:
            _write_valuation_market_history_csv(output, rows)
        validation = validate_market_history_file_for_contract(output, requirements["contract"])
        validation.raise_for_errors()
        provisional_link = {
            "kind": "generated_market_history",
            "source_path": _display_path(output),
            "contract_path": str(requirements["contract_path"]),
            "linked_field": "market_history_path",
        }
        canonical_persist = _persist_generated_valuation_rows_to_canonical_store(
            requirements,
            output,
            persisted_rows,
            provisional_link,
        )
        link = _link_source_to_universe(
            "generated_market_history",
            output,
            {"contract_path": requirements["contract_path"], "confirm": True, "confirm_overwrite": True},
        )
        return {
            "status": "ready",
            "output_path": _display_path(output),
            "row_count": validation.row_count,
            "input_status": input_status,
            "requirements": {k: v for k, v in requirements.items() if k != "contract"},
            "source_link": link,
            "canonical_persist": canonical_persist,
            "readiness": readiness,
            "canonical_series_id": canonical_persist.get("canonical_series_id"),
            "merge_summary": merge_summary,
            "validation": {"row_count": validation.row_count, "checked_identity_rows": validation.checked_identity_rows},
            "message": (
                "Updated and linked the valuation-ready market history while preserving its existing dates."
                if merge_summary is not None
                else "Generated and linked valuation-ready market-history CSV from CB quote, stock, and FX histories."
            ),
        }


def _attach_upload_parse_summary(result: dict[str, Any], kind: str, path: Path, payload: Mapping[str, Any]) -> None:
    if kind == "market_data_auto":
        _attach_auto_market_data_parse_summary(result, path, payload)
    elif kind == "raw_price_history":
        rows = load_price_history_file(path, contract_id=str(payload.get("contract_id") or ""))
        batch = _price_history_store().import_file(path, contract_id=str(payload.get("contract_id") or ""), notes="GUI Data Upload raw CB quote history")
        result.update({
            "parse_status": "ok",
            "row_count": len(rows),
            "database_import": _price_history_import_summary(batch, quote_count=len(rows), market_data_count=0),
            "message": "Raw CB prices imported. Build the valuation history after the required stock and FX prices are ready.",
        })
    elif kind == "market_data_history":
        rows = load_market_data_file(path)
        batch = _price_history_store().import_market_data_file(path, notes="GUI Data Upload equity/FX market data")
        fx_sources = _register_fx_canonical_sources(path, rows, result.get("sha256", ""), confirm_overwrite=_mapping_bool(payload, "confirm_fx_overwrite", False))
        result.update({
            "parse_status": "ok",
            "row_count": len(rows),
            "database_import": _price_history_import_summary(batch, quote_count=0, market_data_count=len(rows)),
            "fx_canonical_sources": fx_sources,
            "message": "Equity/FX market data imported into the database. FX canonical sources are one-per-currency-pair; existing canonical sources are preserved unless overwrite is confirmed.",
        })
    elif kind == "market_history_csv":
        contract_path = str(payload.get("contract_path") or DEFAULT_CONTRACT)
        contract = load_contract_json(resolve_project_path(contract_path))
        validation = validate_market_history_file_for_contract(path, contract)
        validation.raise_for_errors()
        rows = load_market_history_csv(path)
        result.update({"parse_status": "ok", "row_count": len(rows), "validation": {"row_count": validation.row_count, "checked_identity_rows": validation.checked_identity_rows}, "message": "Valuation-ready market history validated for the selected contract."})


def _attach_auto_market_data_parse_summary(result: dict[str, Any], path: Path, payload: Mapping[str, Any]) -> None:
    """Detect and import a market-data upload without asking the PM to classify it."""

    detected: list[str] = []
    # Parser failures are expected while probing alternate supported layouts.
    # Keep them for a useful error when nothing matches, but do not show a
    # successful CB upload a scary "equity/FX detection skipped" warning (or
    # vice versa).
    detection_failures: list[str] = []
    breakdown: dict[str, int] = {
        "cb_quote_rows": 0,
        "equity_points": 0,
        "fx_points": 0,
        "other_market_data_points": 0,
        "valuation_rows": 0,
    }
    database_imports: list[dict[str, Any]] = []
    total_rows = 0
    # Auto-detected market files are reusable library sources. Never stamp
    # them with whichever CB happened to be selected during upload; the exact
    # quote ISIN is matched to a contract later.
    contract_id = ""
    prefer_mixed_csv = _looks_like_mixed_market_data_csv(path)

    if not prefer_mixed_csv:
        try:
            quote_rows = load_price_history_file(path, contract_id=contract_id)
        except Exception as exc:
            detection_failures.append(f"CB quote detection skipped: {exc}")
            quote_rows = []
    else:
        quote_rows = []
    if quote_rows:
        batch = _price_history_store().import_file(path, contract_id=contract_id, notes="GUI Data Upload auto-detected CB quote history")
        detected.append("cb_quote_history")
        breakdown["cb_quote_rows"] = len(quote_rows)
        total_rows += len(quote_rows)
        database_imports.append(_price_history_import_summary(batch, quote_count=len(quote_rows), market_data_count=0))

    if not prefer_mixed_csv:
        try:
            market_points = load_market_data_file(path)
        except Exception as exc:
            detection_failures.append(f"Equity/FX detection skipped: {exc}")
            market_points = []
    else:
        market_points = []
    if market_points:
        batch = _price_history_store().import_market_data_file(path, notes="GUI Data Upload auto-detected equity/FX market data")
        counts_by_type: dict[str, int] = {}
        for point in market_points:
            point_type = str(getattr(point, "instrument_type", "") or "unknown")
            counts_by_type[point_type] = counts_by_type.get(point_type, 0) + 1
        if counts_by_type.get("equity", 0):
            detected.append("equity_price_history")
        if counts_by_type.get("fx", 0):
            detected.append("fx_price_history")
        unknown_count = sum(count for key, count in counts_by_type.items() if key not in {"equity", "fx"})
        if unknown_count:
            detected.append("other_market_data")
        breakdown["equity_points"] = counts_by_type.get("equity", 0)
        breakdown["fx_points"] = counts_by_type.get("fx", 0)
        breakdown["other_market_data_points"] = unknown_count
        total_rows += len(market_points)
        fx_sources = _register_fx_canonical_sources(path, market_points, result.get("sha256", ""), confirm_overwrite=_mapping_bool(payload, "confirm_fx_overwrite", False))
        if fx_sources:
            result["fx_canonical_sources"] = fx_sources
        database_imports.append(_price_history_import_summary(batch, quote_count=0, market_data_count=len(market_points)))

    if not quote_rows and not market_points:
        try:
            mixed_quotes, mixed_points = _parse_auto_mixed_market_data_csv(path, contract_id=contract_id)
        except Exception as exc:
            detection_failures.append(f"Mixed market-data detection skipped: {exc}")
            mixed_quotes, mixed_points = [], []
        if mixed_quotes or mixed_points:
            batch = _price_history_store().import_detected_rows(
                path,
                quote_rows=mixed_quotes,
                market_data_points=mixed_points,
                notes="GUI Data Upload auto-detected mixed CB/equity/FX market data",
            )
            counts_by_type: dict[str, int] = {}
            for point in mixed_points:
                point_type = str(getattr(point, "instrument_type", "") or "unknown")
                counts_by_type[point_type] = counts_by_type.get(point_type, 0) + 1
            if mixed_quotes:
                detected.append("cb_quote_history")
            if counts_by_type.get("equity", 0):
                detected.append("equity_price_history")
            if counts_by_type.get("fx", 0):
                detected.append("fx_price_history")
            unknown_count = sum(count for key, count in counts_by_type.items() if key not in {"equity", "fx"})
            if unknown_count:
                detected.append("other_market_data")
            breakdown["cb_quote_rows"] = len(mixed_quotes)
            breakdown["equity_points"] = counts_by_type.get("equity", 0)
            breakdown["fx_points"] = counts_by_type.get("fx", 0)
            breakdown["other_market_data_points"] = unknown_count
            total_rows += len(mixed_quotes) + len(mixed_points)
            fx_sources = _register_fx_canonical_sources(path, mixed_points, result.get("sha256", ""), confirm_overwrite=_mapping_bool(payload, "confirm_fx_overwrite", False))
            if fx_sources:
                result["fx_canonical_sources"] = fx_sources
            database_imports.append(_price_history_import_summary(batch, quote_count=len(mixed_quotes), market_data_count=len(mixed_points)))

    if path.suffix.lower() == ".csv":
        contract_path = str(payload.get("contract_path") or "").strip()
        default_contract = resolve_project_path(DEFAULT_CONTRACT)
        if not contract_path and not default_contract.exists():
            valuation_rows = []
            validation = None
        else:
            try:
                contract = load_contract_json(resolve_project_path(contract_path or DEFAULT_CONTRACT))
                validation = validate_market_history_file_for_contract(path, contract)
                validation.raise_for_errors()
                valuation_rows = load_market_history_csv(path)
            except Exception as exc:
                detection_failures.append(f"Valuation-ready market-history detection skipped: {exc}")
                valuation_rows = []
                validation = None
        if valuation_rows:
            detected.append("valuation_market_history")
            breakdown["valuation_rows"] = len(valuation_rows)
            total_rows += len(valuation_rows)
            result["validation"] = {"row_count": validation.row_count, "checked_identity_rows": validation.checked_identity_rows} if validation else {"row_count": len(valuation_rows), "checked_identity_rows": 0}

    if not detected:
        detail = "; ".join(detection_failures[-3:]) if detection_failures else "No supported CB, equity, FX, or valuation-ready market-data layout was found."
        raise ValueError(f"Could not determine market-data type automatically. {detail}")

    unique_detected = list(dict.fromkeys(detected))
    result.update({
        "parse_status": "ok",
        "detected_market_data_types": unique_detected,
        "market_data_breakdown": breakdown,
        "row_count": total_rows,
        "database_imports": database_imports,
        "database_import": database_imports[0] if database_imports else None,
        "input_normalization": {
            "status": "normalized",
            "representation": "canonical long-form records",
            "date_format": "YYYY-MM-DD",
            "blank_rows": "ignored",
            "numeric_cells": "parsed from numbers and formatted numeric text",
            "source_file_preserved": True,
        },
        "warnings": [],
        "message": "Market-data upload classified automatically as " + ", ".join(label.replace("_", " ") for label in unique_detected) + ". Source formatting was normalized internally; the uploaded workbook was preserved unchanged.",
    })


def _looks_like_mixed_market_data_csv(path: Path) -> bool:
    if path.suffix.lower() != ".csv":
        return False
    try:
        with path.open("r", newline="", encoding="utf-8-sig") as handle:
            reader = csv.reader(handle)
            headers = next(reader, [])
    except Exception:
        return False
    normalized = {_normalize_upload_header(header) for header in headers}
    return {"instrument type", "date", "instrument id"}.issubset(normalized)


def _parse_auto_mixed_market_data_csv(path: Path, *, contract_id: str = "") -> tuple[list[PriceQuoteRow], list[MarketDataPoint]]:
    if path.suffix.lower() != ".csv":
        return [], []
    quotes: list[PriceQuoteRow] = []
    points: list[MarketDataPoint] = []
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            return [], []
        normalized_headers = {_normalize_upload_header(name): name for name in reader.fieldnames}
        if "instrument type" not in normalized_headers or "date" not in normalized_headers or "instrument id" not in normalized_headers:
            return [], []
        for source_row, raw in enumerate(reader, start=2):
            if all(str(value or "").strip() == "" for value in raw.values()):
                continue
            row_type = _csv_value(raw, normalized_headers, "instrument type").lower().replace(" ", "_")
            instrument_id = _csv_value(raw, normalized_headers, "instrument id")
            as_of_date = date.fromisoformat(_csv_value(raw, normalized_headers, "date"))
            if row_type in {"cb", "convertible", "convertible_bond", "bond"}:
                bid = _optional_upload_float(_csv_value(raw, normalized_headers, "bid"))
                ask = _optional_upload_float(_csv_value(raw, normalized_headers, "ask"))
                mid = _optional_upload_float(_csv_value(raw, normalized_headers, "mid") or _csv_value(raw, normalized_headers, "value"))
                if mid is None and bid is not None and ask is not None:
                    mid = (bid + ask) / 2.0
                if bid is None and ask is None and mid is None:
                    continue
                reference_security = _csv_value(raw, normalized_headers, "reference security") or instrument_id
                quotes.append(
                    PriceQuoteRow(
                        reference_security=reference_security,
                        as_of_date=as_of_date,
                        bid_price=bid,
                        ask_price=ask,
                        mid_price=mid,
                        price_currency=_csv_value(raw, normalized_headers, "currency").upper(),
                        instrument_id=instrument_id or reference_security,
                        contract_id=contract_id or _csv_value(raw, normalized_headers, "contract id"),
                        source_file=str(path),
                        source_sheet="mixed_csv",
                        source_row=source_row,
                    )
                )
            elif row_type in {"equity", "stock", "fx", "currency"}:
                instrument_type = "fx" if row_type in {"fx", "currency"} else "equity"
                points.append(
                    MarketDataPoint(
                        instrument_id=instrument_id,
                        instrument_type=instrument_type,
                        as_of_date=as_of_date,
                        field=_csv_value(raw, normalized_headers, "field") or "PX_LAST",
                        value=_required_upload_float(_csv_value(raw, normalized_headers, "value"), source_row),
                        source_file=str(path),
                        source_sheet="mixed_csv",
                        source_row=source_row,
                    )
                )
    return quotes, points


def _normalize_upload_header(value: str) -> str:
    return " ".join(str(value or "").strip().lower().replace("_", " ").split())


def _csv_value(raw: Mapping[str, Any], headers: Mapping[str, str], key: str) -> str:
    header = headers.get(key)
    if not header:
        return ""
    value = raw.get(header, "")
    return "" if value is None else str(value).strip()


def _optional_upload_float(value: str) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    return float(text.replace(",", ""))


def _required_upload_float(value: str, source_row: int) -> float:
    parsed = _optional_upload_float(value)
    if parsed is None:
        raise ValueError(f"missing numeric value on source row {source_row}")
    return parsed


def _price_history_store() -> PriceHistoryStore:
    return PriceHistoryStore(resolve_project_path(DEFAULT_PRICE_HISTORY_DB))


def _price_history_import_summary(batch: Any, *, quote_count: int, market_data_count: int) -> dict[str, Any]:
    return {
        "database_path": DEFAULT_PRICE_HISTORY_DB,
        "batch_id": batch.id,
        "source_file": _safe_display_optional_path(batch.source_file),
        "source_sha256": batch.source_sha256,
        "row_count": batch.row_count,
        "quote_count": quote_count,
        "market_data_count": market_data_count,
        "imported_at": batch.imported_at,
    }


def _register_fx_canonical_sources(path: Path, rows: list[Any], digest: str, *, confirm_overwrite: bool = False) -> list[dict[str, Any]]:
    pairs: dict[str, dict[str, Any]] = {}
    for row in rows:
        if getattr(row, "instrument_type", "") != "fx":
            continue
        pair = _fx_pair_key(getattr(row, "instrument_id", ""))
        if not pair:
            continue
        entry = pairs.setdefault(pair, {"pair": pair, "instrument_ids": set(), "row_count": 0})
        entry["instrument_ids"].add(str(getattr(row, "instrument_id", "")))
        entry["row_count"] += 1
    if not pairs:
        return []
    index_path = resolve_project_path(FX_CANONICAL_SOURCES)
    index = _load_json_mapping(index_path, default={"version": 1, "pairs": {}})
    existing_pairs = index.setdefault("pairs", {})
    if not isinstance(existing_pairs, dict):
        existing_pairs = {}
        index["pairs"] = existing_pairs
    rel = _display_path(path)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    results: list[dict[str, Any]] = []
    changed = False
    for pair, info in sorted(pairs.items()):
        current = existing_pairs.get(pair) if isinstance(existing_pairs.get(pair), Mapping) else None
        status = "registered_canonical"
        if current and current.get("source_path") and current.get("source_path") != rel and not confirm_overwrite:
            status = "existing_canonical_preserved"
            results.append({
                "pair": pair,
                "status": status,
                "source_path": current.get("source_path", ""),
                "uploaded_source_path": rel,
                "instrument_ids": sorted(info["instrument_ids"]),
                "row_count": info["row_count"],
            })
            continue
        if current and current.get("source_path") == rel:
            status = "canonical_refreshed"
        elif current and confirm_overwrite:
            status = "canonical_overwritten"
        existing_pairs[pair] = {
            "pair": pair,
            "source_path": rel,
            "source_sha256": digest,
            "instrument_ids": sorted(info["instrument_ids"]),
            "row_count": info["row_count"],
            "updated_at": now,
        }
        changed = True
        results.append({**existing_pairs[pair], "status": status})
    if changed:
        _write_json_atomic(index_path, index)
    return results


def _fx_pair_key(instrument_id: str) -> str:
    text = str(instrument_id or "").strip().upper()
    direct = re.fullmatch(r"([A-Z]{3})\s*/\s*([A-Z]{3})", text)
    if direct:
        return f"{direct.group(1)}/{direct.group(2)}"
    match = re.search(r"\b([A-Z]{6})(?:\s+CURNCY|\s+CUR|\b)", text)
    if not match:
        return ""
    token = match.group(1)
    return f"{token[:3]}/{token[3:]}"


def _safe_upload_filename(value: str) -> str:
    name = Path(value).name.strip()
    if not name or name in {".", ".."}:
        raise ValueError("upload filename required")
    safe = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")
    if not safe or safe in {".", ".."}:
        raise ValueError("upload filename is not usable")
    return safe


def _write_unique_upload_file(directory: Path, filename: str, content: bytes) -> Path:
    directory.resolve().relative_to(PROJECT_ROOT.resolve())
    stem, suffix = Path(filename).stem, Path(filename).suffix
    for index in range(1000):
        candidate = directory / (filename if index == 0 else f"{stem}-{index}{suffix}")
        candidate = candidate.resolve()
        candidate.relative_to(PROJECT_ROOT.resolve())
        try:
            with candidate.open("xb") as handle:
                handle.write(content)
            return candidate
        except FileExistsError:
            continue
    raise ValueError("could not allocate unique upload filename")


def _batch_yield_summary(
    contract: Any,
    results: list[ResultRow],
    issue_yield_validation: Mapping[str, Any],
) -> dict[str, Any]:
    latest_yield_row = next(
        (row for row in reversed(results) if row.yield_to_maturity is not None),
        None,
    )
    issue_ytm_check = issue_yield_validation.get("yield_to_maturity")
    issue_ytm_check = issue_ytm_check if isinstance(issue_ytm_check, Mapping) else {}
    return {
        "latest_yield_to_maturity": (
            latest_yield_row.yield_to_maturity if latest_yield_row else None
        ),
        "latest_yield_to_put": (
            latest_yield_row.yield_to_put if latest_yield_row else None
        ),
        "latest_yield_to_put_date": (
            latest_yield_row.yield_to_put_date.isoformat()
            if latest_yield_row and latest_yield_row.yield_to_put_date
            else None
        ),
        "quoted_issue_yield_to_maturity": contract.yield_to_maturity,
        "calculated_issue_yield_to_maturity": issue_ytm_check.get(
            "calculated_yield"
        ),
        "issue_yield_difference_bps": issue_ytm_check.get("difference_bps"),
        "issue_yield_status": issue_ytm_check.get("status", "unavailable"),
    }


def _result_to_api_row(row: ResultRow) -> dict[str, Any]:
    return {
        "date": row.as_of_date.isoformat(),
        "stock_price": row.stock_price,
        "bond_price": row.bond_price,
        "market_fx_rate": row.market_fx_rate,
        "fair_value": row.fair_value,
        "parity": row.parity,
        "bond_floor": row.bond_floor,
        "cheapness": row.cheapness,
        "implied_volatility": row.implied_volatility,
        "yield_to_maturity": row.yield_to_maturity,
        "yield_to_put": row.yield_to_put,
        "yield_to_put_date": (
            row.yield_to_put_date.isoformat() if row.yield_to_put_date else None
        ),
        "yield_accrued_interest": row.yield_accrued_interest,
        "yield_dirty_price": row.yield_dirty_price,
        "yield_price_basis": row.yield_price_basis,
        "yield_warning": row.yield_warning,
        "output_currency": row.output_currency,
        "warnings": row.warnings,
        "error": row.error,
        "assumption_source": row.assumption_source,
        "volatility": row.volatility,
        "risk_free_rate": row.risk_free_rate,
        "credit_spread": row.credit_spread,
        "borrow_rate": row.borrow_rate,
        "dividend_yield": row.dividend_yield,
        "steps": row.steps,
        "model_version": row.model_version,
    }


def render_dashboard_html() -> str:
    """Return a self-contained browser UI that draws SVG charts client-side."""

    metric_views_json = dumps_json(METRIC_GROUPS)
    supported_yield_curve_currencies_json = dumps_json(SUPPORTED_YIELD_CURVE_CURRENCIES)
    yield_curve_options_html = "".join(
        f'<option value="{currency}">{currency} yield curve</option>'
        for currency in SUPPORTED_YIELD_CURVE_CURRENCIES
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CB Terminal</title>
  <style>
    :root {{
      color-scheme:dark;
      --bg:#000; --surface:#050505; --panel:#080808; --panel-strong:#171717; --ink:#f2f2f2; --muted:#a0a0a0; --accent:#ff9d00; --accent-strong:#ffb000; --warning:#ffd000; --bad:#ff5c5c; --good:#65dc8c; --line:#383838; --axes:#8a8a8a; --grid:#202020;
      --text-xs:clamp(.7rem, .68rem + .05vw, .75rem);
      --text-sm:clamp(.75rem, .72rem + .08vw, .8rem);
      --text-base:clamp(.8rem, .77rem + .1vw, .86rem);
      --text-md:clamp(.86rem, .82rem + .12vw, .94rem);
      --text-lg:clamp(.95rem, .9rem + .16vw, 1.05rem);
      --text-xl:clamp(1.05rem, 1rem + .2vw, 1.16rem);
      --text-command:clamp(.86rem, .82rem + .14vw, .98rem);
      --text-security-label:clamp(1.125rem, 1.08rem + .12vw, 1.2rem);
      --text-security-command:clamp(1.29rem, 1.23rem + .21vw, 1.47rem);
      --page-gutter:clamp(5px, .7vw, 10px);
      --panel-pad:clamp(6px, .65vw, 9px);
      --layout-gap:clamp(4px, .55vw, 7px);
      --radius:0px;
      --mono:Consolas, "Lucida Console", "Courier New", monospace;
    }}
    * {{ box-sizing: border-box; }}
    html {{ font-size:16px; text-size-adjust:100%; -webkit-text-size-adjust:100%; }}
    body {{ margin:0; min-width:0; overflow-x:hidden; font-family:var(--mono); font-variant-numeric:tabular-nums; background:var(--bg); color:var(--ink); font-size:var(--text-base); line-height:1.3; }}
    button, input, select, textarea {{ font:inherit; }}
    header {{ min-height:30px; padding:3px var(--page-gutter); border-bottom:1px solid #565656; display:flex; align-items:center; justify-content:space-between; gap:8px; min-width:0; background:#111; }}
    header > div:first-child {{ min-width:0; }}
    .brand-lockup {{ display:flex; align-items:center; gap:7px; }}
    .terminal-badge {{ border-left:1px solid #555; padding:1px 0 1px 8px; color:#bdbdbd; background:transparent; font-size:var(--text-xs); line-height:1.2; white-space:nowrap; }}
    .eyebrow {{ display:block; color:var(--accent); font-size:var(--text-xs); font-weight:700; letter-spacing:.04em; text-transform:uppercase; }}
    h1 {{ margin:0; padding:3px 6px; color:#000; background:var(--accent); font-size:var(--text-sm); line-height:1; font-weight:800; letter-spacing:.04em; }}
    h2 {{ margin:0 0 5px; padding-bottom:3px; border-bottom:1px solid #4a4a4a; color:var(--accent); font-size:var(--text-lg); line-height:1.2; font-weight:700; text-transform:uppercase; }}
    h3 {{ margin:0 0 4px; color:var(--accent); font-size:var(--text-md); line-height:1.2; font-weight:700; text-transform:uppercase; }}
    p {{ margin:3px 0; color:var(--muted); line-height:1.3; }}
    main.workbench-layout {{ width:100%; margin:0; padding:var(--layout-gap) var(--page-gutter) 18px; display:grid; grid-template-columns:minmax(0,1fr); gap:var(--layout-gap); align-items:start; }}
    .panel {{ min-width:0; background:var(--panel); border:1px solid var(--line); border-radius:0; padding:var(--panel-pad); }}
    .controls-panel {{ max-width:980px; }}
    .plots-panel {{ display:grid; gap:var(--layout-gap); min-width:0; }}
    .instrument-nav {{ position:relative; border-bottom:1px solid #565656; padding:4px var(--page-gutter); display:grid; grid-template-columns:minmax(0,1fr); gap:4px; align-items:stretch; background:#000; font-size:var(--text-sm); }}
    .command-shell {{ position:relative; display:flex; align-items:center; gap:10.5px; width:100%; margin:0; min-height:45px; border:1px solid #555; border-radius:0; background:#000; padding:3px 7.5px; }}
    .command-shell:focus-within {{ border-color:var(--accent); }}
    .command-prompt {{ min-width:99px; padding:6px 9px; color:#000; background:var(--accent); font-family:var(--mono); font-size:var(--text-security-label); font-weight:800; letter-spacing:.04em; white-space:nowrap; }}
    .command-input-wrap {{ position:relative; flex:1; min-width:0; }}
    .instrument-nav input {{ position:relative; z-index:1; width:100%; font-family:var(--mono); text-transform:none; border:0; background:transparent; color:#fff; padding:6px 0; font-size:var(--text-security-command); line-height:1.15; outline:none; caret-color:var(--accent); }}
    .instrument-nav input::placeholder {{ color:#777; opacity:1; }}
    .command-input-ghost {{ position:absolute; inset:6px 0 auto 0; z-index:0; font-family:var(--mono); font-size:var(--text-security-command); line-height:1.15; color:#777; pointer-events:none; white-space:pre; overflow:hidden; }}
    .command-ghost-prefix {{ color:transparent; }}
    .command-autocomplete {{ position:absolute; left:var(--page-gutter); right:var(--page-gutter); top:calc(100% - 1px); z-index:20; border:1px solid #666; border-radius:0; background:#000; max-height:min(42vh, 320px); overflow:auto; display:none; }}
    .command-autocomplete.active {{ display:block; }}
    .command-suggestion {{ display:grid; grid-template-columns:minmax(180px, 1fr) minmax(110px, .55fr) minmax(90px, .4fr); gap:8px; padding:5px 7px; border-bottom:1px solid #242424; cursor:pointer; }}
    .command-suggestion:hover, .command-suggestion.active {{ background:#151200; color:#ffd43b; }}
    .command-suggestion strong {{ color:#f6f1d0; }}
    .command-suggestion.active strong {{ color:#ffd43b; }}
    .instrument-context {{ width:100%; margin:0; display:grid; grid-template-columns:minmax(0,1fr) auto; gap:4px; align-items:stretch; }}
    .selected-identity {{ border:1px solid #333; border-radius:0; min-height:26px; padding:4px 6px; color:#f5f5f5; background:#050505; font-size:var(--text-xs); overflow-wrap:anywhere; }}
    .selected-identity-title {{ display:flex; align-items:center; gap:7px; flex-wrap:wrap; }}
    .readiness-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(min(145px,100%),1fr)); gap:2px; margin-top:3px; }}
    .readiness-card {{ border-left:2px solid var(--accent); background:#080808; padding:3px 5px; color:#d8d8d8; }}
    .readiness-card b {{ display:block; color:var(--accent); font-size:var(--text-xs); text-transform:uppercase; letter-spacing:.03em; }}
    .readiness-card span {{ color:#b0b0b0; font-size:var(--text-xs); }}
    .context-actions {{ display:flex; gap:3px; }}
    .context-actions button {{ min-width:110px; }}
    .active-assumptions-strip {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(min(130px,100%),1fr)); gap:3px; margin-top:5px; }}
    .assumption-chip {{ border:1px solid #333; background:#000; padding:4px 5px; font-size:var(--text-xs); }}
    .assumption-chip b {{ display:block; color:#fff; margin-top:3px; font-size:var(--text-base); }}
    .hidden-select {{ display:none; }}
    .output-panel, .plot-panel {{ padding:var(--panel-pad); }}
    form {{ display:grid; gap:5px; }}
    .assumption-grid, .advanced-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(min(150px,100%),1fr)); gap:4px; }}
    label {{ display:grid; gap:2px; color:var(--accent); font-size:var(--text-xs); }}
    select, input, textarea {{ width:100%; padding:5px 6px; border:1px solid #555; border-radius:0; background:#030303; color:var(--ink); font-size:var(--text-base); line-height:1.2; }}
    select:focus, input:focus, textarea:focus {{ border-color:var(--accent); outline:2px solid transparent; }}
    input[type="checkbox"] {{ width:auto; }}
    .check-row {{ display:flex; align-items:center; gap:8px; }}
    .button-row {{ display:grid; grid-template-columns:1fr; gap:4px; }}
    button {{ padding:5px 8px; border:1px solid #555; border-radius:0; background:#161616; color:#e8e8e8; font-size:var(--text-sm); line-height:1.1; font-weight:700; cursor:pointer; text-transform:uppercase; }}
    button:hover {{ border-color:var(--accent); background:#211600; color:#fff; }}
    button:focus-visible, summary:focus-visible {{ outline:2px solid var(--accent); outline-offset:2px; }}
    button:disabled {{ cursor:not-allowed; opacity:.5; }}
    details {{ border:1px solid #444; border-radius:0; padding:5px; }}
    summary {{ cursor:pointer; color:var(--muted); font-size:var(--text-sm); }}
    .metric-block {{ border-top:1px solid #222; padding-top:10px; }}
    .metric-toggles {{ display:grid; grid-template-columns:1fr; gap:6px; margin-top:6px; }}
    .metric-toggles label {{ display:flex; align-items:center; gap:6px; padding:4px 0; border:0; background:transparent; }}
    .kpis {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(min(135px,100%),1fr)); gap:2px; margin-top:4px; }}
    .kpi {{ border:0; border-right:1px solid #2f2f2f; border-bottom:1px solid #222; padding:3px 5px; background:#000; min-width:0; }}
    .kpi span {{ color:var(--accent); font-size:var(--text-xs); }}
    .kpi b {{ display:block; font-size:var(--text-md); line-height:1.1; margin-top:1px; color:#fff; overflow:hidden; text-overflow:ellipsis; }}
    svg.matlab-plot {{ width:100%; height:clamp(190px, 21vw, 260px); min-height:0; background:#000; border:1px solid #555; display:block; }}
    svg.matlab-plot.small-plot {{ height:clamp(150px, 16vw, 200px); }}
    svg.matlab-plot.pm-plot {{ height:clamp(180px, 18vw, 230px); }}
    svg.matlab-plot.pm-small-plot {{ height:clamp(140px, 14vw, 180px); }}
    .chart-note {{ margin:2px 0 5px; font-size:var(--text-xs); color:var(--muted); }}
    .chart-gesture-hint {{ color:#8a8a8a; font-size:var(--text-xs); line-height:1.4; }}
    .small-multiple-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(min(390px,100%),1fr)); gap:5px; }}
    .chart-actions {{ display:flex; justify-content:space-between; align-items:center; gap:7px; margin-bottom:4px; }}
    .subtab-bar {{ display:flex; gap:2px; flex-wrap:nowrap; margin:5px 0 7px; border-bottom:1px solid #555; padding-bottom:2px; overflow-x:auto; overscroll-behavior-inline:contain; scrollbar-width:thin; }}
    .subtab-button {{ flex:0 0 auto; padding:5px 8px; border:1px solid #555; background:#151515; color:#c6c6c6; font-size:var(--text-sm); letter-spacing:.01em; }}
    .subtab-button.active {{ color:#000; background:var(--accent); border-color:var(--accent); }}
    .subtab-panel {{ display:none; min-width:0; max-height:74vh; max-height:74dvh; overflow:auto; }}
    .subtab-panel.active {{ display:block; }}
    .intake-toolbar {{ position:sticky; top:0; z-index:2; background:var(--panel); border-bottom:1px solid var(--line); padding-bottom:5px; margin-bottom:6px; }}
    .intake-toolbar-header {{ display:flex; align-items:flex-start; justify-content:space-between; gap:5px; flex-wrap:wrap; }}
    .intake-toolbar-title {{ flex:1 1 260px; min-width:220px; }}
    .intake-action-row {{ display:flex; align-items:center; justify-content:flex-end; gap:3px; flex-wrap:wrap; }}
    .intake-action-row.command-group {{ border:1px solid #444; border-radius:0; background:#050505; padding:2px; }}
    .table-command-bar {{ display:flex; justify-content:flex-end; margin-top:4px; border-top:1px solid #222; padding-top:4px; }}
    .command-label {{ color:#8a8a8a; font-size:var(--text-xs); line-height:1; letter-spacing:.08em; padding:0 4px; text-transform:uppercase; white-space:nowrap; }}
    .intake-action-row button {{ padding:4px 7px; min-height:24px; font-size:var(--text-xs); line-height:1.1; white-space:nowrap; }}
    .upload-extract-row {{ display:flex; align-items:center; gap:4px; flex-wrap:wrap; }}
    .prospectus-action-button {{ width:auto; padding:4px 7px; min-height:24px; font-size:var(--text-xs); line-height:1.1; letter-spacing:.01em; }}
    .prospectus-action-button.cmd-secondary {{ border-color:#444; color:#d8d8d8; background:#090909; }}
    .prospectus-action-button.cmd-primary {{ border-color:var(--accent); color:#000; background:var(--accent); }}
    .prospectus-action-button.cmd-primary:not(:disabled):hover {{ border-color:#ffc04d; color:#000; background:#ffc04d; }}
    .row-select-glyph {{ display:inline-flex; align-items:center; justify-content:center; width:18px; height:18px; border:1px solid #555; color:#000; background:#070707; font-size:var(--text-xs); font-weight:700; }}
    tr.selected .row-select-glyph {{ border-color:var(--accent); background:var(--accent); color:#000; }}
    .cmd-primary {{ border-color:var(--accent); color:#000; background:var(--accent); }}
    .cmd-primary:hover {{ border-color:#ffc04d; background:#ffc04d; color:#000; }}
    .cmd-utility {{ color:#aaa; border-color:#444; }}
    .cmd-danger {{ border-color:#5c1f1f; color:#ff9a9a; }}
    .cmd-danger:hover {{ border-color:#ff5555; color:#ffb3b3; }}
    .subtab-panel[aria-busy="true"] .intake-toolbar {{ border-bottom-color:#ffd43b; }}
    tr.active td {{ border-top:1px solid #ffd43b; border-bottom:1px solid #ffd43b; }}
    tr.selected td, tr.selected-row td {{ background:#211600; }}
    tr.clickable-row {{ cursor:pointer; }}
    .badge {{ display:inline-block; border:1px solid #555; padding:2px 5px; color:#d8d8d8; font-size:var(--text-xs); text-transform:uppercase; letter-spacing:.04em; }}
    .badge.warn {{ border-color:var(--warning); color:var(--warning); }} .badge.good {{ border-color:var(--good); color:var(--good); }} .badge.bad {{ border-color:var(--bad); color:var(--bad); }}
    .progress-wrap {{ display:none; border:1px solid #333; padding:4px; margin:4px 0; background:#000; }}
    .progress-wrap.active {{ display:block; }}
    .progress-track {{ height:8px; border:1px solid #555; overflow:hidden; background:#111; }}
    .progress-bar {{ width:0%; height:100%; background:var(--accent); transition:width .25s ease; }}
    .progress-wrap.complete {{ border-color:var(--good); }}
    .progress-wrap.complete .progress-bar {{ background:var(--good); }}
    .progress-wrap.warning {{ border-color:var(--warning); }}
    .progress-wrap.warning .progress-bar {{ background:var(--warning); }}
    .progress-wrap.failed {{ border-color:var(--bad); }}
    .progress-wrap.failed .progress-bar {{ background:var(--bad); }}
    .term-group {{ border:1px solid #333; padding:5px; margin-top:5px; background:#030303; }}
    details.term-group > summary {{ display:flex; justify-content:space-between; gap:8px; color:var(--ink); font-weight:700; text-transform:uppercase; }}
    .term-section-heading {{ display:flex; align-items:center; justify-content:space-between; gap:8px; margin:7px 0 3px; }}
    .term-section-heading h3 {{ margin:0; }}
    .term-row.attention td {{ background:#160d00; }}
    .term-row input, .term-row select {{ padding:5px 6px; width:100%; box-sizing:border-box; }}
    .term-row input[readonly] {{ color:#d8d8d8; border-style:dashed; background:#101010; }}
    .term-table td {{ vertical-align:top; white-space:normal; }}
    .term-table th:nth-child(1) {{ width:30%; }}
    .term-table th:nth-child(2) {{ width:28%; }}
    .term-table th:nth-child(3) {{ width:42%; }}
    .term-help {{ display:block; margin-top:2px; color:#a8a8a8; font-size:var(--text-xs); line-height:1.25; }}
    .term-evidence {{ border:0; padding:0; }}
    .term-evidence > summary {{ color:#d8d8d8; font-size:var(--text-xs); }}
    .term-evidence-snippet {{ margin-top:4px; white-space:pre-wrap; border-left:2px solid var(--accent); padding:5px 7px; background:#000; max-height:105px; overflow:auto; color:#c8c8c8; font-size:var(--text-xs); }}
    .term-evidence-meta {{ color:#bdbdbd; margin-bottom:3px; }}
    .evidence-list pre {{ white-space:pre-wrap; border:1px solid #252525; padding:7px; background:#000; }}
    .danger-zone {{ border:1px solid #5c1f1f; padding:6px; margin-top:6px; }}
    table {{ width:100%; border-collapse:collapse; font-size:var(--text-sm); }}
    th,td {{ padding:3px 5px; border-bottom:1px solid #222; text-align:left; white-space:nowrap; }}
    th {{ position:sticky; top:0; background:#b8b8b8; z-index:1; color:#000; font-weight:800; }}
    th[data-sortable="true"] {{ cursor:pointer; user-select:none; }}
    th[data-sortable="true"]::after {{ content:' ↕'; color:#555; font-size:var(--text-xs); }}
    th[data-sort-direction="asc"]::after {{ content:' ↑'; color:var(--accent); }}
    th[data-sort-direction="desc"]::after {{ content:' ↓'; color:var(--accent); }}
    .table-wrap {{ overflow:auto; max-height:70vh; }}
    .navigation-shell {{ display:flex; align-items:flex-start; justify-content:space-between; gap:3px; border-bottom:1px solid #666; padding:0; background:#111; }}
    .tab-bar {{ display:flex; gap:2px; flex-wrap:wrap; margin:0; padding:0; overflow:visible; }}
    .tab-button {{ flex:0 0 auto; padding:4px 8px; border:1px solid #4a4a4a; background:#b8b8b8; color:#000; font-size:var(--text-xs); letter-spacing:.01em; }}
    .tab-button:hover {{ color:#000; border-color:var(--accent); background:#d0d0d0; }}
    .tab-button.active {{ color:#000; background:var(--accent); border-color:var(--accent); }}
    .secondary-tabs {{ position:relative; flex:0 0 auto; border:0; padding:0; }}
    .secondary-tabs > summary {{ list-style:none; padding:4px 8px; border:1px solid #4a4a4a; border-radius:0; color:#000; background:#b8b8b8; font-size:var(--text-xs); font-weight:800; text-transform:uppercase; }}
    .secondary-tabs > summary::-webkit-details-marker {{ display:none; }}
    .secondary-tabs > summary::after {{ content:"  +"; color:var(--accent); }}
    .secondary-tabs[open] > summary::after {{ content:"  −"; }}
    .secondary-tabs > summary.active {{ color:#000; border-color:var(--accent); background:var(--accent); }}
    .secondary-tab-bar {{ position:absolute; z-index:12; right:0; top:calc(100% + 2px); width:min(640px, calc(100vw - 16px)); padding:3px; border:1px solid #666; border-radius:0; background:#080808; justify-content:flex-end; }}
    .secondary-tab-bar .tab-button {{ background:#b8b8b8; border-color:#555; }}
    .secondary-tab-bar .tab-button.active {{ color:#000; background:var(--accent); border-color:var(--accent); }}
    .tab-panel {{ display:none; }}
    .tab-panel.active {{ display:grid; gap:var(--layout-gap); }}
    .status-strip {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(min(140px,100%),1fr)); gap:2px; margin-bottom:5px; }}
    .status-cell, .status-chip {{ border:1px solid #333; padding:3px 5px; background:#000; font-size:var(--text-xs); min-width:0; }}
    .status-cell b {{ display:block; color:#fff; margin-top:3px; overflow:hidden; text-overflow:ellipsis; }}
    .status-chip b {{ display:block; color:#fff; margin-top:3px; font-size:var(--text-base); overflow-wrap:anywhere; }}
    .workflow-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(min(250px,100%),1fr)); gap:5px; }}
    .workflow-box {{ border:1px solid var(--line); border-radius:0; padding:6px; background:#050505; }}
    .next-step-card {{ display:flex; align-items:center; justify-content:space-between; gap:10px; border-left:3px solid var(--accent); padding:7px 9px; margin:5px 0; background:#100b00; }}
    .next-step-card.good {{ border-left-color:var(--good); background:#001006; }}
    .next-step-card.bad {{ border-left-color:var(--bad); background:#100000; }}
    .next-step-card p {{ margin:0; color:#e8e8e8; }}
    .next-step-card button {{ flex:0 0 auto; }}
    .diagnostic-box {{ border:1px solid #5c1f1f; background:#070000; padding:9px; margin-top:10px; color:#ffb3b3; }}
    .diagnostic-box.good {{ border-color:#2f5c38; background:#000700; color:#b7f7c4; }}
    .assumption-gate {{ display:flex; align-items:center; justify-content:space-between; gap:10px; border-left:3px solid var(--warning); padding:8px 9px; margin:0 0 8px; background:#100b00; }}
    .assumption-gate[hidden] {{ display:none; }}
    .assumption-gate p {{ margin:0; color:#f1dda5; }}
    .assumption-gate button {{ flex:0 0 auto; }}
    .assumption-readiness {{ border:1px solid #5d4b1a; background:#100b00; padding:7px 8px; margin:0 0 8px; }}
    .assumption-readiness.good {{ border-color:#2f5c38; background:#001006; }}
    .assumption-readiness.good p {{ color:#b7f7c4; }}
    .help-list {{ margin:3px 0; padding-left:18px; color:#d8d8d8; line-height:1.35; }}
    .help-list li {{ margin:2px 0; white-space:normal; }}
    .help-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(min(230px,100%),1fr)); gap:5px; }}
    .help-card-grid, .help-topic-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(min(230px,100%),1fr)); gap:5px; margin-top:7px; }}
    .help-card {{ border-top:2px solid var(--accent); padding:7px; background:#050505; }}
    .help-card h3 {{ margin-bottom:5px; }}
    .help-topic-grid article {{ border:1px solid #333; padding:7px; background:#030303; }}
    .help-topic-grid article p {{ margin-bottom:0; }}
    .file-purpose-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:5px; margin-top:7px; }}
    .step-number {{ min-width:20px; height:20px; display:grid; place-items:center; border:1px solid var(--accent); border-radius:0; color:#000; background:var(--accent); font-family:var(--mono); font-size:var(--text-xs); font-weight:800; }}
    .cta-row {{ display:flex; flex-wrap:wrap; gap:3px; margin-top:5px; }}
    .cta-row button {{ width:auto; }}
    .text-button {{ color:var(--accent); border-color:#7a5200; background:#0d0900; }}
    .numbered-heading {{ display:flex; gap:10px; align-items:flex-start; }}
    .numbered-heading .step-number {{ flex:0 0 auto; margin-top:1px; }}
    .terminal-task-grid {{ display:grid; grid-template-columns:minmax(0,1.25fr) minmax(260px,.75fr); gap:5px; }}
    .market-guide {{ border:1px solid #665000; border-left:3px solid var(--accent); padding:8px; margin:6px 0; background:#0e0a00; }}
    .market-guide-header {{ display:flex; align-items:flex-start; justify-content:space-between; gap:8px; flex-wrap:wrap; }}
    .market-guide-header h3 {{ margin:0; }}
    .market-requirements-grid {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:4px; margin-top:7px; }}
    .market-requirement {{ min-width:0; border:1px solid #444; border-top:2px solid var(--accent); padding:6px; background:#030303; }}
    .market-requirement.good {{ border-top-color:var(--good); }}
    .market-requirement.bad {{ border-top-color:var(--bad); }}
    .market-requirement.neutral {{ border-top-color:#777; }}
    .market-requirement-label {{ display:block; color:var(--muted); font-size:var(--text-xs); text-transform:uppercase; }}
    .market-requirement strong {{ display:block; margin:3px 0; color:#fff; overflow-wrap:anywhere; }}
    .market-next-action {{ margin-top:7px; color:#fff; }}
    .market-mismatch-note {{ margin-top:7px; border-left:2px solid var(--warning); padding:5px 7px; background:#100d00; color:#e8e8e8; }}
    .source-library {{ margin-top:8px; }}
    .upload-card {{ border:1px solid #555; border-radius:0; padding:6px; background:#050505; }}
    .upload-card input[type="file"] {{ margin:4px 0; padding:5px; border-style:solid; }}
    .upload-card button {{ width:100%; }}
    .terminal-task-grid > .workflow-box > .cmd-primary {{ width:100%; margin-top:4px; }}
    .format-note {{ display:flex; gap:4px; align-items:center; color:#b5b5b5; font-size:var(--text-xs); }}
    .format-pill {{ display:inline-block; border:1px solid #777; border-radius:0; padding:1px 4px; color:var(--accent); font-family:var(--mono); font-size:var(--text-xs); }}
    .technical-details {{ margin-top:5px; }}
    .technical-details pre {{ margin-bottom:0; }}
    .disabled-control {{ opacity:.65; cursor:not-allowed; }}
    pre {{ max-width:100%; white-space:pre-wrap; overflow-wrap:anywhere; word-break:break-word; font-size:var(--text-sm); }}
    .error {{ color:var(--bad); }} .good {{ color:#8ce99a; }} .warn {{ color:#ffd43b; }} .muted {{ color:var(--muted); }} .small {{ font-size:var(--text-sm); }}
    @media (max-width:900px) {{
      .command-suggestion {{ grid-template-columns:minmax(0,1fr); }}
      .command-suggestion > * {{ min-width:0; overflow:hidden; text-overflow:ellipsis; }}
      .chart-actions {{ align-items:flex-start; flex-wrap:wrap; }}
      .terminal-task-grid {{ grid-template-columns:1fr; }}
    }}
    @media (max-width:640px) {{
      header {{ align-items:flex-start; flex-wrap:wrap; }}
      .terminal-badge {{ margin-top:2px; }}
      .instrument-context {{ grid-template-columns:1fr; }}
      .context-actions {{ display:grid; grid-template-columns:1fr 1fr; }}
      .context-actions button {{ min-width:0; }}
      .navigation-shell {{ align-items:stretch; flex-direction:column; }}
      .tab-bar {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); width:100%; }}
      .tab-button {{ white-space:normal; }}
      .secondary-tabs {{ width:100%; }}
      .secondary-tabs > summary {{ text-align:center; }}
      .secondary-tab-bar {{ position:static; width:100%; margin-top:7px; grid-template-columns:repeat(2,minmax(0,1fr)); }}
      .intake-toolbar {{ position:static; }}
      .intake-toolbar-title {{ min-width:0; flex-basis:100%; }}
      .intake-action-row {{ justify-content:flex-start; }}
      .intake-action-row.command-group {{ width:100%; flex-wrap:nowrap; justify-content:flex-start; overflow-x:auto; }}
      .intake-action-row button {{ min-height:38px; white-space:normal; }}
      .chart-actions {{ flex-direction:column; gap:6px; }}
      .chart-actions button {{ width:auto; }}
      .subtab-panel {{ max-height:none; overflow:visible; }}
      main input:not([type="checkbox"]):not([type="radio"]), main select, main textarea {{ font-size:16px; }}
      .term-group {{ overflow:visible; }}
      .term-table, .term-table tbody {{ display:block; width:100%; min-width:0; }}
      .term-table thead {{ position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden; clip:rect(0,0,0,0); white-space:nowrap; border:0; }}
      .term-table .term-row {{ display:block; margin:0 0 10px; padding:8px 10px; border:1px solid #333; background:#000; }}
      .term-table td {{ display:block; width:100%; padding:8px 0; border:0; border-bottom:1px solid #242424; }}
      .term-table td:last-child {{ border-bottom:0; }}
      .term-table td::before {{ display:block; margin-bottom:4px; color:var(--muted); font-size:var(--text-xs); letter-spacing:.06em; text-transform:uppercase; }}
      .term-table td:nth-child(1)::before {{ content:"Term"; }}
      .term-table td:nth-child(2)::before {{ content:"Current / edit value"; }}
      .term-table td:nth-child(3)::before {{ content:"Source"; }}
      .next-step-card {{ align-items:stretch; flex-direction:column; }}
      .next-step-card button {{ width:100%; }}
      .assumption-gate {{ align-items:stretch; flex-direction:column; }}
      .assumption-gate button {{ width:100%; }}
      .file-purpose-grid {{ grid-template-columns:1fr; }}
      .market-requirements-grid {{ grid-template-columns:1fr; }}
    }}
    @media (max-width:420px) {{
      :root {{ --page-gutter:5px; --panel-pad:6px; --layout-gap:4px; }}
      header {{ gap:5px; }}
      .terminal-badge {{ padding:2px 0 2px 5px; }}
      .command-shell {{ min-height:51px; }}
      .tab-button, .subtab-button {{ padding:6px 8px; }}
    }}
  </style>
</head>
<body>
<header>
  <div class="brand-lockup">
    <h1>CB TERMINAL</h1>
  </div>
  <div class="terminal-badge">LOCAL</div>
</header>
<nav class="instrument-nav" aria-label="Instrument navigation">
  <div class="command-shell">
    <span class="command-prompt">SECURITY</span>
    <div class="command-input-wrap">
      <div id="cb-command-ghost" class="command-input-ghost" aria-hidden="true"></div>
      <input id="cb-command-input" autocomplete="off" spellcheck="false" placeholder="ISIN / issuer / ticker / name" aria-label="Search by ISIN, issuer, ticker, or display name" aria-autocomplete="both" aria-controls="cb-command-suggestions" aria-expanded="false">
      <span id="cb-command-caret" class="command-caret" aria-hidden="true"></span>
    </div>
  </div>
  <div id="cb-command-suggestions" class="command-autocomplete" role="listbox" aria-label="CB command suggestions"></div>
  <div class="instrument-context">
    <div id="selected-cb-identity" class="selected-identity" aria-live="polite">NO SECURITY SELECTED</div>
    <div class="context-actions" aria-label="Selected instrument shortcuts">
      <button type="button" id="open-data-management" class="text-button">Manage data</button>
      <button type="button" id="open-help" class="text-button">Help</button>
    </div>
  </div>
</nav>
<main class="workbench-layout">
  <select id="cb-select" class="hidden-select" aria-label="Selected convertible bond"><option value="">Loading coverage universe...</option></select>
  <section class="plots-panel" aria-label="Pricing plots and rows">
    <div class="navigation-shell">
      <nav class="tab-bar" aria-label="Primary navigation">
        <button type="button" class="tab-button active" data-tab="pm-view" aria-current="page">Summary</button>
        <button type="button" class="tab-button" data-tab="data-management">Data</button>
        <button type="button" class="tab-button" data-tab="assumptions">Assumptions</button>
        <button type="button" class="tab-button" data-tab="nuke">Nuke</button>
        <button type="button" class="tab-button" data-tab="help">Help</button>
      </nav>
      <details class="secondary-tabs">
        <summary>Advanced views</summary>
        <nav class="tab-bar secondary-tab-bar" aria-label="Advanced navigation">
          <button type="button" class="tab-button" data-tab="valuation">Valuation details</button>
          <button type="button" class="tab-button" data-tab="sensitivity">Sensitivity</button>
          <button type="button" class="tab-button" data-tab="priced-rows">Priced rows</button>
          <button type="button" class="tab-button" data-tab="raw-quotes">Raw quotes</button>
          <button type="button" class="tab-button" data-tab="audit">Audit</button>
        </nav>
      </details>
    </div>
    <section id="tab-pm-view" class="tab-panel active">
      <section class="panel output-panel">
        <h2>Pricing Analysis</h2>
        <section id="assumption-gate" class="assumption-gate" aria-live="polite">
          <p id="assumption-gate-message">Complete the pricing assumptions before running a valuation.</p>
          <button type="button" id="complete-assumptions" class="cmd-primary">Enter assumptions</button>
        </section>
        <section class="kpis" id="kpis" aria-label="Pricing summary"></section>
        <section id="iv-diagnostics" class="diagnostic-box" aria-live="polite" hidden></section>
        <section id="active-assumptions-strip" class="active-assumptions-strip" aria-label="Active assumptions"></section>
        <div class="cta-row">
          <button type="button" id="view-summary-data" class="text-button">Manage data</button>
          <button type="button" id="edit-assumptions" class="cmd-utility">Assumptions</button>
        </div>
      </section>
    </section>
    <section id="tab-valuation" class="tab-panel">
      <section class="panel plot-panel">
        <h2>Valuation Stack</h2>
        <div class="chart-actions"><span class="chart-gesture-hint">PRICE / FAIR VALUE / PARITY / FLOOR · CTRL/⌘ OR ALT + WHEEL TO ZOOM</span><button type="button" data-zoom-window="valuation-stack">Reset</button></div>
        <svg id="price-chart" class="matlab-plot pm-plot" viewBox="0 0 900 220" role="img" aria-label="Fair value market price parity floor chart"></svg>
        <svg id="valuation-cheapness-mini-chart" class="matlab-plot small-plot pm-small-plot" viewBox="0 0 900 160" role="img" aria-label="Valuation cheap rich mini chart"></svg>
      </section>
      <section class="panel plot-panel">
        <h2>Relative Value Drivers</h2>
        <div class="chart-actions"><span class="chart-gesture-hint">Ctrl/⌘ or Alt + wheel to zoom · drag to pan</span><button type="button" data-zoom-window="rv-drivers">Reset</button></div>
        <div class="small-multiple-grid">
          <svg id="rv-cheapness-chart" class="matlab-plot small-plot pm-small-plot" viewBox="0 0 900 160" role="img" aria-label="Relative value cheap rich chart"></svg>
          <svg id="rv-iv-chart" class="matlab-plot small-plot pm-small-plot" viewBox="0 0 900 160" role="img" aria-label="Relative value implied volatility chart"></svg>
          <svg id="rv-credit-spread-chart" class="matlab-plot small-plot pm-small-plot" viewBox="0 0 900 160" role="img" aria-label="Relative value credit spread bps chart"></svg>
          <svg id="rv-stock-chart" class="matlab-plot small-plot pm-small-plot" viewBox="0 0 900 160" role="img" aria-label="Relative value underlying stock chart"></svg>
        </div>
      </section>
      <section class="panel plot-panel">
        <h2>Volatility Overlay</h2>
        <svg id="volatility-overlay-chart" class="matlab-plot pm-plot" viewBox="0 0 900 220" role="img" aria-label="Implied and assumption volatility chart"></svg>
      </section>
      <section class="panel plot-panel">
        <h2>Yield curve used as risk-free rate</h2>
        <p class="chart-note">Tenors use actual years. The marker shows the maturity/tenor used for the latest priced row.</p>
        <svg id="yield-curve-chart" class="matlab-plot" viewBox="0 0 900 240" role="img" aria-label="Yield curve chart with maturity highlight"></svg>
      </section>
      <section class="panel plot-panel">
        <h2>Assumptions: credit spread (bps)</h2>
        <p class="chart-note">Credit spread uses a bps axis.</p>
        <svg id="assumptions-credit-spread-chart" class="matlab-plot" viewBox="0 0 900 240" role="img" aria-label="Credit spread bps assumptions chart"></svg>
      </section>
      <section class="panel plot-panel">
        <h2>Assumptions: rates and volatility (%)</h2>
        <p class="chart-note">Volatility, RF, borrow, and dividend are shown on a percent axis.</p>
        <svg id="assumptions-rates-chart" class="matlab-plot" viewBox="0 0 900 240" role="img" aria-label="Percent assumptions chart"></svg>
      </section>
      <section class="panel plot-panel">
        <h2>Market FX</h2>
        <p class="chart-note">FX is shown separately from valuation metrics.</p>
        <svg id="fx-chart" class="matlab-plot" viewBox="0 0 900 240" role="img" aria-label="Market FX chart"></svg>
      </section>
    </section>
    <section id="tab-sensitivity" class="tab-panel">
      <section class="panel table-wrap">
        <h2>Sensitivity: latest-row what-if</h2>
        <p class="chart-note">Stress the latest row without saving assumptions or valuation runs.</p>
        <table id="sensitivity-table"><thead><tr><th>Scenario</th><th>Vol</th><th>Credit spread (bps)</th><th>Borrow</th><th>Dividend</th><th>Fair value</th><th>Cheapness</th><th>IV</th><th>Delta vs base FV</th></tr></thead><tbody></tbody></table>
      </section>
    </section>
    <section id="tab-assumptions" class="tab-panel">
      <section class="panel controls-panel" aria-label="Pricing assumptions and controls">
        <form id="pricing-form">
          <h2>Assumptions</h2>
          <p class="chart-note">Enter the economic assumptions first. Building market history does not price the bond; Preview does not save changes.</p>
          <div id="assumption-readiness" class="assumption-readiness" aria-live="polite">
            <p>Volatility, credit spread, borrow, dividend, and a risk-free source are required.</p>
          </div>
          <div class="assumption-grid">
            <label>Volatility (%) <input name="volatility" type="number" step="0.01"></label>
            <input name="risk_free_rate" type="hidden">
            <input name="contract_path" type="hidden">
            <input name="market_history_path" type="hidden">
            <input name="raw_price_history_path" type="hidden">
            <label>Credit spread (bps) <input name="credit_spread" type="number" step="1"></label>
            <label>Borrow cost (%) <input name="borrow_rate" type="number" step="0.01"></label>
            <label>Dividend yield (%) <input name="dividend_yield" type="number" step="0.01"></label>
            <label>Risk-free source
              <select name="risk_free_source" id="risk-free-source">{yield_curve_options_html}<option value="manual" selected>Manual</option></select>
            </label>
            <label id="manual-risk-free-field">Manual risk-free rate (%) <input name="manual_rf_display" type="number" step="0.01" oninput="form.elements.risk_free_rate.value=this.value"></label>
            <label>Tree steps <input name="steps" type="number" min="3" max="500" value="250"></label>
          </div>
          <label>Model
            <select name="model_mode"><option value="simple_crr">Simple CRR</option><option value="tf_split_tree" selected>TF split tree</option></select>
          </label>
          <label>Scenario <input name="scenario_name" value="base"></label>
          <p id="risk-free-source-note" class="small muted">Choose a supported government yield curve or enter a manual risk-free rate.</p>
          <div class="button-row intake-action-row command-group assumption-action-group" aria-label="Pricing assumption actions">
            <span class="command-label">PRICE</span>
            <button type="submit" class="cmd-primary" aria-label="Run price preview" title="Run non-persistent price preview">Price Preview</button>
            <button type="button" id="save-assumptions" aria-label="Save assumption set" title="Save current assumptions as an append-only set">Save Assumption Set</button>
          </div>
          <div id="price-preview-progress" class="progress-wrap" aria-live="polite">
            <div class="small muted" id="price-preview-progress-text">Price preview not running.</div>
            <div class="progress-track"><div class="progress-bar"></div></div>
          </div>
          <span class="small muted">Saved assumptions are append-only.</span>
          <div class="metric-block">
            <span class="muted small">Metric views use separate units</span>
            <p class="small muted">Price, cheap/rich, IV, stock, FX, and rates use separate views.</p>
          </div>
          <details>
            <summary>Advanced assumptions</summary>
            <div class="advanced-grid">
              <label class="check-row"><input name="use_history_assumptions" type="checkbox" value="1"><span>Use assumption overrides in CSV</span></label>
            </div>
            <p class="small muted">Leave CSV overrides off for what-if runs. Turn them on only to replay file assumptions.</p>
          </details>
        </form>
        <p id="status" class="muted" role="status">Loading coverage universe...</p>
        <p class="small muted">Pricing uses reviewed terms and validated market-history rows. Raw quotes and PDFs stay as sources until processed.</p>
      </section>
    </section>
    <section id="tab-nuke" class="tab-panel">
      <section class="panel controls-panel" aria-label="Dollar-neutral nuke calculator">
        <form id="nuke-form">
          <h2>Nuke</h2>
          <p class="chart-note">Quickly reprice an anchor bond quote from the FX-adjusted stock move while holding delta and all other context fixed.</p>
          <p id="nuke-context" class="small muted">Enter an anchor observation, the current stock and FX, and the anchor delta.</p>
          <div class="assumption-grid">
            <label>Anchor bond price <input name="anchor_bond_price" type="number" step="any" required></label>
            <label>Anchor stock price <input name="anchor_stock_price" type="number" step="any" required></label>
            <label>Anchor FX <input name="anchor_fx" type="number" step="any" required></label>
            <label>Current stock price <input name="current_stock_price" type="number" step="any" required></label>
            <label>Current FX <input name="current_fx" type="number" step="any" required></label>
            <label>Anchor delta <input name="delta" type="number" step="any" required></label>
          </div>
          <p class="small muted">FX uses stock-currency units per one bond-currency unit. The linear result does not model convexity over large moves.</p>
          <div class="button-row intake-action-row command-group" aria-label="Nuke actions">
            <span class="command-label">NUKE</span>
            <button type="submit" class="cmd-primary">Nuke</button>
            <button type="button" id="reset-nuke-anchor" class="cmd-utility">Use latest row</button>
          </div>
        </form>
        <section id="nuke-result" aria-live="polite">
          <p class="muted">No nuke calculated.</p>
        </section>
      </section>
    </section>
    <section id="tab-priced-rows" class="tab-panel">
      <section class="panel table-wrap">
        <h2>Priced Rows</h2>
        <p class="chart-note">Valuation rows after contract and market-history validation. Assumptions shown are the values used for each row.</p>
        <table id="results-table"><thead><tr><th>Date</th><th>CB px</th><th>YTM</th><th>Yield to put</th><th>Put date</th><th>Stock</th><th>FX</th><th>Fair value</th><th>Parity</th><th>Bond floor</th><th>IV</th><th>Curve/RF</th><th>Credit spread (bps)</th><th>Borrow</th><th>Div</th><th>Cheapness</th><th>Output ccy</th><th>Source</th><th>Warnings</th></tr></thead><tbody></tbody></table>
      </section>
    </section>
    <section id="tab-raw-quotes" class="tab-panel">
      <section class="panel plot-panel">
        <h2>Raw quote history</h2>
        <p class="chart-note">Raw quote rows for the selected ISIN. These are source rows, not joined valuation rows.</p>
        <svg id="raw-quote-chart" class="matlab-plot" viewBox="0 0 900 240" role="img" aria-label="Raw CB quote history chart"></svg>
      </section>
      <section class="panel table-wrap">
        <h2>Raw quote rows</h2>
        <table id="raw-quotes-table"><thead><tr><th>Date</th><th>Time</th><th>Dealer</th><th>Bid</th><th>Ask</th><th>Mid</th><th>Stock</th><th>Security</th><th>Reference</th><th>Source row</th></tr></thead><tbody></tbody></table>
      </section>
    </section>
    <section id="tab-data-management" class="tab-panel">
      <section class="panel">
        <span class="eyebrow">Sources to valuation</span>
        <h2>Data management</h2>
        <p class="chart-note">Upload source files, review and approve extracted terms, then match prices and build valuation history.</p>
        <div class="status-strip" id="prospectus-status-strip"></div>
        <nav class="subtab-bar" aria-label="Data management steps">
          <button type="button" class="subtab-button active" data-data-subtab="upload" aria-controls="data-subtab-upload" aria-current="step">1 Upload</button>
          <button type="button" class="subtab-button" data-data-subtab="review" aria-controls="data-subtab-review">2 Review &amp; approve</button>
          <button type="button" class="subtab-button" data-data-subtab="match" aria-controls="data-subtab-match">3 Match &amp; build</button>
          <button type="button" class="subtab-button" data-data-subtab="library" aria-controls="data-subtab-library">Files</button>
        </nav>
        <section id="data-subtab-match" class="subtab-panel" aria-label="Match data and build valuation history">
        <h2>Match &amp; build</h2>
        <p class="chart-note">Check uploaded prices against the selected bond, then build valuation history when every input is ready.</p>
        <div id="market-data-guide" class="market-guide" aria-live="polite">
          <div class="market-guide-header">
            <h3>Select a bond</h3>
            <span class="badge warn">Waiting</span>
          </div>
          <p class="market-next-action">Select a bond to see exactly which price files it needs.</p>
        </div>
        <div class="terminal-task-grid">
          <div class="workflow-box">
            <div class="numbered-heading">
              <span class="step-number">1</span>
              <div><h3>Match uploaded prices</h3><p class="small muted">Recheck exact bond, stock, and FX identifiers after reviewing terms or uploading files.</p></div>
            </div>
            <div class="button-row">
              <button type="button" id="match-uploaded-market-data" class="cmd-primary">Match uploaded prices</button>
              <button type="button" class="cmd-utility" data-open-data-step="upload">Upload more files</button>
            </div>
            <p id="market-match-status" class="small muted" role="status">Already uploaded prices? Match exact identifiers after adding or correcting the termsheet.</p>
          </div>
          <div class="workflow-box">
            <div class="numbered-heading">
              <span class="step-number">2</span>
              <div><h3>Build valuation history</h3><p id="market-build-status" class="small muted">Upload the requested prices first.</p></div>
            </div>
            <button type="button" id="generate-valuation-history" class="cmd-primary" data-market-action="build" disabled>Build valuation history</button>
            <div id="market-build-progress" class="progress-wrap" aria-live="polite">
              <div class="small muted" id="market-build-progress-text">Build not running.</div>
              <div class="progress-track"><div class="progress-bar"></div></div>
            </div>
          </div>
        </div>
        <div class="cta-row">
          <button type="button" id="refresh-selected-cb" class="cmd-utility" aria-label="Refresh selected CB and price" title="Reload selected CB paths and price if ready">Refresh selected bond</button>
        </div>
        </section>
        <section id="data-subtab-library" class="subtab-panel" aria-label="Uploaded file library">
        <span class="eyebrow">Advanced · Data library</span>
        <h2>Uploaded files and troubleshooting</h2>
        <p class="chart-note">Inspect, rename, or remove uploaded files. Normal upload, approval, and matching stay in the first three steps.</p>
        <div id="source-library-details" class="source-library">
          <div class="status-strip" id="source-status-strip"></div>
          <div class="intake-toolbar">
          <div class="intake-toolbar-header">
            <div class="advanced-grid">
              <label>Search <input id="source-search" placeholder="filename, PM name, ISIN, type"></label>
              <label>Type
                <select id="source-kind-filter">
                  <option value="">All sources</option>
                  <option value="raw_prospectus">Raw prospectus PDFs</option>
                  <option value="raw_price_history">Raw quote / market data files</option>
                  <option value="generated_market_history">Valuation-ready market history</option>
                  <option value="contract">Contract terms JSON</option>
                </select>
              </label>
            </div>
            <div class="intake-action-row command-group" aria-label="Data source actions">
              <span class="command-label">SOURCE</span>
              <button type="button" id="refresh-source-matches" class="cmd-utility" aria-label="Refresh market source matches" title="Refresh market source matches">Refresh</button>
              <button type="button" id="rename-source" aria-label="Rename selected source" title="Rename selected source">Rename</button>
              <button type="button" id="edit-source" aria-label="Open selected contract terms" title="Open the selected contract in Review and approve">Open terms</button>
              <button type="button" id="remove-source" class="cmd-danger" aria-label="Remove selected sources" title="Confirm removal for each selected source">Remove</button>
            </div>
          </div>
          <div id="source-link-progress" class="progress-wrap" aria-live="polite">
            <div class="small muted" id="source-link-progress-text">Source link not running.</div>
            <div class="progress-track"><div class="progress-bar"></div></div>
          </div>
          <pre id="source-action-status" class="small muted">No source selected.</pre>
            <div id="market-generation-readiness" class="workflow-box small muted">Select a bond to inspect technical readiness.</div>
          </div>
          <div class="table-wrap">
            <table id="sources-table"><thead><tr><th></th><th>Name</th><th>Type</th><th>Status</th><th>Used by / coverage</th><th>Identifiers found</th><th>Size</th><th>Last modified</th><th>Location</th><th>Actions / Notes</th></tr></thead><tbody id="sources-body"><tr><td colspan="10" class="muted">Loading data sources...</td></tr></tbody></table>
          </div>
          <div id="source-detail" class="workflow-box"><p class="small muted">Select a row to see technical details.</p></div>
        </div>
      </section>
        <section id="data-subtab-upload" class="subtab-panel active" aria-label="Upload source files">
          <h2>Upload source files</h2>
          <p class="chart-note">Add bond documents and dated market prices in one place. File type determines the next step automatically.</p>
          <div class="workflow-grid">
          <div class="upload-card">
            <h3>Upload termsheet or prospectus</h3>
            <div class="format-note"><span class="format-pill">PDF</span><span>Terms are extracted automatically. You still approve them before pricing.</span></div>
            <input id="upload-prospectus" type="file" accept="application/pdf,.pdf" multiple aria-label="Choose prospectus PDF files">
            <button type="button" class="cmd-primary" data-upload-kind="prospectus" data-upload-input="upload-prospectus" data-upload-status="prospectus-upload-status">Upload &amp; extract</button>
            <pre id="prospectus-upload-status" class="small muted">NO PROSPECTUS UPLOAD</pre>
          </div>
          <div class="upload-card">
            <h3>Upload market prices</h3>
            <div class="format-note"><span class="format-pill">CSV</span><span class="format-pill">XLSX</span><span>Dated CB, stock, or FX prices.</span></div>
            <input id="upload-market-data" type="file" accept=".csv,.xlsx" multiple aria-label="Choose CSV or Excel market data files">
            <button type="button" id="upload-market-data-button" class="cmd-primary" data-upload-kind="market_data_auto" data-upload-input="upload-market-data">Upload price files</button>
            <pre id="upload-status" class="small muted">NO MARKET DATA UPLOAD</pre>
          </div>
          </div>
          <details id="pdf-extraction-queue" class="technical-details">
            <summary>PDF extraction queue and retries</summary>
          <div class="intake-toolbar">
            <div class="intake-toolbar-header">
              <div class="intake-toolbar-title">
                <h3>Retry extraction</h3>
                <p class="chart-note">Use these controls only for PDFs still waiting below.</p>
              </div>
              <div class="intake-action-row command-group" aria-label="Raw PDF extraction actions">
                <span class="command-label">INTAKE</span>
                <button type="button" id="extract-selected-prospectuses" class="prospectus-action-button cmd-primary" aria-label="Extract selected PDFs" title="Select one or more raw PDFs before extraction" disabled>Extract selected (0)</button>
                <button type="button" id="extract-all-prospectuses" class="prospectus-action-button cmd-secondary" aria-label="Extract all pending PDFs" title="Extract all pending PDFs">Extract all</button>
                <button type="button" id="refresh-review-queue" class="cmd-utility" aria-label="Refresh review queue" title="Refresh review queue">Refresh</button>
              </div>
            </div>
            <div id="extraction-progress" class="progress-wrap" aria-live="polite">
              <div class="small muted" id="extraction-progress-text">Extraction not running.</div>
              <div class="progress-track"><div class="progress-bar"></div></div>
            </div>
            <pre id="prospectus-extraction-status" class="small muted">Extraction idle.</pre>
            <details class="technical-details">
              <summary>Extraction environment</summary>
              <div id="extraction-environment-card" class="workflow-box">
                <h3>Extraction preflight</h3>
                <p class="small muted">PDF extraction backend: checked on the next intake run. OCR is optional for scanned PDFs.</p>
              </div>
            </details>
          </div>
          <div class="table-wrap">
            <table id="document-inbox-table"><thead><tr><th></th><th>Raw PDF</th><th>Status</th><th>SHA-256</th><th>Message</th></tr></thead><tbody id="document-inbox"></tbody></table>
          </div>
          <details class="technical-details">
            <summary>File actions</summary>
            <div class="table-command-bar">
              <div class="intake-action-row command-group" aria-label="Pending raw PDF file actions">
                <span class="command-label">FILE</span>
                <button type="button" id="rename-raw-prospectus" aria-label="Rename unlinked PDF" title="Rename unlinked PDF">Rename</button>
                <button type="button" id="delete-pending-raw-prospectus" class="cmd-danger" aria-label="Delete unlinked PDF" title="Delete unlinked PDF">Delete</button>
              </div>
            </div>
          </details>
          <table id="review-queue-table" style="display:none"><thead><tr><th>Status</th><th>Source</th><th>SHA-256</th><th>Contract</th><th>Evidence</th><th>Message</th></tr></thead><tbody></tbody></table>
          </details>
        </section>
        <section id="data-subtab-review" class="subtab-panel" aria-label="Review and approve terms">
          <h2>Review &amp; approve</h2>
          <p class="chart-note">Choose an extracted bond, check highlighted terms, save edits, and approve it for pricing.</p>
          <h3>Extracted bonds</h3>
          <div class="table-wrap">
            <table id="extracted-instruments-table"><thead><tr><th></th><th>PM name</th><th>Status</th><th>Legal issuer</th><th>Currency</th><th>Maturity</th><th>Underlying</th><th>Conversion price</th><th>Source PDF</th><th>Evidence</th></tr></thead><tbody id="extracted-instruments"><tr><td colspan="10" class="muted">Loading extracted instruments...</td></tr></tbody></table>
          </div>
          <h3>Terms</h3>
          <p class="chart-note">Check highlighted items, save any changes, then approve for pricing.</p>
          <pre id="contract-review-status" class="small muted">No contract selected.</pre>
          <div id="terms-next-step" class="next-step-card" aria-live="polite"></div>
          <div class="intake-action-row command-group terms-action-group" aria-label="Terms review actions">
            <span class="command-label">TERMS</span>
            <button type="button" id="save-contract-terms" aria-label="Save changed terms" title="Save changed terms">Save changes</button>
            <button type="button" id="approve-contract-terms" class="cmd-primary" aria-label="Approve terms and continue" title="Approve terms and continue">Approve &amp; continue</button>
            <button type="button" id="load-contract-review" class="cmd-utility" aria-label="Reload terms for selected CB" title="Reload terms for selected CB">Reload</button>
            <button type="button" id="refresh-contract-economics" class="cmd-utility" aria-label="Refresh or reconcile economics from linked PDF" title="Fill missing terms or reconcile a machine-extracted yield that fails the independent cash-flow check" hidden>Refresh PDF</button>
          </div>
          <div id="term-review-groups"></div>
          <div id="contract-term-fields" class="advanced-grid" style="display:none"></div>
          <h2>Review checks</h2>
          <div id="evidence-actions">
            <p class="small muted">Select a CB to see evidence, validation blockers, and file actions.</p>
          </div>
          <details class="technical-details">
            <summary>Technical file actions</summary>
            <div class="button-row">
              <button type="button" id="detach-prospectus">Unlink source PDF from this CB</button>
            </div>
            <div class="danger-zone">
              <h3>Danger zone</h3>
              <p class="small muted">Deletion asks for confirmation and still runs all backend safety checks.</p>
              <div class="button-row">
                <button type="button" id="delete-raw-prospectus">Delete source PDF after terms approved</button>
              </div>
            </div>
          </details>
        </section>
      </section>
    </section>
    <section id="tab-audit" class="tab-panel">
      <section class="panel">
        <h2>Audit</h2>
        <p class="chart-note">Selected contract, market data, model, assumptions, row counts, and warnings.</p>
        <div id="audit-strip" class="status-strip"></div>
      </section>
    </section>
    <section id="tab-help" class="tab-panel">
      <section class="panel">
        <h2>Help</h2>
        <p class="chart-note">Two inputs are needed: approved bond terms and dated market prices.</p>
        <div class="help-card-grid">
          <article class="help-card">
            <h3>PDF = bond terms</h3>
            <p>Upload a termsheet or prospectus. The app extracts the terms; you review and approve them.</p>
            <button type="button" class="cmd-primary" data-go-tab="data-management" data-go-subtab="upload">Open data management</button>
          </article>
          <article class="help-card">
            <h3>CSV/XLSX = market prices</h3>
            <p>Data management names the required CB, stock, and FX identifiers. Upload files, match exact identifiers, then build valuation history.</p>
            <button type="button" data-go-tab="data-management" data-go-subtab="match">Check matching</button>
          </article>
        </div>
        <div class="help-topic-grid">
          <article><h3>New bond</h3><p>Upload the PDF, review highlighted terms, approve, then follow the next action shown.</p></article>
          <article><h3>Existing bond</h3><p>Search for it at the top. The app shows whether terms or market prices are missing.</p></article>
          <article><h3>Identifier status</h3><p><b>ISIN assigned</b> means the final ISIN is known. <b>ISIN pending</b> is for a new issue awaiting its ISIN.</p></article>
          <article><h3>Approval</h3><p>Saving keeps a bond in review. Approval is a separate confirmation that makes its terms eligible for pricing.</p></article>
        </div>
      </section>
    </section>
  </section>
</main>
<script>
const form = document.getElementById('pricing-form');
const nukeForm = document.getElementById('nuke-form');
const metricViews = {metric_views_json};
const supportedYieldCurveCurrencies = {supported_yield_curve_currencies_json};
const statusEl = document.getElementById('status');
const cbSelect = document.getElementById('cb-select');
const cbCommandInput = document.getElementById('cb-command-input');
const cbCommandGhost = document.getElementById('cb-command-ghost');
const cbCommandSuggestions = document.getElementById('cb-command-suggestions');
const selectedCbIdentity = document.getElementById('selected-cb-identity');
let universeItems = [];
let reviewItems = [];
let sourceItems = [];
let selectedSourceId = '';
let selectedSourceIds = new Set();
let selectedReviewItem = null;
let selectedReviewIndexes = new Set();
let lastSelectedReviewIndex = null;
let latestContractReview = null;
let contractReviewLoadGeneration = 0;
let reviewQueueLoadGeneration = 0;
let pricingLoadGeneration = 0;
let pricePreviewRunning = false;
let marketReadinessLoadGeneration = 0;
let latestMarketReadiness = null;
let latestPayload = null;
let chartView = {{}};
let responsiveChartTimer = null;
let sensitivityGeneration = 0;
let extractionRunning = false;
let sourceActionRunning = false;
let marketMatchRunning = false;
let marketMatchNotice = null;
let activeCbSuggestionIndex = -1;
let assumptionFormContractId = '';
let assumptionsLoadedContractId = '';
let assumptionFormTouched = false;
let assumptionLoadGeneration = 0;
let nukeFormContractId = '';
form.addEventListener('submit', event => {{ event.preventDefault(); loadPricing(); }});
nukeForm.addEventListener('submit', event => {{ event.preventDefault(); calculateNuke(); }});
document.getElementById('reset-nuke-anchor').addEventListener('click', () => primeNukeFromPayload(latestPayload, true));
document.getElementById('refresh-selected-cb').addEventListener('click', refreshSelectedCb);
document.getElementById('save-assumptions').addEventListener('click', saveAssumptions);
document.getElementById('edit-assumptions').addEventListener('click', () => {{
  activateTab('assumptions');
  focusDestination('assumptions');
}});
document.getElementById('complete-assumptions').addEventListener('click', () => {{
  activateTab('assumptions');
  focusDestination('assumptions');
  form.elements.volatility.focus();
}});
document.getElementById('open-data-management').addEventListener('click', () => openDataManagement());
document.getElementById('view-summary-data').addEventListener('click', () => openDataManagement());
document.getElementById('open-help').addEventListener('click', () => {{
  activateTab('help');
  focusDestination('help');
}});
cbCommandInput.addEventListener('input', () => {{ renderCbCommandSuggestions(cbCommandInput.value); updateCommandGhost(); }});
cbCommandInput.addEventListener('focus', () => {{ renderCbCommandSuggestions(cbCommandInput.value); updateCommandGhost(); }});
cbCommandInput.addEventListener('blur', () => setTimeout(() => hideCbCommandSuggestions(), 120));
cbCommandInput.addEventListener('keydown', event => {{ handleCbCommandKeydown(event); requestAnimationFrame(updateCommandGhost); }});
cbCommandInput.addEventListener('keyup', updateCommandGhost);
cbCommandInput.addEventListener('click', updateCommandGhost);
cbCommandInput.addEventListener('scroll', updateCommandGhost);
window.addEventListener('resize', () => {{ updateCommandGhost(); scheduleResponsiveChartRender(); }});
document.querySelectorAll('.tab-button').forEach(btn => btn.addEventListener('click', () => {{
  activateTab(btn.dataset.tab);
  if (btn.dataset.tab === 'data-management') void refreshActiveDataStep();
  if (btn.closest('.secondary-tabs')) focusDestination(btn.dataset.tab);
}}));
document.querySelectorAll('.subtab-button[data-data-subtab]').forEach(btn => btn.addEventListener('click', async () => {{
  const step = activateDataSubtab(btn.dataset.dataSubtab);
  if (step === 'review') {{
    if (selectedReviewItem?.contract_path) {{
      syncActiveUniverseContract(selectedReviewItem.contract_path);
      await loadSelectedContractReview();
    }}
    else await syncSelectedContractReviewFromDropdown();
  }}
  if (step === 'match') await loadMarketGenerationReadiness();
  if (step === 'library') await loadSources();
}}));
document.querySelectorAll('[data-go-tab]').forEach(btn => btn.addEventListener('click', async () => {{
  activateTab(btn.dataset.goTab);
  if (btn.dataset.goSubtab) activateDataSubtab(btn.dataset.goSubtab);
  if (normalizeDataStep(btn.dataset.goSubtab) === 'match') await loadMarketGenerationReadiness();
  focusDestination(btn.dataset.goTab, btn.dataset.goSubtab || '');
}}));
document.querySelectorAll('[data-open-data-step]').forEach(btn => btn.addEventListener('click', () => {{
  void openDataManagement(btn.dataset.openDataStep);
}}));
document.querySelectorAll('[data-focus-command]').forEach(btn => btn.addEventListener('click', () => {{
  cbCommandInput.focus();
  cbCommandInput.scrollIntoView({{behavior:'smooth', block:'center'}});
}}));
document.querySelectorAll('[data-upload-kind]').forEach(btn => btn.addEventListener('click', () => uploadSelectedFile(btn.dataset.uploadKind, btn.dataset.uploadInput, btn.dataset.uploadStatus || 'upload-status')));
document.addEventListener('click', handleSortableHeaderClick);
prepareSortableTables();
const sortableTableObserver = new MutationObserver(records => records.forEach(record => record.addedNodes.forEach(node => {{ if (node.nodeType === 1) prepareSortableTables(node); }})));
sortableTableObserver.observe(document.body, {{childList:true, subtree:true}});
const responsiveChartObserver = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(entries => {{
  if (entries.some(entry => entry.contentRect.width > 0 && entry.contentRect.height > 0)) scheduleResponsiveChartRender();
}});
document.querySelectorAll('svg.matlab-plot').forEach(svg => responsiveChartObserver?.observe(svg));
document.querySelectorAll('[data-zoom-window]').forEach(btn => btn.addEventListener('click', () => {{ resetChartWindow(btn.dataset.zoomWindow); if (latestPayload) renderCharts(latestPayload); }}));
document.getElementById('refresh-review-queue').addEventListener('click', loadReviewQueue);
document.getElementById('extract-all-prospectuses').addEventListener('click', event => extractPendingProspectuses(event, 'all'));
document.getElementById('extract-selected-prospectuses').addEventListener('click', event => extractPendingProspectuses(event, 'selected'));
document.getElementById('rename-raw-prospectus').addEventListener('click', renameSelectedRawProspectus);
document.getElementById('delete-pending-raw-prospectus').addEventListener('click', deleteSelectedPendingRawProspectus);
document.getElementById('load-contract-review').addEventListener('click', loadSelectedContractReview);
document.getElementById('refresh-contract-economics').addEventListener('click', refreshContractEconomics);
document.getElementById('save-contract-terms').addEventListener('click', saveContractTerms);
document.getElementById('approve-contract-terms').addEventListener('click', approveContractTerms);
document.getElementById('detach-prospectus').addEventListener('click', detachProspectus);
document.getElementById('delete-raw-prospectus').addEventListener('click', deleteRawProspectus);
document.getElementById('refresh-source-matches').addEventListener('click', loadSources);
document.getElementById('match-uploaded-market-data').addEventListener('click', matchUploadedMarketPrices);
document.getElementById('generate-valuation-history').addEventListener('click', handleMarketPrimaryAction);
document.getElementById('source-search').addEventListener('input', renderSources);
document.getElementById('source-kind-filter').addEventListener('change', renderSources);
document.getElementById('rename-source').addEventListener('click', renameSelectedSource);
document.getElementById('edit-source').addEventListener('click', editSelectedSource);
document.getElementById('remove-source').addEventListener('click', removeSelectedSource);
cbSelect.addEventListener('change', () => {{ applySelectedCb(); refreshActiveInstrumentTabs(); }});
function fmt(value, pct=false) {{ if (value === null || value === undefined || Number.isNaN(Number(value))) return '—'; return pct ? (Number(value)*100).toFixed(2)+'%' : Number(value).toFixed(3); }}
function fmtUnit(value, unit) {{
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
  if (unit === 'percent') return (Number(value)*100).toFixed(2)+'%';
  if (unit === 'bps') return (Number(value)*10000).toFixed(0);
  return Number(value).toFixed(3);
}}
function chartWindowKey(id, opts={{}}) {{ return opts.linkGroup || id; }}
function isChartWheelZoomGesture(event) {{ return Boolean(event.ctrlKey || event.metaKey || event.altKey); }}
function resetChartWindow(key) {{
  delete chartView[key];
  Object.keys(chartView).filter(k => k.startsWith(key + ':')).forEach(k => delete chartView[k]);
}}
function badgeClass(status) {{
  const text = String(status || '').toLowerCase();
  if (text.includes('fail') || text.includes('block') || text.includes('missing')) return 'bad';
  if (text.includes('review') || text.includes('pending') || text.includes('need')) return 'warn';
  if (text.includes('ready') || text.includes('complete') || text.includes('reviewed')) return 'good';
  return '';
}}
function reviewStatus(item) {{ return item.status || item.review_status || (item.contract_path ? 'needs_review' : 'pending_extraction'); }}
function reviewBucket(item) {{
  const status = String(reviewStatus(item)).toLowerCase();
  if (status.includes('fail') || status.includes('block')) return 'failed';
  if (status.includes('reviewed') || status === 'complete' || status === 'ready') return 'reviewed';
  if (status.includes('contract_available') || status.includes('needs_review') || status.includes('human_review') || item.contract_path) return 'needs_review';
  if (status.includes('needs_extraction') || status.includes('needs_ocr') || status.includes('pending_extraction') || status === 'pending' || !item.contract_path) return 'pending_extraction';
  return 'needs_review';
}}
const metricLineStyles = {{
  bond_price:{{label:'Market price', color:'#ff9d00'}}, fair_value:{{label:'Fair value', color:'#f2f2f2'}}, parity:{{label:'Parity', color:'#65dc8c'}}, bond_floor:{{label:'Bond floor', color:'#9a9a9a'}},
  cheapness:{{label:'Cheap/Rich', color:'#ffd000'}}, implied_volatility:{{label:'Implied vol', color:'#f2f2f2', displayUnit:'percent'}}, volatility:{{label:'Assumption vol', color:'#ff9d00', displayUnit:'percent'}},
  credit_spread:{{label:'Credit spread', color:'#c8c8c8', displayUnit:'bps'}}, stock_price:{{label:'Stock price', color:'#ffb000'}}, market_fx_rate:{{label:'Market FX', color:'#65dc8c'}},
  borrow_rate:{{label:'Borrow', color:'#ffd000', displayUnit:'percent'}}, dividend_yield:{{label:'Dividend', color:'#65dc8c', displayUnit:'percent'}}, risk_free_rate:{{label:'RF', color:'#c0c0c0', displayUnit:'percent'}},
  mid_price:{{label:'CB mid', color:'#ff9d00'}}, bid_price:{{label:'Bid', color:'#f2f2f2'}}, ask_price:{{label:'Ask', color:'#ff5c5c'}}
}};
function metricLines(groupKey, panelId=null) {{
  const group = metricViews[groupKey] || {{}};
  const unit = panelId ? (group.panels || []).find(p => p.id === panelId)?.unit : group.unit;
  const metrics = panelId ? ((group.panels || []).find(p => p.id === panelId)?.metrics || []) : (group.metrics || []);
  return metrics.map(key => ({{key, ...(metricLineStyles[key] || {{label:key, color:'#d6d6d6'}}), displayUnit:(metricLineStyles[key]?.displayUnit || (unit === 'percent' ? 'percent' : unit === 'bps' ? 'bps' : ''))}}));
}}
function esc(value) {{ return String(value ?? '').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c])); }}
const PROGRESS_COMPONENTS = {{
  extraction: {{wrapId:'extraction-progress', textId:'extraction-progress-text', idleText:'Extraction not running.'}},
  sourceLink: {{wrapId:'source-link-progress', textId:'source-link-progress-text', idleText:'Source action not running.'}},
  marketBuild: {{wrapId:'market-build-progress', textId:'market-build-progress-text', idleText:'Build not running.'}},
  pricePreview: {{wrapId:'price-preview-progress', textId:'price-preview-progress-text', idleText:'Price preview not running.'}}
}};
function setProgressBar(config, active, text='', percent=0) {{
  const wrap = document.getElementById(config.wrapId);
  const label = document.getElementById(config.textId);
  const bar = wrap ? wrap.querySelector('.progress-bar') : null;
  const pct = Math.max(0, Math.min(100, Math.round(Number(percent) || 0)));
  if (wrap) {{
    wrap.classList.toggle('active', Boolean(active));
    wrap.setAttribute('role', 'progressbar');
    wrap.setAttribute('aria-valuemin', '0');
    wrap.setAttribute('aria-valuemax', '100');
    wrap.setAttribute('aria-valuenow', String(pct));
  }}
  if (bar) bar.style.width = pct + '%';
  if (label) label.textContent = active ? `${{pct}}% — ${{text || 'Working...'}}` : (text || config.idleText || 'Idle.');
}}
function normalizeDataStep(name='') {{
  return ['upload', 'review', 'match', 'library'].includes(name) ? name : '';
}}
function activeDataStep() {{
  return document.querySelector('.subtab-button[data-data-subtab].active')?.dataset.dataSubtab || 'upload';
}}
function activateTab(name) {{
  let activeButton = null;
  document.querySelectorAll('.tab-button').forEach(btn => {{
    const active = btn.dataset.tab === name;
    btn.classList.toggle('active', active);
    if (active) btn.setAttribute('aria-current', 'page');
    else btn.removeAttribute('aria-current');
    if (active) activeButton = btn;
  }});
  document.querySelectorAll('.tab-panel').forEach(panel => panel.classList.toggle('active', panel.id === 'tab-' + name));
  const advanced = document.querySelector('.secondary-tabs');
  const advancedSummary = advanced?.querySelector('summary');
  const advancedActive = Boolean(activeButton?.closest('.secondary-tabs'));
  if (advancedSummary) {{
    advancedSummary.textContent = advancedActive ? `Advanced: ${{activeButton.textContent.trim()}}` : 'Advanced views';
    advancedSummary.classList.toggle('active', advancedActive);
  }}
  if (advanced) advanced.open = false;
  if (latestPayload) requestAnimationFrame(() => renderCharts(latestPayload));
}}
function activateDataSubtab(requestedName) {{
  const name = normalizeDataStep(requestedName) || 'upload';
  document.querySelectorAll('.subtab-button[data-data-subtab]').forEach(btn => {{
    const active = btn.dataset.dataSubtab === name;
    btn.classList.toggle('active', active);
    if (active) btn.setAttribute('aria-current', 'step');
    else btn.removeAttribute('aria-current');
  }});
  document.querySelectorAll('#tab-data-management .subtab-panel').forEach(panel => panel.classList.toggle('active', panel.id === 'data-subtab-' + name));
  return name;
}}
function focusDestination(tabName, subtabName='') {{
  const dataStep = normalizeDataStep(subtabName);
  const panel = dataStep
    ? document.getElementById('data-subtab-' + dataStep)
    : document.getElementById('tab-' + tabName);
  const target = panel?.querySelector('h2, h3') || panel;
  if (!target) return;
  target.setAttribute('tabindex', '-1');
  target.focus({{preventScroll:true}});
  target.scrollIntoView({{behavior:'smooth', block:'start'}});
}}
async function refreshActiveDataStep() {{
  const step = activeDataStep();
  if (step === 'match') await loadMarketGenerationReadiness();
  if (step === 'library') await loadSources();
  if (step === 'review' && selectedReviewItem?.contract_path) await loadSelectedContractReview();
}}
async function openDataManagement(step='') {{
  const destination = normalizeDataStep(step) || activeDataStep();
  activateTab('data-management');
  activateDataSubtab(destination);
  await refreshActiveDataStep();
  focusDestination('data-management', destination);
}}
async function openActiveTerms() {{
  const selected = selectedUniverseItem();
  if (selected?.contract_path) {{
    await syncSelectedContractReviewFromDropdown();
  }} else if (!selectedReviewItem?.contract_path) {{
    const extracted = reviewItems
      .map((item, index) => ({{item, index}}))
      .filter(entry => entry.item.contract_path);
    if (extracted.length === 1) {{
      selectedReviewItem = extracted[0].item;
      selectedReviewIndexes = new Set([extracted[0].index]);
      lastSelectedReviewIndex = extracted[0].index;
      renderReviewSelection();
    }}
  }}
  if (!selectedReviewItem?.contract_path) {{
    selectedReviewItem = null;
    selectedReviewIndexes = new Set();
    lastSelectedReviewIndex = null;
    renderReviewSelection();
    renderSelectedPendingReview();
    activateTab('data-management');
    activateDataSubtab('upload');
    focusDestination('data-management', 'upload');
    return;
  }}
  syncActiveUniverseContract(selectedReviewItem.contract_path);
  activateTab('data-management');
  activateDataSubtab('review');
  focusDestination('data-management', 'review');
  await loadSelectedContractReview();
}}
function fmtBytes(value) {{
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
  const units = ['B','KB','MB','GB']; let n = Number(value), idx = 0;
  while (n >= 1024 && idx < units.length-1) {{ n /= 1024; idx++; }}
  return `${{n.toFixed(idx ? 1 : 0)}} ${{units[idx]}}`;
}}
function prepareSortableTables(root=document) {{
  const tables = [];
  if (root.matches?.('table')) tables.push(root);
  root.querySelectorAll?.('table').forEach(table => tables.push(table));
  tables.forEach(table => {{
    const headers = Array.from(table.tHead?.rows?.[0]?.cells || []);
    headers.forEach((th, index) => {{
      const label = th.textContent.trim();
      if (!label || th.querySelector('input,button,select,textarea')) return;
      th.dataset.sortable = 'true';
      th.dataset.sortColumn = String(index);
      th.tabIndex = 0;
      th.setAttribute('role', 'button');
      th.title = `Sort by ${{label}}`;
      if (!th.hasAttribute('aria-sort')) th.setAttribute('aria-sort', 'none');
    }});
  }});
}}
function handleSortableHeaderClick(event) {{
  const th = event.target.closest('th[data-sortable="true"]');
  if (!th) return;
  const table = th.closest('table');
  if (!table) return;
  sortTableByColumn(table, Number(th.dataset.sortColumn || th.cellIndex || 0), th);
}}
document.addEventListener('keydown', event => {{
  if ((event.key === 'Enter' || event.key === ' ') && event.target?.matches?.('th[data-sortable="true"]')) {{
    event.preventDefault();
    event.target.click();
  }}
}});
function sortTableByColumn(table, columnIndex, header) {{
  const tbody = table.tBodies[0];
  if (!tbody) return;
  const current = header.dataset.sortDirection || 'none';
  const direction = current === 'asc' ? 'desc' : 'asc';
  Array.from(table.tHead?.rows?.[0]?.cells || []).forEach(th => {{
    delete th.dataset.sortDirection;
    if (th.dataset.sortable === 'true') th.setAttribute('aria-sort', 'none');
  }});
  header.dataset.sortDirection = direction;
  header.setAttribute('aria-sort', direction === 'asc' ? 'ascending' : 'descending');
  const sortableRows = Array.from(tbody.rows).filter(row => row.cells.length > columnIndex && !row.querySelector('td[colspan]'));
  const pinnedRows = Array.from(tbody.rows).filter(row => !sortableRows.includes(row));
  sortableRows.sort((a, b) => compareTableCells(a.cells[columnIndex], b.cells[columnIndex], direction));
  tbody.replaceChildren(...sortableRows, ...pinnedRows);
}}
function compareTableCells(aCell, bCell, direction) {{
  const a = parseTableSortValue(tableCellText(aCell));
  const b = parseTableSortValue(tableCellText(bCell));
  const multiplier = direction === 'asc' ? 1 : -1;
  if (a.empty && b.empty) return 0;
  if (a.empty) return 1;
  if (b.empty) return -1;
  if (a.type === b.type && a.type !== 'text') return (a.value - b.value) * multiplier;
  return a.text.localeCompare(b.text, undefined, {{numeric:true, sensitivity:'base'}}) * multiplier;
}}
function tableCellText(cell) {{
  if (!cell) return '';
  const controls = Array.from(cell.querySelectorAll('input,select,textarea')).map(el => el.value || el.textContent || '').filter(Boolean);
  return controls.concat([cell.textContent || '']).join(' ').trim();
}}
function parseTableSortValue(raw) {{
  const text = String(raw || '').replace(/\\s+/g, ' ').trim();
  if (!text || text === '—' || text.toLowerCase() === 'n/a') return {{type:'empty', value:0, text:'', empty:true}};
  if (/^\\d{{4}}-\\d{{2}}-\\d{{2}}(?:[T\\s].*)?$/.test(text)) {{
    const time = Date.parse(text);
    if (!Number.isNaN(time)) return {{type:'date', value:time, text, empty:false}};
  }}
  const size = text.match(/^(-?[\\d,.]+)\\s*(B|KB|MB|GB)$/i);
  if (size) {{
    const unit = {{B:1, KB:1024, MB:1024**2, GB:1024**3}}[size[2].toUpperCase()] || 1;
    return {{type:'number', value:Number(size[1].replace(/,/g, '')) * unit, text, empty:false}};
  }}
  const numeric = text.replace(/,/g, '').replace(/%|bps?|bp$/gi, '').trim();
  if (/^-?\\d+(?:\\.\\d+)?$/.test(numeric)) return {{type:'number', value:Number(numeric), text, empty:false}};
  return {{type:'text', value:0, text:text.toLowerCase(), empty:false}};
}}
function cbSearchText(item) {{
  const identity = item?.identity || {{}};
  return [
    item?.display_id, item?.instrument_display_name, item?.instrument_short_name, item?.label,
    item?.contract_id, item?.id, item?.canonical_id, item?.isin, item?.issuer, item?.instrument_legal_name, item?.underlying_ticker,
    identity.primary_id, identity.contract_id, identity.display_id, identity.instrument_key
  ].filter(Boolean).join(' ').toLowerCase();
}}
function cbIsin(item) {{
  const identity = item?.identity || {{}};
  return item?.isin || item?.canonical_id || identity.primary_id || '';
}}
function dataReadinessCard(label, component) {{
  component = component || {{}};
  const status = String(component.status || 'unknown').replaceAll('_', ' ');
  const latest = component.latest_date ? `until ${{component.latest_date}}` : (component.path ? component.path : '—');
  const count = component.row_count ? `${{component.row_count}} rows` : '';
  return `<div class="readiness-card"><b>${{esc(label)}} <span class="badge ${{badgeClass(status)}}">${{esc(status)}}</span></b><span>${{esc([latest, count].filter(Boolean).join(' · '))}}</span></div>`;
}}
function renderSelectedCbIdentity(item) {{
  if (!selectedCbIdentity) return;
  if (!item) {{ selectedCbIdentity.textContent = 'NO SECURITY SELECTED · SEARCH ABOVE OR OPEN TERMS > UPLOAD'; return; }}
  const data = item.data_readiness || {{components:{{}}}};
  const comps = data.components || {{}};
  const readiness = item.readiness || {{}};
  const status = (data.status || readiness.status || item.pricing_input_status || (item.available_for_pricing ? 'ready' : 'needs data')).replaceAll('_', ' ');
  const title = [cbDisplayLabel(item), cbIsin(item) || item.contract_id || 'identifier pending', item.instrument_legal_name || item.issuer || '', item.underlying_ticker || ''].filter(Boolean).join(' · ');
  selectedCbIdentity.innerHTML = `<div class="selected-identity-title"><b>${{esc(title)}}</b><span class="badge ${{badgeClass(status)}}">${{esc(status)}}</span></div><div class="readiness-grid">${{dataReadinessCard('Terms', comps.terms)}}${{dataReadinessCard('CB price', comps.cb_price_history)}}${{dataReadinessCard('Equity price', comps.equity_price_history)}}${{dataReadinessCard('FX', comps.fx_history)}}${{dataReadinessCard('Valuation rows', comps.valuation_history)}}</div>`;
}}
function cbSuggestionRows(query) {{
  const q = String(query || '').trim().toLowerCase();
  const scored = universeItems.map((item, idx) => {{
    const label = cbDisplayLabel(item);
    const isin = cbIsin(item);
    const exacts = [label, isin, item.contract_id, item.id].filter(Boolean).map(v => String(v).toLowerCase());
    const search = cbSearchText(item);
    let score = 0;
    if (!q) score = 1;
    else if (exacts.includes(q)) score = 100;
    else if (exacts.some(v => v.startsWith(q))) score = 80;
    else if (search.includes(q)) score = 40;
    return {{item, idx, score}};
  }}).filter(row => row.score > 0);
  return scored.sort((a,b) => b.score - a.score || cbDisplayLabel(a.item).localeCompare(cbDisplayLabel(b.item))).slice(0, 8);
}}
function commandCompletionCandidate(query) {{
  const typed = String(query || '');
  const q = typed.trim().toLowerCase();
  if (!q) return '';
  const rows = cbSuggestionRows(typed);
  for (const row of rows) {{
    const item = row.item;
    const candidates = [cbDisplayLabel(item), cbIsin(item), item.contract_id, item.id].filter(Boolean).map(v => String(v));
    const match = candidates.find(value => value.toLowerCase().startsWith(q) && value.length > typed.length);
    if (match) return match;
  }}
  return '';
}}
function updateCommandGhost() {{
  if (!cbCommandInput || !cbCommandGhost) return;
  const typed = cbCommandInput.value || '';
  const candidate = commandCompletionCandidate(typed);
  if (!candidate || cbCommandInput.selectionStart !== typed.length) {{
    cbCommandGhost.innerHTML = '';
    return;
  }}
  const suffix = candidate.slice(typed.length);
  cbCommandGhost.innerHTML = `<span class="command-ghost-prefix">${{esc(typed)}}</span>${{esc(suffix)}}`;
}}
function acceptCommandGhostCompletion() {{
  if (!cbCommandInput) return false;
  const typed = cbCommandInput.value || '';
  const candidate = commandCompletionCandidate(typed);
  if (!candidate || cbCommandInput.selectionStart !== typed.length) return false;
  cbCommandInput.value = candidate;
  cbCommandInput.setSelectionRange(candidate.length, candidate.length);
  renderCbCommandSuggestions(candidate);
  updateCommandGhost();
  return true;
}}
function hideCbCommandSuggestions() {{
  if (!cbCommandSuggestions) return;
  cbCommandSuggestions.classList.remove('active');
  cbCommandSuggestions.innerHTML = '';
  activeCbSuggestionIndex = -1;
  if (cbCommandInput) {{
    cbCommandInput.setAttribute('aria-expanded', 'false');
    cbCommandInput.removeAttribute('aria-activedescendant');
  }}
  updateCommandGhost();
}}
function renderCbCommandSuggestions(query='') {{
  if (!cbCommandSuggestions) return;
  const rows = cbSuggestionRows(query);
  if (!rows.length) {{
    cbCommandSuggestions.innerHTML = '<div class="command-suggestion" role="option"><strong>No matching CB</strong><span class="muted">Try ISIN or display ID</span><span></span></div>';
    cbCommandSuggestions.classList.add('active');
    cbCommandInput.setAttribute('aria-expanded', 'true');
    cbCommandInput.removeAttribute('aria-activedescendant');
    updateCommandGhost();
    return;
  }}
  if (activeCbSuggestionIndex < 0) activeCbSuggestionIndex = 0;
  if (activeCbSuggestionIndex >= rows.length) activeCbSuggestionIndex = rows.length - 1;
  cbCommandSuggestions.innerHTML = rows.map((row, pos) => {{
    const item = row.item;
    const readiness = item.readiness || {{}};
    const status = readiness.status || item.pricing_input_status || (item.available_for_pricing ? 'ready' : 'needs data');
    const meta = [cbIsin(item), item.underlying_ticker].filter(Boolean).join(' / ') || item.contract_id || '';
    return `<div id="cb-command-option-${{pos}}" class="command-suggestion ${{pos === activeCbSuggestionIndex ? 'active' : ''}}" role="option" aria-selected="${{pos === activeCbSuggestionIndex ? 'true' : 'false'}}" data-cb-index="${{row.idx}}"><strong>${{esc(cbDisplayLabel(item))}}</strong><span>${{esc(meta)}}</span><span>${{esc(status.replaceAll('_', ' '))}}</span></div>`;
  }}).join('');
  cbCommandSuggestions.querySelectorAll('[data-cb-index]').forEach(node => node.addEventListener('mousedown', event => {{
    event.preventDefault();
    selectCbByIndex(Number(node.dataset.cbIndex));
  }}));
  cbCommandSuggestions.classList.add('active');
  cbCommandInput.setAttribute('aria-expanded', 'true');
  cbCommandInput.setAttribute('aria-activedescendant', `cb-command-option-${{activeCbSuggestionIndex}}`);
  updateCommandGhost();
}}
function handleCbCommandKeydown(event) {{
  const rows = cbSuggestionRows(cbCommandInput.value);
  if (event.key === 'Tab' && acceptCommandGhostCompletion()) {{
    event.preventDefault();
    return;
  }}
  if (event.key === 'ArrowDown') {{
    event.preventDefault();
    if (!rows.length) return;
    activeCbSuggestionIndex = Math.min(rows.length - 1, activeCbSuggestionIndex + 1);
    renderCbCommandSuggestions(cbCommandInput.value);
    return;
  }}
  if (event.key === 'ArrowUp') {{
    event.preventDefault();
    if (!rows.length) return;
    activeCbSuggestionIndex = Math.max(0, activeCbSuggestionIndex < 0 ? rows.length - 1 : activeCbSuggestionIndex - 1);
    renderCbCommandSuggestions(cbCommandInput.value);
    return;
  }}
  if (event.key === 'Escape') {{ hideCbCommandSuggestions(); return; }}
  if (event.key === 'Enter') {{
    event.preventDefault();
    if (rows.length && activeCbSuggestionIndex >= 0) selectCbByIndex(rows[activeCbSuggestionIndex].idx);
    else selectCbFromCommand(cbCommandInput.value);
  }}
}}
function renderCbCommandList() {{ renderCbCommandSuggestions(cbCommandInput?.value || ''); }}
async function selectCbByIndex(idx) {{
  if (idx < 0 || idx >= universeItems.length) return;
  cbSelect.value = String(idx);
  hideCbCommandSuggestions();
  applySelectedCb();
  await refreshActiveInstrumentTabs({{source:'command'}});
}}
async function selectCbFromCommand(raw) {{
  const query = String(raw || '').trim().toLowerCase();
  if (!query) return;
  const normalized = query.split('|')[0].trim();
  let idx = universeItems.findIndex(item => [cbDisplayLabel(item), cbIsin(item), item.contract_id, item.id].filter(Boolean).some(v => String(v).toLowerCase() === query || String(v).toLowerCase() === normalized));
  if (idx < 0) idx = universeItems.findIndex(item => cbSearchText(item).includes(query) || cbSearchText(item).includes(normalized));
  if (idx < 0) {{ statusEl.textContent = `No CB matched: ${{raw}}`; renderSelectedCbIdentity(selectedUniverseItem()); return; }}
  await selectCbByIndex(idx);
}}
function selectedRiskFreeSource() {{
  const value = String(form.elements.risk_free_source?.value || 'manual').trim().toUpperCase();
  return supportedYieldCurveCurrencies.includes(value) ? value : 'manual';
}}
function defaultRiskFreeSource(selected=selectedUniverseItem()) {{
  const currency = String(selected?.risk_free_curve_currency || '').trim().toUpperCase();
  return supportedYieldCurveCurrencies.includes(currency) ? currency : 'manual';
}}
function applyRiskFreeSourceToPayload(body) {{
  const source = selectedRiskFreeSource();
  body.use_yield_curve = source !== 'manual';
  body.yield_curve_currency = source === 'manual' ? '' : source;
  return body;
}}
function updateActiveAssumptionsStrip() {{
  const strip = document.getElementById('active-assumptions-strip');
  if (!strip || !form) return;
  const f = form.elements;
  if (!selectedUniverseItem()) {{ strip.innerHTML = ''; return; }}
  const display = (value, suffix='') => String(value ?? '').trim() === '' ? 'Required' : `${{value}}${{suffix}}`;
  const riskFreeSource = selectedRiskFreeSource();
  const chips = [
    ['Vol', display(f.volatility.value, '%')],
    ['Credit', display(f.credit_spread.value, ' bps')],
    ['Borrow', display(f.borrow_rate.value, '%')],
    ['Dividend', display(f.dividend_yield.value, '%')],
    ['Model', f.model_mode.value],
    ['RF', riskFreeSource === 'manual' ? display(f.manual_rf_display?.value || f.risk_free_rate.value, '% manual') : `${{riskFreeSource}} curve`],
    ['Scenario', f.scenario_name.value || 'base'],
  ];
  strip.innerHTML = chips.map(([k,v]) => `<div class="assumption-chip"><span class="muted">${{esc(k)}}</span><b>${{esc(v)}}</b></div>`).join('');
}}
function resetAssumptionFormForContract(contractId) {{
  assumptionFormContractId = contractId || '';
  assumptionsLoadedContractId = '';
  assumptionFormTouched = false;
  assumptionLoadGeneration += 1;
  ['volatility', 'credit_spread', 'borrow_rate', 'dividend_yield', 'risk_free_rate', 'manual_rf_display'].forEach(name => {{
    if (form.elements[name]) form.elements[name].value = '';
  }});
  form.elements.steps.value = '250';
  form.elements.model_mode.value = 'tf_split_tree';
  form.elements.scenario_name.value = 'base';
  form.elements.risk_free_source.value = defaultRiskFreeSource();
  form.elements.use_history_assumptions.checked = false;
}}
function assumptionReadiness() {{
  const selected = selectedUniverseItem();
  const f = form.elements;
  const missing = [];
  [
    ['volatility', 'volatility'],
    ['credit_spread', 'credit spread'],
    ['borrow_rate', 'borrow cost'],
    ['dividend_yield', 'dividend yield'],
  ].forEach(([name, label]) => {{
    if (String(f[name]?.value || '').trim() === '') missing.push(label);
  }});
  const riskFreeSource = selectedRiskFreeSource();
  if (riskFreeSource === 'manual' && String(f.risk_free_rate?.value || '').trim() === '') {{
    missing.push('manual risk-free rate');
  }}
  return {{
    ready: Boolean(selected) && missing.length === 0,
    missing,
    riskFreeSource,
    economicCurrency: selected?.risk_free_curve_currency || '',
  }};
}}
function updateAssumptionReadiness() {{
  const readiness = assumptionReadiness();
  const selected = selectedUniverseItem();
  const economicCurrency = readiness.economicCurrency || 'unknown';
  const manual = readiness.riskFreeSource === 'manual';
  const manualField = document.getElementById('manual-risk-free-field');
  const manualInput = form.elements.manual_rf_display;
  const note = document.getElementById('risk-free-source-note');
  if (manualField) manualField.hidden = !manual;
  if (manualInput) manualInput.disabled = !manual;
  if (note) {{
    const sourceText = manual
      ? 'Enter a manual risk-free rate.'
      : `Using the ${{readiness.riskFreeSource}} government yield curve.`;
    note.textContent = `${{sourceText}} The bond's economic principal currency is ${{economicCurrency}}, not the stock trading currency. Online curves replay the current curve across historical rows; use dated row assumptions for a historical IV series.`;
  }}
  const message = readiness.ready
    ? 'Assumptions complete. Preview is ready; saving remains optional.'
    : `Required before valuation: ${{readiness.missing.join(', ') || 'select a bond'}}.`;
  const panel = document.getElementById('assumption-readiness');
  if (panel) {{
    panel.classList.toggle('good', readiness.ready);
    panel.innerHTML = `<p>${{esc(message)}}</p>`;
  }}
  const gate = document.getElementById('assumption-gate');
  if (gate) gate.hidden = !selected || readiness.ready;
  const gateMessage = document.getElementById('assumption-gate-message');
  if (gateMessage) gateMessage.textContent = message;
  const previewButton = form.querySelector('button[type="submit"]');
  const saveButton = document.getElementById('save-assumptions');
  [previewButton, saveButton].forEach(button => {{
    if (button) {{
      button.disabled = pricePreviewRunning || !readiness.ready;
      button.classList.toggle('disabled-control', button.disabled);
    }}
  }});
  updateActiveAssumptionsStrip();
  return readiness;
}}
async function loadSavedAssumptionsForSelected(selected) {{
  if (!selected?.id || assumptionsLoadedContractId === selected.id || assumptionFormTouched) return;
  const generation = ++assumptionLoadGeneration;
  try {{
    const params = new URLSearchParams({{contract_id:selected.id, scenario_name:'base'}});
    const res = await fetch('/api/assumptions?' + params.toString());
    const payload = await res.json();
    if (!res.ok) throw new Error(payload.error || res.statusText);
    if (generation !== assumptionLoadGeneration || selectedUniverseItem()?.id !== selected.id || assumptionFormTouched) return;
    const saved = payload.assumption_set;
    if (saved) {{
      form.elements.volatility.value = Number(saved.volatility) * 100;
      form.elements.credit_spread.value = Number(saved.credit_spread) * 10000;
      form.elements.borrow_rate.value = Number(saved.borrow_rate) * 100;
      form.elements.dividend_yield.value = Number(saved.dividend_yield) * 100;
      form.elements.risk_free_rate.value = Number(saved.risk_free_rate) * 100;
      form.elements.manual_rf_display.value = Number(saved.risk_free_rate) * 100;
      form.elements.steps.value = saved.steps || 250;
      form.elements.scenario_name.value = saved.scenario_name || 'base';
      const savedCurveCurrency = String(saved.yield_curve_currency || selected.risk_free_curve_currency || '').trim().toUpperCase();
      form.elements.risk_free_source.value = saved.use_yield_curve && supportedYieldCurveCurrencies.includes(savedCurveCurrency)
        ? savedCurveCurrency
        : 'manual';
    }}
    assumptionsLoadedContractId = selected.id;
    updateAssumptionReadiness();
  }} catch (err) {{
    if (generation === assumptionLoadGeneration && selectedUniverseItem()?.id === selected.id) {{
      assumptionsLoadedContractId = selected.id;
      updateAssumptionReadiness();
    }}
  }}
}}
form.addEventListener('input', () => {{
  assumptionFormTouched = true;
  updateAssumptionReadiness();
}});
form.addEventListener('change', () => {{
  assumptionFormTouched = true;
  updateAssumptionReadiness();
}});
function selectedSource() {{ return sourceItems.find(item => item.source_id === selectedSourceId) || sourceItems.find(item => selectedSourceIds.has(item.source_id)) || null; }}
function selectedSourcesForRemoval() {{
  const selected = sourceItems.filter(item => selectedSourceIds.has(item.source_id));
  if (selected.length) return selected;
  const active = selectedSource();
  return active ? [active] : [];
}}
async function confirmFileDeletions(items, deleteFile) {{
  const result = {{deleted:0, skipped:0, failures:[]}};
  for (let index = 0; index < items.length; index += 1) {{
    const item = items[index];
    const position = items.length > 1 ? ` (${{index + 1}} of ${{items.length}})` : '';
    if (!confirm(`Delete "${{item.filename}}"?${{position}}\\n\\nThis cannot be undone.`)) {{
      result.skipped += 1;
      continue;
    }}
    try {{
      await deleteFile(item);
      result.deleted += 1;
    }} catch (err) {{
      result.failures.push({{filename:item.filename, message:err.message}});
    }}
  }}
  return result;
}}
function cbDisplayLabel(item) {{
  if (!item) return 'CB';
  return item.display_id || item.instrument_display_name || item.instrument_short_name || item.label || item.contract_id || item.contract_path || item.id || 'CB';
}}
function sourceLinkedLabel(item) {{
  const meta = item.metadata || {{}};
  const labels = [];
  (item.contracts || []).forEach(contract => labels.push(cbDisplayLabel(contract)));
  (item.universe_items || []).forEach(universe => {{
    const label = cbDisplayLabel(universe);
    labels.push(universe.linked_field ? `${{label}}:${{universe.linked_field}}` : label);
  }});
  const fallback = meta.display_id || meta.instrument_display_name || meta.contract_id || '—';
  return [...new Set(labels.filter(Boolean))].join(', ') || fallback;
}}
function marketSourceMatchGroups(item) {{
  const meta = item.metadata || {{}};
  const matches = item.market_source_matches || meta.market_source_matches || {{}};
  const readIds = key => {{
    const raw = matches[key] || [];
    if (Array.isArray(raw)) return raw.map(match => String(match?.instrument_id || match?.pair || match?.identifier || '').trim()).filter(Boolean);
    return (raw.instrument_ids || raw.pairs || []).map(value => String(value || '').trim()).filter(Boolean);
  }};
  return {{
    cb: [...new Set(readIds('cb_quotes'))],
    stock: [...new Set(readIds('equities'))],
    fx: [...new Set(readIds('fx'))]
  }};
}}
function sourceIdentifier(item) {{
  const groups = marketSourceMatchGroups(item);
  const found = [
    ...groups.cb.map(value => `CB: ${{value}}`),
    ...groups.stock.map(value => `Stock: ${{value}}`),
    ...groups.fx.map(value => `FX: ${{value}}`)
  ];
  if (found.length) return found.join(' · ');
  const meta = item.metadata || {{}};
  const contract = (item.contracts || [])[0] || {{}};
  const universe = (item.universe_items || [])[0] || {{}};
  return meta.canonical_id || contract.canonical_id || universe.canonical_id || '—';
}}
function sourceStatusLabel(item) {{
  const status = String(item.status || '').toLowerCase();
  const groups = marketSourceMatchGroups(item);
  const readinessMatchesActive = latestMarketReadiness?.contract_path === activeContractPath()
    && !['loading', 'error', 'needs_identifier'].includes(latestMarketReadiness?.status);
  const cbExpected = readinessMatchesActive ? String(latestMarketReadiness?.components?.cb_quote_history?.expected_identifier || '').toUpperCase() : '';
  const stockExpected = readinessMatchesActive ? String(latestMarketReadiness?.components?.stock_history?.expected_identifier || '').toUpperCase() : '';
  const fxExpected = readinessMatchesActive ? String(latestMarketReadiness?.components?.fx_history?.expected_identifier || '').toUpperCase() : '';
  const hasExpectedIds = Boolean(cbExpected || stockExpected || fxExpected);
  const carriesMarketIds = groups.cb.length || groups.stock.length || groups.fx.length;
  const matchesSelected = groups.cb.some(value => value.toUpperCase() === cbExpected)
    || groups.stock.some(value => value.toUpperCase() === stockExpected)
    || groups.fx.some(value => value.toUpperCase() === fxExpected);
  if (carriesMarketIds && hasExpectedIds && !matchesSelected) return 'For another security';
  if (status === 'unmatched_market_source') return 'For another security';
  if (status === 'market_data_source' || status.includes('matched')) return 'Imported';
  return String(item.status || 'Unknown').replaceAll('_', ' ');
}}
async function loadSources() {{
  const status = document.getElementById('source-action-status');
  status.textContent = 'Loading source inventory...';
  try {{
    const res = await fetch('/api/sources');
    const payload = await res.json();
    if (!res.ok) throw new Error(payload.error || res.statusText);
    sourceItems = payload.sources || [];
    const validSourceIds = new Set(sourceItems.map(item => item.source_id));
    selectedSourceIds = new Set(Array.from(selectedSourceIds).filter(id => validSourceIds.has(id)));
    if (!validSourceIds.has(selectedSourceId)) selectedSourceId = Array.from(selectedSourceIds)[0] || '';
    renderSourceSummary(payload.summary || {{}});
    renderSources();
    await loadMarketGenerationReadiness();
    renderSources();
    status.textContent = `Loaded ${{sourceItems.length}} source records.`;
    return payload;
  }} catch (err) {{
    status.innerHTML = '<span class="error">Source inventory failed: ' + esc(err.message) + '</span>';
    return null;
  }}
}}
function renderSourceSummary(summary) {{
  const strip = document.getElementById('source-status-strip');
  const byKind = summary.by_kind || {{}};
  strip.innerHTML = `<div class="status-chip"><strong>${{esc(String(summary.source_count ?? 0))}}</strong><span>uploaded sources</span></div>` +
    Object.entries(byKind).map(([k,v]) => `<div class="status-chip"><strong>${{esc(v)}}</strong><span>${{esc(k)}}</span></div>`).join('');
}}
function renderSources() {{
  const tbody = document.getElementById('sources-body');
  const query = document.getElementById('source-search').value.trim().toLowerCase();
  const kind = document.getElementById('source-kind-filter').value;
  const rows = sourceItems.filter(item => (!kind || item.kind === kind) && (!query || JSON.stringify(item).toLowerCase().includes(query)));
  if (!rows.length) {{ tbody.innerHTML = '<tr><td colspan="10" class="muted">No matching data sources.</td></tr>'; renderSourceDetail(); return; }}
  tbody.innerHTML = rows.map(item => {{
    const selected = selectedSourceIds.has(item.source_id);
    const active = item.source_id === selectedSourceId;
    const linked = sourceLinkedLabel(item);
    const identifier = sourceIdentifier(item);
    const reason = item.editable?.reason || (item.linked_reference_count ? `${{item.linked_reference_count}} linked refs` : 'Safe if backend confirms');
    const selectedText = selected ? `${{item.filename}} is selected` : `Select ${{item.filename}}`;
    return `<tr data-source-id="${{esc(item.source_id)}}" class="clickable-row ${{active ? 'active ' : ''}}${{selected ? 'selected selected-row' : ''}}"><td><input type="checkbox" name="source-row" ${{selected ? 'checked' : ''}} aria-label="${{esc(selectedText)}}"></td><td>${{esc(item.filename)}}</td><td>${{esc(item.type_label || item.kind)}}</td><td><span class="badge ${{badgeClass(item.status)}}">${{esc(sourceStatusLabel(item))}}</span></td><td>${{esc(linked)}}</td><td>${{esc(identifier)}}</td><td>${{fmtBytes(item.size_bytes)}}</td><td>${{esc(item.modified_at || '—')}}</td><td>${{esc(item.directory || '')}}</td><td>${{esc(reason)}}</td></tr>`;
  }}).join('');
  tbody.querySelectorAll('tr[data-source-id]').forEach(row => {{
    row.addEventListener('click', event => selectSourceRow(row.dataset.sourceId, rows, event));
    const box = row.querySelector('input[name="source-row"]');
    if (box) box.addEventListener('click', event => selectSourceRow(row.dataset.sourceId, rows, event));
  }});
  renderSourceDetail();
}}
function selectSourceRow(sourceId, visibleRows, event={{}}) {{
  const ids = visibleRows.map(item => item.source_id);
  const fromCheckbox = event.target?.matches?.('input[name="source-row"]');
  if (event.stopPropagation && fromCheckbox) event.stopPropagation();
  if (event.shiftKey && selectedSourceId && ids.includes(selectedSourceId)) {{
    const start = ids.indexOf(selectedSourceId);
    const end = ids.indexOf(sourceId);
    ids.slice(Math.min(start, end), Math.max(start, end) + 1).forEach(id => selectedSourceIds.add(id));
  }} else {{
    if (selectedSourceIds.has(sourceId)) selectedSourceIds.delete(sourceId);
    else selectedSourceIds.add(sourceId);
  }}
  selectedSourceId = sourceId;
  renderSources();
}}
function renderSourceDetail() {{
  const detail = document.getElementById('source-detail');
  const item = selectedSource();
  const remove = document.getElementById('remove-source');
  if (!item) {{
    if (remove) {{ remove.textContent = 'Remove'; remove.setAttribute('aria-label', 'Remove selected sources'); }}
    detail.innerHTML = '<p class="small muted">Select a row to see path, hash, links, and actions.</p>';
    return;
  }}
  const removalCount = selectedSourcesForRemoval().length;
  if (remove) {{
    remove.textContent = removalCount > 1 ? `Remove selected (${{removalCount}})` : 'Remove';
    remove.setAttribute('aria-label', removalCount > 1 ? `Remove ${{removalCount}} selected sources` : 'Remove selected source');
  }}
  const selectionNote = selectedSourceIds.size > 1 ? `<p class="small warn">${{selectedSourceIds.size}} sources selected. Remove confirms each selected file; Rename and Open terms use active source ${{esc(item.filename)}}.</p>` : '';
  const contracts = (item.contracts || []).map(c => cbDisplayLabel(c)).join(', ') || '—';
  const universe = (item.universe_items || []).map(u => `${{cbDisplayLabel(u)}}:${{u.linked_field || ''}}`).join(', ') || '—';
  detail.innerHTML = `<h2>${{esc(item.filename)}}</h2>${{selectionNote}}<p class="small muted">${{esc(item.type_label || item.kind)}} · ${{esc(item.path)}} · ${{esc(item.exists ? 'exists' : 'missing')}}</p><div class="advanced-grid"><label>SHA-256 <input readonly value="${{esc(item.sha256 || 'not loaded')}}"></label><label>Linked contracts <input readonly value="${{esc(contracts)}}"></label><label>Universe links <input readonly value="${{esc(universe)}}"></label><label>SQLite source <input readonly value="${{esc(item.canonical_source?.source_of_truth || item.content_hash_status || 'not registered')}}"></label><label>Canonical source id <input readonly value="${{esc(item.canonical_source_id || '—')}}"></label><label>Action guard <input readonly value="${{esc(item.editable?.reason || 'backend will re-check before writing')}}"></label></div>`;
}}
function componentReadinessRow(label, component) {{
  const status = component?.status || 'unknown';
  const range = component?.row_count ? `${{component.first_date || '—'}} → ${{component.latest_date || '—'}} (${{component.row_count}} rows)` : 'Not found';
  const sources = (component?.source_files || []).slice(0, 3).map(src => `${{src.source_file}} [${{src.first_date || '—'}} → ${{src.latest_date || '—'}}, ${{src.row_count}} rows]`).join('; ') || 'Not found';
  const labelByStatus = {{missing:'Missing', ready:'Ready', not_required:'Not needed', available:'Ready'}};
  return `<tr><td>${{esc(label)}}</td><td><span class="badge ${{badgeClass(status)}}">${{esc(labelByStatus[status] || status)}}</span></td><td>${{esc(component?.expected_identifier || '—')}}</td><td>${{esc(range)}}</td><td>${{esc(sources)}}</td></tr>`;
}}
function marketRequirementCard(label, component) {{
  const status = String(component?.status || 'missing');
  const statusText = status === 'not_required' ? 'Not needed' : (status === 'ready' || status === 'available') ? 'Ready' : status === 'missing' ? 'Missing' : status.replaceAll('_', ' ');
  const tone = status === 'not_required' ? 'neutral' : (status === 'ready' || status === 'available') ? 'good' : 'bad';
  const identifier = status === 'not_required'
    ? 'Same currency'
    : String(component?.expected_identifier || component?.expected_pair || 'Not specified');
  const range = component?.row_count
    ? `<span class="small muted">${{esc(component.first_date || '—')}} → ${{esc(component.latest_date || '—')}}</span>`
    : '';
  return `<div class="market-requirement ${{tone}}"><span class="market-requirement-label">${{esc(label)}}</span><strong>${{esc(identifier)}}</strong><span class="badge ${{badgeClass(status)}}">${{esc(statusText)}}</span>${{range}}</div>`;
}}
function uploadedMarketIdentifiers() {{
  const result = {{cb:new Set(), stock:new Set(), fx:new Set(), fileCount:0}};
  sourceItems.forEach(item => {{
    const groups = marketSourceMatchGroups(item);
    if (groups.cb.length || groups.stock.length || groups.fx.length) result.fileCount += 1;
    groups.cb.forEach(value => result.cb.add(value));
    groups.stock.forEach(value => result.stock.add(value));
    groups.fx.forEach(value => result.fx.add(value));
  }});
  return result;
}}
function marketInputLabel(key) {{
  return {{
    cb_quote_history:'CB prices',
    stock_history:'stock prices',
    fx_history:'FX prices'
  }}[key] || String(key || '').replaceAll('_', ' ');
}}
function matchedMarketInputLabels(payload) {{
  const components = payload?.components || {{}};
  return Object.entries(components)
    .filter(([, component]) => ['ready', 'available'].includes(String(component?.status || '')))
    .map(([key]) => marketInputLabel(key));
}}
function renderMarketMatchStatus(payload) {{
  const status = document.getElementById('market-match-status');
  if (!status) return;
  const contractPath = payload?.contract_path || activeContractPath();
  if (marketMatchNotice?.contractPath === contractPath) {{
    status.className = `small ${{marketMatchNotice.error ? 'error' : marketMatchNotice.warning ? 'warn' : 'muted'}}`;
    status.textContent = marketMatchNotice.message;
    return;
  }}
  status.className = 'small muted';
  if (!contractPath) {{
    status.textContent = 'Select a bond before matching uploaded prices.';
  }} else if (payload?.status === 'loading') {{
    status.textContent = 'Checking uploaded prices.';
  }} else if (payload?.status === 'needs_identifier') {{
    status.textContent = 'Add the final ISIN under Review and approve before matching prices.';
  }} else if (matchedMarketInputLabels(payload).length) {{
    status.textContent = 'Exact matching is automatic; use this after changing the termsheet or final ISIN.';
  }} else {{
    status.textContent = 'Already uploaded prices? Match exact identifiers after adding or correcting the termsheet.';
  }}
}}
function renderMarketDataGuide(payload) {{
  const guide = document.getElementById('market-data-guide');
  const uploadInput = document.getElementById('upload-market-data');
  const uploadButton = document.getElementById('upload-market-data-button');
  const matchButton = document.getElementById('match-uploaded-market-data');
  const buildButton = document.getElementById('generate-valuation-history');
  const buildStatus = document.getElementById('market-build-status');
  if (!guide || !uploadButton || !matchButton || !buildButton || !buildStatus) return;
  renderMarketMatchStatus(payload);
  if (!payload) {{
    guide.innerHTML = '<div class="market-guide-header"><h3>Select a bond</h3><span class="badge warn">Waiting</span></div><p class="market-next-action">Select a bond to see exactly which price files it needs.</p>';
    if (uploadInput) uploadInput.disabled = false;
    uploadButton.textContent = 'Upload price files';
    uploadButton.disabled = false;
    matchButton.textContent = 'Match uploaded prices';
    matchButton.disabled = true;
    buildButton.textContent = 'Build valuation history';
    buildButton.dataset.marketAction = 'build';
    buildButton.disabled = true;
    buildStatus.textContent = 'Select a bond first.';
    return;
  }}
  if (payload.status === 'loading' || payload.status === 'error') {{
    const failed = payload.status === 'error';
    guide.innerHTML = failed
      ? `<div class="market-guide-header"><h3>Market-data check failed</h3><span class="badge bad">Error</span></div><p class="market-next-action">${{esc(payload.message || 'Readiness could not be checked.')}}</p><div class="cta-row"><button type="button" data-market-retry>Retry</button></div>`
      : '<div class="market-guide-header"><h3>Checking market data</h3><span class="badge warn">Loading</span></div><p class="market-next-action">Checking the selected bond’s required identifiers and uploaded prices.</p>';
    if (uploadInput) uploadInput.disabled = false;
    uploadButton.textContent = 'Upload price files';
    uploadButton.disabled = false;
    matchButton.textContent = marketMatchRunning ? 'Checking uploaded prices…' : 'Match uploaded prices';
    matchButton.disabled = true;
    buildButton.textContent = 'Build valuation history';
    buildButton.dataset.marketAction = 'build';
    buildButton.disabled = true;
    buildStatus.textContent = failed ? 'Retry the market-data check.' : 'Checking required prices.';
    guide.querySelector('[data-market-retry]')?.addEventListener('click', () => loadMarketGenerationReadiness());
    return;
  }}
  const components = payload.components || {{}};
  const missingCount = (payload.missing || []).length;
  const selected = selectedUniverseItem() || {{}};
  const historyReady = Boolean(selected.available_for_pricing || selected.readiness?.status === 'ready');
  const additionalDateCount = Number(payload.linked_history?.additional_date_count || 0);
  const historyNeedsUpdate = historyReady && Boolean(payload.linked_history?.can_update_from_sources);
  const fxRequired = components.fx_history?.status !== 'not_required';
  const overlapInputs = fxRequired ? 'CB, stock, and FX' : 'CB and stock';
  let title = 'Market prices needed';
  let badgeText = missingCount ? `${{missingCount}} missing input${{missingCount === 1 ? '' : 's'}}` : 'Inputs ready';
  let badgeTone = missingCount ? 'bad' : 'good';
  let nextAction = missingCount
    ? `Next: upload the missing prices. One workbook may contain more than one input.`
    : 'Next: build the valuation history.';
  if (payload.status === 'needs_identifier') {{
    title = 'Final ISIN needed';
    badgeText = 'Terms action';
    badgeTone = 'bad';
    nextAction = 'You can upload prices now. Add the final ISIN under Review and approve before matching them.';
  }} else if (payload.status === 'needs_terms_approval') {{
    title = 'Approve terms before building';
    badgeText = 'Terms action';
    badgeTone = 'bad';
    nextAction = missingCount
      ? 'Approve the extracted terms. You can upload the missing prices now.'
      : 'Approve the extracted terms, then build the valuation history.';
  }} else if (payload.status === 'no_overlap') {{
    title = 'Price dates do not overlap';
    badgeText = 'More dates needed';
    badgeTone = 'bad';
    nextAction = `Upload additional prices so the ${{overlapInputs}} dates overlap. Existing uploaded prices will be kept.`;
  }} else if (historyNeedsUpdate) {{
    title = additionalDateCount ? 'Additional price dates ready' : 'Updated prices ready';
    badgeText = additionalDateCount ? `${{additionalDateCount}} new date${{additionalDateCount === 1 ? '' : 's'}}` : 'Update available';
    badgeTone = 'good';
    nextAction = 'Update the valuation history to add new dates and refresh matching dates. Existing uploaded prices will be kept.';
  }} else if (historyReady) {{
    title = 'Valuation history ready';
    badgeText = 'Complete';
    badgeTone = 'good';
    nextAction = 'The selected bond is ready to view.';
  }}
  const requirements = payload.status === 'needs_identifier' || (historyReady && !historyNeedsUpdate && payload.status !== 'no_overlap')
    ? ''
    : `<div class="market-requirements-grid">${{marketRequirementCard('CB prices', components.cb_quote_history)}}${{marketRequirementCard('Stock prices', components.stock_history)}}${{marketRequirementCard('FX prices', components.fx_history)}}</div>`;
  const found = uploadedMarketIdentifiers();
  const requiredCb = String(components.cb_quote_history?.expected_identifier || '').toUpperCase();
  const requiredStock = String(components.stock_history?.expected_identifier || '').toUpperCase();
  const requiredFx = String(components.fx_history?.expected_identifier || '').toUpperCase();
  const otherCbs = [...found.cb].filter(value => value.toUpperCase() !== requiredCb);
  const otherStocks = [...found.stock].filter(value => value.toUpperCase() !== requiredStock);
  const otherFx = fxRequired ? [...found.fx].filter(value => value.toUpperCase() !== requiredFx) : [];
  const mismatchParts = [];
  if (otherCbs.length) mismatchParts.push(`CBs: ${{otherCbs.join(', ')}}`);
  if (otherStocks.length) mismatchParts.push(`stocks: ${{otherStocks.join(', ')}}`);
  if (otherFx.length) mismatchParts.push(`FX: ${{otherFx.join(', ')}}`);
  const mismatch = !historyReady && missingCount && mismatchParts.length
    ? `<div class="market-mismatch-note">We checked ${{found.fileCount}} uploaded market file${{found.fileCount === 1 ? '' : 's'}}; the missing inputs were not found. Found ${{esc(mismatchParts.join(' · '))}}.</div>`
    : '';
  const termsButton = ['needs_identifier', 'needs_terms_approval'].includes(payload.status)
    ? `<div class="cta-row"><button type="button" class="cmd-primary" data-market-guide-tab="data-management">${{payload.status === 'needs_identifier' ? 'Add final ISIN' : 'Review and approve terms'}}</button></div>`
    : '';
  guide.innerHTML = `<div class="market-guide-header"><h3>${{esc(title)}}</h3><span class="badge ${{badgeTone}}">${{esc(badgeText)}}</span></div>${{requirements}}<p class="market-next-action">${{esc(nextAction)}}</p>${{mismatch}}${{termsButton}}`;
  guide.querySelector('[data-market-guide-tab]')?.addEventListener('click', () => openActiveTerms());

  if (uploadInput) uploadInput.disabled = false;
  uploadButton.disabled = false;
  uploadButton.textContent = missingCount
    ? 'Upload missing prices'
    : payload.status === 'no_overlap' ? 'Upload prices for overlapping dates' : 'Upload more prices';
  matchButton.textContent = marketMatchRunning
    ? 'Checking uploaded prices…'
    : historyReady ? 'Prices already matched' : 'Match uploaded prices';
  matchButton.disabled = marketMatchRunning || payload.status === 'needs_identifier' || historyReady;
  if (historyReady && !historyNeedsUpdate) {{
    buildButton.textContent = 'View summary';
    buildButton.dataset.marketAction = 'view-summary';
    buildButton.disabled = false;
    buildStatus.textContent = 'Valuation history is already built.';
  }} else {{
    buildButton.textContent = historyNeedsUpdate ? 'Update valuation history' : 'Build valuation history';
    buildButton.dataset.marketAction = 'build';
    buildButton.disabled = sourceActionRunning || payload.status !== 'ready';
    buildStatus.textContent = payload.status === 'ready'
      ? historyNeedsUpdate
        ? additionalDateCount
          ? `Update to include ${{additionalDateCount}} additional date${{additionalDateCount === 1 ? '' : 's'}}.`
          : 'Update to refresh matching dates.'
        : 'All required prices were found.'
      : payload.status === 'no_overlap'
        ? `${{overlapInputs}} dates must overlap.`
        : payload.status === 'needs_terms_approval'
          ? 'Approve the extracted terms first.'
          : 'This unlocks when all required prices are found.';
  }}
}}
async function matchUploadedMarketPrices(options={{}}) {{
  const contractPath = String(options?.contractPath || activeContractPath() || '').trim();
  if (marketMatchRunning) return latestMarketReadiness;
  if (!contractPath) {{
    marketMatchNotice = {{contractPath:'', message:'Select a bond before matching uploaded prices.', warning:true}};
    renderMarketMatchStatus(null);
    return null;
  }}
  if (activeContractPath() !== contractPath) syncActiveUniverseContract(contractPath);
  marketMatchRunning = true;
  marketMatchNotice = {{contractPath, message:'Checking every uploaded price file for exact identifiers.'}};
  renderMarketDataGuide(latestMarketReadiness?.contract_path === contractPath ? latestMarketReadiness : {{
    status:'loading', contract_path:contractPath, components:{{}}, missing:[]
  }});
  try {{
    const sources = await loadSources();
    if (!sources || activeContractPath() !== contractPath) return null;
    const readiness = latestMarketReadiness?.contract_path === contractPath
      ? latestMarketReadiness
      : await loadMarketGenerationReadiness(contractPath);
    if (!readiness) return null;
    const matched = matchedMarketInputLabels(readiness);
    const missing = (readiness.missing || []).map(marketInputLabel);
    let message = '';
    let warning = false;
    if (readiness.status === 'needs_identifier') {{
      message = 'Add the final ISIN under Review and approve before matching uploaded prices.';
      warning = true;
    }} else if (!matched.length) {{
      message = missing.length
        ? `No exact match found. Still needed: ${{missing.join(', ')}}.`
        : 'No exact uploaded price match was found for this bond.';
      warning = true;
    }} else if (readiness.market_status === 'no_overlap' || readiness.status === 'no_overlap') {{
      message = `Matched ${{matched.join(', ')}}, but their dates do not overlap. Upload additional prices for overlapping dates; existing prices will be kept.`;
      warning = true;
    }} else if (missing.length) {{
      message = `Matched ${{matched.join(', ')}}. Still needed: ${{missing.join(', ')}}.`;
      warning = true;
    }} else if (!readiness.terms_approved) {{
      message = `Matched ${{matched.join(', ')}}. Approve the terms next.`;
    }} else {{
      message = `Matched ${{matched.join(', ')}}. Build the valuation history next.`;
    }}
    marketMatchNotice = {{contractPath, message, warning}};
    return readiness;
  }} catch (err) {{
    marketMatchNotice = {{contractPath, message:'Price matching failed: ' + String(err.message || err), error:true}};
    return null;
  }} finally {{
    marketMatchRunning = false;
    const current = latestMarketReadiness?.contract_path === contractPath ? latestMarketReadiness : null;
    if (activeContractPath() === contractPath) renderMarketDataGuide(current);
  }}
}}
function renderMarketGenerationReadiness(payload) {{
  const el = document.getElementById('market-generation-readiness');
  renderMarketDataGuide(payload);
  if (!el) return;
  if (!payload) {{ el.innerHTML = '<p class="small muted">Select a bond to inspect technical readiness.</p>'; return; }}
  if (payload.status === 'loading' || payload.status === 'error' || payload.status === 'needs_identifier') {{
    const label = payload.status === 'loading' ? 'Checking market data…' : esc(payload.message || 'Market-data readiness is unavailable.');
    el.innerHTML = `<p class="small ${{payload.status === 'error' ? 'error' : 'muted'}}">${{label}}</p>`;
    return;
  }}
  const components = payload.components || {{}};
  const overlap = payload.overlap || {{}};
  const badge = `<span class="badge ${{badgeClass(payload.status)}}">${{esc(payload.status || 'unknown')}}</span>`;
  const missing = (payload.missing || []).length ? `<p class="small warn">Missing inputs: ${{esc((payload.missing || []).join(', '))}}</p>` : '';
  const traded = payload.latest_traded_yields;
  const tradedYield = traded
    ? `<div class="status-strip" aria-label="Latest traded cash-flow yields"><div class="status-cell"><span class="muted">Latest CB mid</span><b>${{fmt(traded.mid_price)}}</b></div><div class="status-cell"><span class="muted">Market YTM</span><b>${{fmt(traded.yield_to_maturity,true)}}</b></div><div class="status-cell"><span class="muted">Yield to put</span><b>${{fmt(traded.yield_to_put,true)}}</b></div><div class="status-cell"><span class="muted">Put date</span><b>${{esc(traded.yield_to_put_date || '—')}}</b></div><div class="status-cell"><span class="muted">Quote as of</span><b>${{esc(`${{traded.as_of_date || ''}} ${{traded.as_of_time || ''}} ${{traded.dealer || ''}}`.trim())}}</b></div></div>${{traded.warning ? `<p class="small warn">${{esc(traded.warning)}}</p>` : ''}}`
    : '<p class="small muted">Market YTM and yield to put appear when a CB quote is imported.</p>';
  const overlapText = overlap.row_count ? `${{overlap.first_date || '—'}} → ${{overlap.latest_date || '—'}} (${{overlap.row_count}} overlapping rows)` : 'No overlapping dates yet';
  el.innerHTML = `<h2>Technical market-data status ${{badge}}</h2><p class="small muted">${{esc(payload.message || '')}}</p>${{missing}}<details class="technical-details"><summary>Database details</summary><p class="small">Expected identifiers come from the selected contract. Imported ranges come from ${{esc(payload.source_of_truth || 'price history database')}}.</p><div class="table-wrap"><table><thead><tr><th>Input</th><th>Status</th><th>Expected ID</th><th>Imported range</th><th>Source rows</th></tr></thead><tbody>${{componentReadinessRow('CB prices', components.cb_quote_history)}}${{componentReadinessRow('Stock prices', components.stock_history)}}${{componentReadinessRow('FX prices', components.fx_history)}}</tbody></table></div><p class="small"><b>Date overlap:</b> ${{esc(overlapText)}}</p></details>`;
  el.querySelector('h2')?.insertAdjacentHTML('afterend', tradedYield);
}}
async function loadMarketGenerationReadiness(contractPathOverride='') {{
  const contractPath = contractPathOverride || activeContractPath();
  const el = document.getElementById('market-generation-readiness');
  if (!el) return null;
  const generation = ++marketReadinessLoadGeneration;
  if (!contractPath) {{
    latestMarketReadiness = null;
    renderMarketGenerationReadiness(null);
    return null;
  }}
  latestMarketReadiness = {{
    status: 'loading',
    contract_path: contractPath,
    message: 'Checking required market prices.',
    missing: [],
    components: {{}},
    overlap: {{}}
  }};
  renderMarketGenerationReadiness(latestMarketReadiness);
  renderSources();
  try {{
    const res = await fetch('/api/market-generation-readiness' + '?contract_path=' + encodeURIComponent(contractPath));
    const payload = await res.json();
    if (!res.ok) throw new Error(payload.error || res.statusText);
    if (generation !== marketReadinessLoadGeneration || activeContractPath() !== contractPath) return null;
    latestMarketReadiness = payload;
    renderMarketGenerationReadiness(payload);
    return payload;
  }} catch (err) {{
    if (generation !== marketReadinessLoadGeneration || activeContractPath() !== contractPath) return null;
    const message = String(err.message || 'Readiness check failed');
    const payload = {{
      status: message.toLowerCase().includes('final isin') ? 'needs_identifier' : 'error',
      contract_path: contractPath,
      message,
      missing: [],
      components: {{}},
      overlap: {{}}
    }};
    latestMarketReadiness = payload;
    renderMarketGenerationReadiness(payload);
    return payload;
  }}
}}
function setSourceActionControls(active) {{
  sourceActionRunning = Boolean(active);
  ['refresh-source-matches', 'rename-source', 'edit-source', 'remove-source'].forEach(id => {{
    const btn = document.getElementById(id);
    if (btn) {{ btn.disabled = Boolean(active); btn.classList.toggle('disabled-control', Boolean(active)); }}
  }});
  const build = document.getElementById('generate-valuation-history');
  if (build) {{
    const canRun = build.dataset.marketAction === 'view-summary' || latestMarketReadiness?.status === 'ready';
    build.disabled = Boolean(active) || !canRun;
    build.classList.toggle('disabled-control', build.disabled);
  }}
}}
async function sourceAction(body, options={{}}) {{
  const status = document.getElementById('source-action-status');
  status.textContent = 'Applying source action...';
  const res = await fetch('/api/source-action', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(body)}});
  const payload = await res.json();
  if (!res.ok) throw new Error(payload.error || res.statusText);
  status.textContent = JSON.stringify(payload, null, 2);
  if (options.reload !== false) await loadSources();
  return payload;
}}
async function handleMarketPrimaryAction(event) {{
  if (event?.currentTarget?.dataset.marketAction === 'view-summary') {{
    activateTab('pm-view');
    await loadPricing();
    focusDestination('pm-view');
    return;
  }}
  await generateValuationHistory();
}}
async function generateValuationHistory() {{
  const contractPath = activeContractPath();
  const status = document.getElementById('source-action-status');
  const primaryStatus = document.getElementById('market-build-status');
  const showStatus = (message, error=false) => {{
    if (status) status.innerHTML = error ? '<span class="error">' + esc(message) + '</span>' : esc(message);
    if (primaryStatus) primaryStatus.textContent = message;
  }};
  if (sourceActionRunning) return;
  if (!contractPath) {{ showStatus('Select a bond before building its valuation history.'); return; }}
  const readiness = latestMarketReadiness?.contract_path === contractPath
    ? latestMarketReadiness
    : await loadMarketGenerationReadiness(contractPath);
  if (readiness?.status !== 'ready') {{
    const overlapInputs = readiness?.components?.fx_history?.status === 'not_required' ? 'CB and stock' : 'CB, stock, and FX';
    showStatus(readiness?.status === 'needs_terms_approval'
      ? 'Approve the extracted terms before building.'
      : readiness?.status === 'no_overlap'
        ? `${{overlapInputs}} dates must overlap before building.`
        : 'Upload every requested market-price input before building.');
    return;
  }}
  setSourceActionControls(true);
  setMarketBuildProgress(true, 'Checking the uploaded price histories.', 15);
  try {{
    const res = await fetch('/api/generate-valuation-market-history', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{contract_path:contractPath, confirm:true, confirm_overwrite:true}})}});
    const payload = await res.json();
    if (!res.ok) throw new Error(payload.error || res.statusText);
    if (payload.status !== 'ready') {{
      if (payload.readiness) {{
        latestMarketReadiness = payload.readiness;
        renderMarketGenerationReadiness(payload.readiness);
      }}
      const missing = (payload.missing || []).join(', ');
      status.textContent = payload.status === 'no_overlap'
        ? (payload.message || 'No overlapping dates across CB quote, stock, and FX histories.')
        : `${{payload.message || 'Generation blocked.'}}${{missing ? ' Missing: ' + missing + '.' : ''}}`;
      const overlapInputs = payload.readiness?.components?.fx_history?.status === 'not_required' ? 'CB and stock' : 'CB, stock, and FX';
      showStatus(payload.status === 'needs_terms_approval'
        ? 'Approve the extracted terms before building.'
        : payload.status === 'no_overlap'
          ? `${{overlapInputs}} dates do not overlap.`
          : 'Some requested prices are still missing.');
      setMarketBuildProgress(false, payload.status === 'needs_terms_approval' ? 'Build blocked: terms need approval.' : payload.status === 'no_overlap' ? 'Build blocked: dates do not overlap.' : 'Build blocked: prices are missing.', 100);
      return;
    }}
    setMarketBuildProgress(true, 'Valuation history built; refreshing the summary.', 80);
    if (payload.readiness) {{
      latestMarketReadiness = payload.readiness;
      renderMarketGenerationReadiness(payload.readiness);
    }}
    await loadSources();
    await loadUniverse({{preferredContractPaths:[contractPath], price:true}});
    await loadMarketGenerationReadiness(contractPath);
    showStatus(`Valuation history ${{payload.merge_summary ? 'updated' : 'built'}} with ${{payload.row_count}} dated row(s).`);
    setMarketBuildProgress(false, 'Valuation history is ready.', 100);
    activateTab('pm-view');
    focusDestination('pm-view');
  }} catch (err) {{
    setMarketBuildProgress(false, 'Build failed.', 100);
    showStatus('Build failed: ' + err.message, true);
  }} finally {{
    setSourceActionControls(false);
  }}
}}
async function renameSelectedSource() {{
  const item = selectedSource();
  if (!item) return;
  if (!item.editable?.rename && item.kind !== 'raw_price_history' && item.kind !== 'generated_market_history') {{ alert(item.editable?.reason || 'Rename is blocked for this source.'); return; }}
  const name = prompt('New filename in the same folder:', item.filename);
  if (!name || name === item.filename) return;
  try {{ await sourceAction({{action:'rename', kind:item.kind, source_path:item.path, new_filename:name, confirm:true}}); }} catch (err) {{ document.getElementById('source-action-status').innerHTML = '<span class="error">' + esc(err.message) + '</span>'; }}
}}
async function removeSelectedSource() {{
  const status = document.getElementById('source-action-status');
  const items = selectedSourcesForRemoval();
  if (!items.length) {{ status.textContent = 'Select one or more sources first.'; return; }}
  const removable = items.filter(item => item.editable?.remove);
  const blocked = items.filter(item => !item.editable?.remove);
  if (!removable.length) {{
    status.textContent = blocked.map(item => `${{item.filename}}: ${{item.editable?.reason || 'removal is blocked'}}`).join('\\n');
    return;
  }}
  setSourceActionControls(true);
  try {{
    const result = await confirmFileDeletions(removable, item => sourceAction(
      {{action:'remove', kind:item.kind, source_path:item.path, typed_confirmation:item.filename, confirm:true}},
      {{reload:false}}
    ));
    blocked.forEach(item => result.failures.push({{filename:item.filename, message:item.editable?.reason || 'removal is blocked'}}));
    if (result.deleted) await loadSources();
    const parts = [`Deleted ${{result.deleted}} file(s).`];
    if (result.skipped) parts.push(`Skipped ${{result.skipped}}.`);
    if (result.failures.length) parts.push(`Could not delete ${{result.failures.length}}: ${{result.failures.map(item => item.filename).join(', ')}}.`);
    status.textContent = parts.join(' ');
  }} finally {{
    setSourceActionControls(false);
  }}
}}
async function editSelectedSource() {{
  const item = selectedSource();
  if (!item) return;
  if (item.kind !== 'contract') {{ alert('Open terms is available only for contract JSON sources. Use Rename for source files.'); return; }}
  form.elements.contract_path.value = item.path;
  const idx = universeItems.findIndex(u => u.contract_path === item.path);
  if (idx >= 0) cbSelect.value = String(idx);
  await syncSelectedContractReviewFromDropdown();
  activateTab('data-management');
  activateDataSubtab('review');
  focusDestination('data-management', 'review');
  await loadSelectedContractReview();
  document.getElementById('source-action-status').textContent = 'Opened the selected contract in Review and approve.';
}}
function invalidatePricePreview() {{
  pricingLoadGeneration += 1;
  sensitivityGeneration += 1;
  setPricePreviewControls(false);
  setPricePreviewProgress(false);
}}
function clearPricingView(message) {{
  latestPayload = null;
  sensitivityGeneration++;
  setPricePreviewControls(false);
  setPricePreviewProgress(false);
  const kpis = document.getElementById('kpis');
  if (kpis) kpis.innerHTML = `<div class="kpi"><span class="muted">Selected CB</span><b>${{esc(message || 'No valuation loaded')}}</b></div>`;
  updateActiveAssumptionsStrip();
  ['price-chart','valuation-cheapness-mini-chart','rv-cheapness-chart','rv-iv-chart','rv-credit-spread-chart','rv-stock-chart','volatility-overlay-chart','yield-curve-chart','assumptions-credit-spread-chart','assumptions-rates-chart','fx-chart','raw-quote-chart'].forEach(id => {{
    const svg = document.getElementById(id);
    if (svg) drawChartEmptyState(svg, message || 'No valuation loaded');
  }});
  ['#results-table tbody','#raw-quotes-table tbody','#sensitivity-table tbody'].forEach(selector => {{
    const tbody = document.querySelector(selector);
    if (tbody) tbody.innerHTML = '';
  }});
  const audit = document.getElementById('audit-strip');
  if (audit) audit.innerHTML = '';
  const ivDiagnostics = document.getElementById('iv-diagnostics');
  if (ivDiagnostics) {{
    ivDiagnostics.hidden = true;
    ivDiagnostics.innerHTML = '';
  }}
}}
async function refreshActiveInstrumentTabs(options={{}}) {{
  const selected = selectedUniverseItem();
  if (!selected?.contract_path) {{ clearPricingView('Search for a bond above to load its valuation.'); statusEl.textContent = 'No bond selected.'; return; }}
  const contractPath = selected.contract_path;
  statusEl.textContent = `Refreshing ${{cbDisplayLabel(selected)}} terms, market data, and valuation...`;
  await loadPricing();
  await loadSources();
  if (selectedUniverseItem()?.contract_path !== contractPath) return;
  const reviewQueueLoaded = await loadReviewQueue({{
    preferredContractPaths:[contractPath],
    requiredActiveContractPath:contractPath
  }});
  if (!reviewQueueLoaded || selectedUniverseItem()?.contract_path !== contractPath) return;
  await syncSelectedContractReviewFromDropdown();
  await loadMarketGenerationReadiness();
  if (selectedUniverseItem()?.contract_path === contractPath) {{
    const label = cbDisplayLabel(selectedUniverseItem());
    if (options.source === 'command') statusEl.textContent = `Loaded ${{label}}. Summary, Data, and Assumptions are ready to inspect.`;
  }}
}}
async function refreshSelectedCb() {{
  const selected = selectedUniverseItem();
  if (!selected?.contract_path) {{ statusEl.textContent = 'Select a CB first.'; return; }}
  statusEl.textContent = `Refreshing ${{cbDisplayLabel(selected)}} from coverage universe...`;
  await loadUniverse({{preferredContractPaths:[selected.contract_path], price:false}});
  await refreshActiveInstrumentTabs();
}}
async function loadUniverse(options={{}}) {{
  try {{
    const res = await fetch('/api/universe');
    const payload = await res.json();
    if (!res.ok) throw new Error(payload.error || res.statusText);
    const previousContractPath = form.elements.contract_path.value;
    universeItems = payload.items || [];
    cbSelect.innerHTML = universeItems.map((item, idx) => {{
      const readiness = item.readiness || {{}};
      const status = readiness.status || item.pricing_input_status || (item.available_for_pricing ? 'ready' : 'missing_market_history');
      const suffix = status === 'ready' ? '' : (status === 'missing_contract' ? ' (missing contract)' : item.data_readiness?.components?.cb_price_history?.status === 'ready' ? ' (raw quotes available; generate market history)' : ' (terms extracted; add market history)');
      return `<option value="${{idx}}" ${{item.has_contract ? '' : 'disabled'}} title="${{esc(readiness.source_of_truth || item.canonical_source?.source_of_truth || '')}}">${{esc(cbDisplayLabel(item))}}${{esc(suffix)}}</option>`;
    }}).join('') || '<option value="">No covered CBs</option>';
    const preferredContracts = new Set(options.preferredContractPaths || []);
    let selectedIndex = preferredContracts.size ? universeItems.findIndex(item => preferredContracts.has(item.contract_path)) : -1;
    if (selectedIndex < 0 && previousContractPath) selectedIndex = universeItems.findIndex(item => item.contract_path === previousContractPath);
    if (selectedIndex >= 0) {{
      cbSelect.value = String(selectedIndex);
      applySelectedCb();
      if (options.price !== false) await loadPricing();
    }} else {{
      cbSelect.value = '';
      form.elements.contract_path.value = '';
      form.elements.market_history_path.value = '';
      if (form.elements.raw_price_history_path) form.elements.raw_price_history_path.value = '';
      if (cbCommandInput) {{ cbCommandInput.value = ''; updateCommandGhost(); }}
      renderSelectedCbIdentity(null);
      updateAssumptionReadiness();
      clearPricingView('Search for a bond above to load its valuation.');
      statusEl.textContent = universeItems.length ? 'Bond library loaded. Search by ISIN, issuer, ticker, or display name.' : 'No covered bonds loaded.';
    }}
  }} catch (err) {{ statusEl.innerHTML = '<span class="error">Universe load failed: ' + esc(err.message) + '</span>'; }}
}}
function applySelectedCb() {{
  const item = universeItems[Number(cbSelect.value)];
  if (!item) {{ renderSelectedCbIdentity(null); return; }}
  invalidatePricePreview();
  contractReviewLoadGeneration += 1;
  if (latestContractReview?.contract_path !== item.contract_path) latestContractReview = null;
  form.elements.contract_path.value = item.contract_path;
  form.elements.market_history_path.value = item.market_history_path || '';
  if (form.elements.raw_price_history_path) form.elements.raw_price_history_path.value = item.raw_price_history_path || '';
  if (assumptionFormContractId !== item.id) resetAssumptionFormForContract(item.id);
  if (cbCommandInput) {{ cbCommandInput.value = cbDisplayLabel(item); updateCommandGhost(); }}
  renderSelectedCbIdentity(item);
  updateAssumptionReadiness();
}}
function syncActiveUniverseContract(contractPath) {{
  const path = String(contractPath || '').trim();
  if (!path) return null;
  const index = universeItems.findIndex(item => item.contract_path === path);
  if (index >= 0) {{
    cbSelect.value = String(index);
    applySelectedCb();
    if (latestPayload && !sourcePathsMatch(latestPayload?.inputs?.contract_path, path)) {{
      clearPricingView(`Open Summary to load ${{cbDisplayLabel(universeItems[index])}}.`);
    }}
    return universeItems[index];
  }}
  invalidatePricePreview();
  contractReviewLoadGeneration += 1;
  latestContractReview = null;
  cbSelect.value = '';
  form.elements.contract_path.value = path;
  form.elements.market_history_path.value = '';
  if (form.elements.raw_price_history_path) form.elements.raw_price_history_path.value = '';
  if (cbCommandInput) {{ cbCommandInput.value = activeContractLabel(); updateCommandGhost(); }}
  renderSelectedCbIdentity(null);
  updateAssumptionReadiness();
  if (latestPayload) clearPricingView('Open Summary after this bond has complete market data.');
  return null;
}}
function selectedUniverseItem() {{
  const raw = cbSelect.value;
  if (raw === '' || raw === null || raw === undefined) return null;
  const idx = Number(raw);
  if (!Number.isInteger(idx) || idx < 0) return null;
  return universeItems[idx] || null;
}}
function activeContractPath() {{
  const selected = selectedUniverseItem();
  const formPath = (form.elements.contract_path?.value || '').trim();
  return selected?.contract_path || formPath;
}}
function activeContractLabel() {{
  const selected = selectedUniverseItem();
  if (selected) return cbDisplayLabel(selected);
  const formPath = activeContractPath();
  return formPath ? formPath.split('/').pop().replace(/\\.json$/i, '') : 'selected CB';
}}
async function syncSelectedContractReviewFromDropdown() {{
  const selected = selectedUniverseItem();
  contractReviewLoadGeneration += 1;
  if (!selected?.contract_path) {{
    selectedReviewItem = null;
    selectedReviewIndexes = new Set();
    lastSelectedReviewIndex = null;
    latestContractReview = null;
    updateTermActionState();
    renderReviewSelection();
    return false;
  }}
  const idx = reviewItems.findIndex(item => item.contract_path === selected.contract_path);
  if (idx >= 0) {{
    selectedReviewItem = reviewItems[idx];
    selectedReviewIndexes = new Set([idx]);
    if (latestContractReview?.contract_path !== selected.contract_path) {{
      latestContractReview = null;
      updateTermActionState();
    }}
    renderReviewSelection();
    return true;
  }}
  selectedReviewItem = null;
  selectedReviewIndexes = new Set();
  lastSelectedReviewIndex = null;
  latestContractReview = null;
  updateTermActionState();
  renderReviewSelection();
  return false;
}}
function pricingReadinessMessage(selected) {{
  const label = cbDisplayLabel(selected);
  const readiness = selected?.readiness || {{}};
  const missing = readiness.missing || [];
  if (!selected?.has_contract || missing.includes('contract')) return `${{label}} is missing contract terms.`;
  if (selected.data_readiness?.components?.cb_price_history?.status === 'ready' && (!selected.market_history_path || missing.includes('valuation_market_history'))) return `${{label}} raw quotes linked; generate market history to create a valuation-ready CSV before pricing.`;
  if (readiness.status && readiness.status !== 'ready') return `${{label}} is not valuation-ready yet: ${{(missing.length ? missing.join(', ') : readiness.status).replaceAll('_', ' ')}}.`;
  return `${{label}} has extracted terms but needs valuation-ready market history before pricing.`;
}}
async function loadPricing() {{
  const generation = ++pricingLoadGeneration;
  sensitivityGeneration++;
  const selected = selectedUniverseItem();
  if (!selected) {{ clearPricingView('Search for a bond above to load its valuation.'); statusEl.textContent = 'No bond selected.'; return; }}
  const contractPath = selected.contract_path;
  if (selected && !selected.available_for_pricing) {{
    const message = pricingReadinessMessage(selected);
    clearPricingView(message);
    statusEl.textContent = message;
    await syncSelectedContractReviewFromDropdown();
    return;
  }}
  await loadSavedAssumptionsForSelected(selected);
  if (generation !== pricingLoadGeneration || selectedUniverseItem()?.contract_path !== contractPath) return null;
  const assumptionStatus = updateAssumptionReadiness();
  if (!assumptionStatus.ready) {{
    const message = `Pricing assumptions required: ${{assumptionStatus.missing.join(', ')}}. Open Assumptions to enter them before valuation.`;
    clearPricingView(message);
    statusEl.textContent = message;
    return null;
  }}
  const body = Object.fromEntries(new FormData(form).entries());
  body.input_units = 'display';
  applyRiskFreeSourceToPayload(body);
  body.use_history_assumptions = form.elements.use_history_assumptions.checked;
  setPricePreviewControls(true);
  setPricePreviewProgress(true, 'Pricing the base valuation rows.', 10);
  statusEl.textContent = 'Pricing preview...';
  try {{
    const res = await fetch('/api/price-preview', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(body)}});
    const payload = await res.json();
    if (!res.ok) throw new Error(payload.error || res.statusText);
    if (generation !== pricingLoadGeneration || selectedUniverseItem()?.contract_path !== contractPath) return null;
    setPricePreviewProgress(true, 'Rendering the base preview.', 50);
    latestPayload = payload;
    statusEl.textContent = `Base preview priced ${{payload.summary.priced_row_count}}/${{payload.summary.row_count}} rows. Calculating sensitivity scenarios...`;
    const sensitivityResult = await renderPayload(payload, generation, body);
    if (!sensitivityResult || generation !== pricingLoadGeneration || selectedUniverseItem()?.contract_path !== contractPath) return null;
    if (sensitivityResult.failureCount) {{
      const completion = `Base preview complete, but ${{sensitivityResult.failureCount}}/${{sensitivityResult.scenarioCount}} sensitivity scenarios failed.`;
      statusEl.textContent = completion;
      setPricePreviewProgress(true, completion, 100, 'warning');
      return payload;
    }}
    const completion = `Preview complete: ${{payload.summary.priced_row_count}}/${{payload.summary.row_count}} rows priced for ${{payload.contract.issuer}}.`;
    statusEl.textContent = completion;
    setPricePreviewProgress(true, completion, 100, 'complete');
    return payload;
  }} catch (err) {{
    if (generation === pricingLoadGeneration && selectedUniverseItem()?.contract_path === contractPath) {{
      statusEl.innerHTML = '<span class="error">' + esc(err.message) + '</span>';
      setPricePreviewProgress(true, 'Price preview failed: ' + String(err.message || err), 100, 'failed');
    }}
    return null;
  }} finally {{
    if (generation === pricingLoadGeneration) setPricePreviewControls(false);
  }}
}}
async function saveAssumptions() {{
  const selected = universeItems[Number(cbSelect.value)];
  if (!selected) return;
  const assumptionStatus = updateAssumptionReadiness();
  if (!assumptionStatus.ready) {{
    statusEl.textContent = `Complete assumptions before saving: ${{assumptionStatus.missing.join(', ')}}.`;
    return;
  }}
  const body = Object.fromEntries(new FormData(form).entries());
  body.input_units = 'display';
  body.contract_id = selected.id;
  applyRiskFreeSourceToPayload(body);
  try {{
    const res = await fetch('/api/assumptions', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(body)}});
    const payload = await res.json();
    if (!res.ok) throw new Error(payload.error || res.statusText);
    assumptionsLoadedContractId = selected.id;
    statusEl.textContent = `Saved assumption set #${{payload.assumption_set.id}} for ${{selected.id}}/${{payload.assumption_set.scenario_name}}.`;
  }} catch (err) {{ statusEl.innerHTML = '<span class="error">Save failed: ' + esc(err.message) + '</span>'; }}
}}
function latestCurveMatch(payload) {{
  const matches = payload.yield_curve?.matches || [];
  return matches.length ? matches.at(-1) : null;
}}
function renderPayload(payload, pricingGeneration, baseRequestBody) {{
  const curveMatch = latestCurveMatch(payload);
  const rfSource = payload.yield_curve?.enabled ? `${{payload.yield_curve.currency}} yield curve` : 'manual fallback';
  const curveTarget = curveMatch ? `${{curveMatch.target_date}} (${{Number(curveMatch.target_years).toFixed(2)}}y)` : '—';
  const issueYieldDifference = payload.summary.issue_yield_difference_bps;
  const hasIssueYieldDifference = issueYieldDifference !== null && issueYieldDifference !== undefined && issueYieldDifference !== '' && Number.isFinite(Number(issueYieldDifference));
  const issueYieldCheck = `${{String(payload.summary.issue_yield_status || 'unavailable').toUpperCase()}}${{hasIssueYieldDifference ? ` (${{Number(issueYieldDifference).toFixed(2)}} bp)` : ''}}`;
  document.getElementById('kpis').innerHTML = [
    ['Issuer', payload.contract.issuer],
    ['Underlying', payload.contract.underlying_ticker],
    ['Market YTM', fmt(payload.summary.latest_yield_to_maturity, true)],
    ['Yield to put', fmt(payload.summary.latest_yield_to_put, true)],
    ['Next put date', payload.summary.latest_yield_to_put_date || '—'],
    ['Prospectus YTM', fmt(payload.summary.quoted_issue_yield_to_maturity, true)],
    ['Calculated issue YTM', fmt(payload.summary.calculated_issue_yield_to_maturity, true)],
    ['Issue YTM check', issueYieldCheck],
    ['Latest IV', fmt(payload.summary.latest_implied_volatility, true)],
    ['Latest cheapness', fmt(payload.summary.latest_cheapness)],
    ['Output ccy', payload.summary.output_currency],
    ['Risk-free source', rfSource],
    ['Matched yield', fmt(payload.series.at(-1)?.risk_free_rate, true)],
    ['Curve target', curveTarget],
    ['Raw quote rows', payload.raw_quote_history?.row_count ?? 0],
    ['Warnings', payload.summary.warning_count]
  ].map(([k,v]) => `<div class="kpi"><span class="muted">${{esc(k)}}</span><b>${{esc(v)}}</b></div>`).join('');
  renderCharts(payload);
  renderImpliedVolatilityDiagnostics(payload);
  const sensitivityPromise = renderSensitivity(payload, pricingGeneration, baseRequestBody);
  renderAudit(payload);
  primeNukeFromPayload(payload);
  updateActiveAssumptionsStrip();
  document.querySelector('#results-table tbody').innerHTML = payload.series.map(r => `<tr><td>${{esc(r.date)}}</td><td>${{fmt(r.bond_price)}}</td><td>${{fmt(r.yield_to_maturity,true)}}</td><td>${{fmt(r.yield_to_put,true)}}</td><td>${{esc(r.yield_to_put_date || '—')}}</td><td>${{fmt(r.stock_price)}}</td><td>${{fmt(r.market_fx_rate)}}</td><td>${{fmt(r.fair_value)}}</td><td>${{fmt(r.parity)}}</td><td>${{fmt(r.bond_floor)}}</td><td>${{fmt(r.implied_volatility,true)}}</td><td>${{fmt(r.risk_free_rate,true)}}</td><td>${{fmtUnit(r.credit_spread,'bps')}}</td><td>${{fmt(r.borrow_rate,true)}}</td><td>${{fmt(r.dividend_yield,true)}}</td><td>${{fmt(r.cheapness)}}</td><td>${{esc(r.output_currency)}}</td><td>${{esc(r.assumption_source)}}</td><td>${{esc(r.warnings || r.error || '')}}</td></tr>`).join('');
  const rawRows = payload.raw_quote_history?.rows || [];
  document.querySelector('#raw-quotes-table tbody').innerHTML = rawRows.map(r => `<tr><td>${{esc(r.date)}}</td><td>${{esc(r.time)}}</td><td>${{esc(r.dealer)}}</td><td>${{fmt(r.bid_price)}}</td><td>${{fmt(r.ask_price)}}</td><td>${{fmt(r.mid_price)}}</td><td>${{fmt(r.stock_price)}}</td><td>${{esc(r.security)}}</td><td>${{esc(r.reference_security)}}</td><td>${{esc(r.source_row)}}</td></tr>`).join('');
  return sensitivityPromise;
}}
function primeNukeFromPayload(payload, force=false) {{
  const latest = payload?.series?.at(-1);
  if (!latest) return;
  const contractId = String(payload.contract?.id || '');
  const resetForContract = Boolean(contractId && contractId !== nukeFormContractId);
  const values = {{
    anchor_bond_price: latest.bond_price,
    anchor_stock_price: latest.stock_price,
    anchor_fx: latest.market_fx_rate,
    current_stock_price: latest.stock_price,
    current_fx: latest.market_fx_rate
  }};
  Object.entries(values).forEach(([name, value]) => {{
    const input = nukeForm.elements[name];
    if (input && Number.isFinite(Number(value)) && (force || resetForContract || input.value === '')) input.value = String(value);
  }});
  if (resetForContract) nukeForm.elements.delta.value = '';
  nukeFormContractId = contractId;
  const context = document.getElementById('nuke-context');
  if (context) context.textContent = `Anchor loaded from ${{payload.contract?.issuer || 'selected bond'}} on ${{latest.date || 'the latest valuation row'}}. Enter the anchor delta and update current market inputs.`;
  if (force || resetForContract) document.getElementById('nuke-result').innerHTML = '<p class="muted">Latest valuation row loaded. Enter delta, then calculate.</p>';
}}
async function calculateNuke() {{
  const result = document.getElementById('nuke-result');
  if (!nukeForm.reportValidity()) return;
  const body = Object.fromEntries(new FormData(nukeForm).entries());
  result.innerHTML = '<p class="muted">Calculating nuke...</p>';
  try {{
    const res = await fetch('/api/nuke', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(body)}});
    const payload = await res.json();
    if (!res.ok) throw new Error(payload.error || res.statusText);
    result.innerHTML = `<div class="kpis" aria-label="Nuke result">
      <div class="kpi"><span class="muted">Nuked bond price</span><b>${{fmt(payload.nuked_bond_price)}}</b></div>
      <div class="kpi"><span class="muted">Bond price change</span><b>${{fmt(payload.bond_price_change)}}</b></div>
      <div class="kpi"><span class="muted">FX-adjusted stock move</span><b>${{fmt(payload.stock_move_in_bond_currency)}}</b></div>
    </div>`;
  }} catch (err) {{
    result.innerHTML = '<p class="error">Nuke failed: ' + esc(err.message) + '</p>';
  }}
}}
function renderImpliedVolatilityDiagnostics(payload) {{
  const el = document.getElementById('iv-diagnostics');
  if (!el) return;
  const rows = payload.series || [];
  if (!rows.length) {{ el.hidden = true; return; }}
  const missing = rows.filter(r => r.implied_volatility === null || r.implied_volatility === undefined || !Number.isFinite(Number(r.implied_volatility)));
  if (!missing.length) {{
    el.hidden = true;
    el.classList.add('good');
    el.innerHTML = '';
    return;
  }}
  const noBondPrice = missing.filter(r => r.bond_price === null || r.bond_price === undefined || !Number.isFinite(Number(r.bond_price))).length;
  const solverWarnings = missing.filter(r => String(r.warnings || r.error || '').includes('implied_volatility')).map(r => r.warnings || r.error);
  const pricingErrors = missing.filter(r => r.error || String(r.warnings || '').includes('pricing_error')).map(r => r.error || r.warnings);
  const brackets = missing.map(row => {{
    const match = String(row.warnings || row.error || '').match(/target price\\s+([-+0-9.eE]+)\\s+outside vol bracket price range\\s+\\[([-+0-9.eE]+),\\s*([-+0-9.eE]+)\\]/);
    return match ? {{row, target:Number(match[1]), low:Number(match[2]), high:Number(match[3])}} : null;
  }}).filter(Boolean);
  const belowMinimum = brackets.filter(item => item.target < item.low).length;
  const aboveMaximum = brackets.filter(item => item.target > item.high).length;
  const belowFloor = missing.filter(row => Number.isFinite(Number(row.bond_price)) && Number.isFinite(Number(row.bond_floor)) && Number(row.bond_price) < Number(row.bond_floor) - 0.01).length;
  const parityMismatch = missing.filter(row => Number.isFinite(Number(row.bond_price)) && Number.isFinite(Number(row.parity)) && Number(row.parity) > Number(row.bond_price) * 1.5).length;
  const reasons = [];
  if (noBondPrice) reasons.push(`${{noBondPrice}} row(s): No bond market price was supplied, so IV cannot be inverted.`);
  if (belowMinimum) reasons.push(`Target bond price is outside the solver range on ${{belowMinimum}} row(s): it is below the model's minimum value even at the lowest volatility. Check FX direction/date, stock and bond currencies, conversion terms, cash-flow terms, risk-free rate, and credit spread.`);
  if (aboveMaximum) reasons.push(`Target bond price is outside the solver range on ${{aboveMaximum}} row(s): it is above the model's value at the maximum tested volatility. Check the inputs before widening the volatility bracket.`);
  if (belowFloor) reasons.push(`${{belowFloor}} row(s) trade below the modeled bond floor. Confirm the credit spread, risk-free rate, puts/redemption cash flows, and whether the market price is clean or dirty.`);
  if (parityMismatch) reasons.push(`${{parityMismatch}} row(s) have conversion parity far above the CB market price. This can be real for distressed credit, but it is also a strong FX/currency/conversion-ratio warning.`);
  if (solverWarnings.length && !brackets.length) reasons.push(`Target bond price is outside the solver range or model bracket on ${{solverWarnings.length}} row(s): ${{solverWarnings[0]}}`);
  if (pricingErrors.length) reasons.push(`Pricing failed before IV on ${{pricingErrors.length}} row(s): ${{pricingErrors[0]}}`);
  if (!reasons.length) reasons.push('Check market history rows for bond_price, stock_price, market_fx_rate, and pricing warnings.');
  el.hidden = false;
  el.classList.remove('good');
  el.innerHTML = `<b>Implied volatility unavailable for ${{missing.length}}/${{rows.length}} row(s).</b><ul>${{reasons.map(r => `<li>${{esc(r)}}</li>`).join('')}}</ul>`;
}}
function renderSensitivity(payload, pricingGeneration, baseRequestBody) {{
  const tbody = document.querySelector('#sensitivity-table tbody');
  const latest = payload.series.at(-1) || {{}};
  tbody.innerHTML = `<tr><td>Base</td><td>${{fmt(latest.volatility,true)}}</td><td>${{fmtUnit(latest.credit_spread,'bps')}}</td><td>${{fmt(latest.borrow_rate,true)}}</td><td>${{fmt(latest.dividend_yield,true)}}</td><td>${{fmt(latest.fair_value)}}</td><td>${{fmt(latest.cheapness)}}</td><td>${{fmt(latest.implied_volatility,true)}}</td><td>${{fmt(0)}}</td></tr>`;
  const generation = ++sensitivityGeneration;
  return runSensitivityGrid(payload, generation, pricingGeneration, baseRequestBody);
}}
async function runSensitivityGrid(basePayload, generation, pricingGeneration, baseRequestBody) {{
  const baseLatest = basePayload.series.at(-1) || {{}};
  const baseBody = {{...baseRequestBody}};
  const baseVol = Number(baseBody.volatility || 0), baseCs = Number(baseBody.credit_spread || 0), baseBorrow = Number(baseBody.borrow_rate || 0), baseDiv = Number(baseBody.dividend_yield || 0);
  const scenarios = [
    ['Vol -10 pts', {{volatility: Math.max(0, baseVol-10)}}], ['Vol -5 pts', {{volatility: Math.max(0, baseVol-5)}}], ['Vol +5 pts', {{volatility: baseVol+5}}], ['Vol +10 pts', {{volatility: baseVol+10}}],
    ['Spread -100 bp', {{credit_spread: Math.max(0, baseCs-100)}}], ['Spread +100 bp', {{credit_spread: baseCs+100}}], ['Spread +300 bp', {{credit_spread: baseCs+300}}],
    ['Borrow +100 bp', {{borrow_rate: baseBorrow+1}}], ['Dividend +100 bp', {{dividend_yield: baseDiv+1}}]
  ];
  const rows = [];
  let failureCount = 0;
  for (let index = 0; index < scenarios.length; index += 1) {{
    if (generation !== sensitivityGeneration || pricingGeneration !== pricingLoadGeneration) return false;
    const [name, patch] = scenarios[index];
    const body = {{...baseBody, ...patch}};
    try {{
      const res = await fetch('/api/price-preview', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(body)}});
      const payload = await res.json();
      if (!res.ok) throw new Error(payload.error || res.statusText);
      const r = payload.series.at(-1) || {{}};
      rows.push(`<tr><td>${{esc(name)}}</td><td>${{fmt(r.volatility,true)}}</td><td>${{fmtUnit(r.credit_spread,'bps')}}</td><td>${{fmt(r.borrow_rate,true)}}</td><td>${{fmt(r.dividend_yield,true)}}</td><td>${{fmt(r.fair_value)}}</td><td>${{fmt(r.cheapness)}}</td><td>${{fmt(r.implied_volatility,true)}}</td><td>${{fmt((r.fair_value ?? NaN) - (baseLatest.fair_value ?? NaN))}}</td></tr>`);
    }} catch (err) {{
      failureCount += 1;
      rows.push(`<tr><td>${{esc(name)}}</td><td colspan="8" class="error">${{esc(err.message)}}</td></tr>`);
    }}
    if (generation === sensitivityGeneration && pricingGeneration === pricingLoadGeneration) {{
      const complete = index + 1;
      const percent = 50 + Math.round((complete / scenarios.length) * 45);
      setPricePreviewProgress(true, `Calculating sensitivity scenarios (${{complete}}/${{scenarios.length}}).`, percent);
    }}
  }}
  if (generation !== sensitivityGeneration || pricingGeneration !== pricingLoadGeneration) return false;
  document.querySelector('#sensitivity-table tbody').innerHTML += rows.join('');
  return {{complete:true, failureCount, scenarioCount:scenarios.length}};
}}
function renderAudit(payload) {{
  const selected = selectedUniverseItem() || {{}};
  const latest = payload.series.at(-1) || {{}};
  document.getElementById('audit-strip').innerHTML = [
    ['Contract', payload.inputs?.contract_path || selected.contract_path || '—'],
    ['Market history', payload.inputs?.market_history_path || selected.market_history_path || '—'],
    ['Model version', payload.model_version || '—'],
    ['Rows priced', `${{payload.summary.priced_row_count}}/${{payload.summary.row_count}}`],
    ['Assumption source', latest.assumption_source || '—'],
    ['Raw quote rows', payload.raw_quote_history?.row_count ?? 0],
    ['Output currency', payload.summary.output_currency || '—'],
    ['Warnings', payload.summary.warning_count]
  ].map(([k,v]) => `<div class="status-cell"><span class="muted">${{esc(k)}}</span><b>${{esc(v)}}</b></div>`).join('');
}}
async function uploadSelectedFile(kind, inputId, statusId='upload-status') {{
  const input = document.getElementById(inputId);
  const out = document.getElementById(statusId) || document.getElementById('upload-status');
  const disclosure = out?.closest('details');
  if (disclosure) disclosure.open = true;
  const files = Array.from(input.files || []);
  if (!files.length) {{ out.textContent = 'Select one or more files first.'; return; }}
  if (kind === 'market_data_auto') marketMatchNotice = null;
  const selected = selectedUniverseItem() || {{}};
  const summaries = [];
  const failures = [];
  const uploadedProspectusPaths = [];
  let shouldReloadUniverse = false;
  let shouldPriceAfterUploads = false;
  let shouldReloadReviewQueue = false;
  for (let index = 0; index < files.length; index += 1) {{
    const file = files[index];
    out.textContent = `Uploading ${{index + 1}}/${{files.length}}: ${{file.name}}...`;
    try {{
      const contentBase64 = await fileToBase64(file);
      const body = {{kind, filename:file.name, content_base64:contentBase64, confirm:true, contract_path:form.elements.contract_path.value}};
      if (kind !== 'market_data_auto') body.contract_id = selected.id || '';
      const res = await fetch('/api/upload', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(body)}});
      const payload = await res.json();
      if (!res.ok) throw new Error(payload.error || res.statusText);
      summaries.push(`${{file.name}}\\n${{uploadResultText(payload)}}`);
      if ((kind === 'market_history_csv' || payload.detected_market_data_types?.includes('valuation_market_history')) && (payload.generated_market_history_path || payload.path)) {{
        form.elements.market_history_path.value = payload.generated_market_history_path || payload.path;
      }}
      if (kind === 'raw_price_history' && payload.path) {{
        summaries[summaries.length - 1] += '\\nRaw quote files stay in the Data library until you generate the valuation CSV.';
      }}
      if (payload.source_link) {{
        shouldReloadUniverse = true;
        shouldPriceAfterUploads = shouldPriceAfterUploads || kind === 'market_history_csv' || payload.detected_market_data_types?.includes('valuation_market_history');
      }}
      if (payload.sync_status === 'market_sources_imported') {{ shouldReloadUniverse = true; }}
      if (kind === 'prospectus') {{
        shouldReloadReviewQueue = true;
        uploadedProspectusPaths.push(payload.path || `data/raw/prospectuses/${{file.name}}`);
      }}
    }} catch (err) {{
      failures.push(`${{file.name}}\\nUpload failed: ${{err.message}}`);
    }}
  }}
  out.textContent = summaries.concat(failures).join('\\n\\n') || 'No files uploaded.';
  if (shouldReloadUniverse) {{
    await loadUniverse({{preferredContractPaths:[selected.contract_path].filter(Boolean), price:shouldPriceAfterUploads}});
    applySelectedCb();
  }}
  if (shouldReloadReviewQueue) {{
    out.textContent += '\\nExtracting uploaded PDF…';
    await loadReviewQueue({{preferredSourcePaths:uploadedProspectusPaths}});
    if (selectedSourcePaths().length) {{
    const extractionResult = await extractPendingProspectuses(null, 'selected');
      const extractionNeedsAttention = Number(extractionResult?.payload?.needs_extraction || 0)
        + Number(extractionResult?.payload?.failed || 0);
      if (extractionNeedsAttention) document.getElementById('pdf-extraction-queue').open = true;
      if (extractionResult?.createdPaths?.length && extractionNeedsAttention) {{
        const uploadFailureText = failures.length ? ` ${{failures.length}} upload(s) also failed.` : '';
        out.textContent = `Uploaded ${{summaries.length}} PDF(s). ${{extractionResult.createdPaths.length}} bond(s) are ready; ${{extractionNeedsAttention}} PDF(s) need intervention below.${{uploadFailureText}}`;
      }} else if (extractionResult?.createdPaths?.length) {{
        out.textContent = failures.length
          ? `Uploaded and extracted ${{summaries.length}} PDF(s). ${{failures.length}} upload(s) failed.`
          : `Uploaded and extracted ${{summaries.length}} PDF(s). Review the highlighted terms.`;
      }} else {{
        const uploadFailureText = failures.length ? ` ${{failures.length}} upload(s) also failed.` : '';
        out.textContent = `Uploaded ${{summaries.length}} PDF(s), but terms still need attention. See the extraction message below.${{uploadFailureText}}`;
      }}
    }} else if (selectedReviewItem?.contract_path) {{
      activateTab('data-management');
      activateDataSubtab('review');
      await loadSelectedContractReview();
      const uploadFailureText = failures.length ? ` ${{failures.length}} upload(s) also failed.` : '';
      out.textContent = `This PDF was already extracted. Review the highlighted terms.${{uploadFailureText}}`;
    }}
  }}
  input.value = '';
  await loadSources();
  if (kind === 'market_data_auto' && summaries.length) {{
    activateTab('data-management');
    activateDataSubtab('match');
    focusDestination('data-management', 'match');
  }}
}}
function uploadResultText(payload) {{
  const lines = [payload.message || 'Upload complete.'];
  if (payload.detected_market_data_types?.length) lines.push(`Detected: ${{payload.detected_market_data_types.join(', ')}}`);
  if (payload.market_data_breakdown) lines.push(`Breakdown: CB quotes ${{payload.market_data_breakdown.cb_quote_rows || 0}}, equity ${{payload.market_data_breakdown.equity_points || 0}}, FX ${{payload.market_data_breakdown.fx_points || 0}}, other ${{payload.market_data_breakdown.other_market_data_points || 0}}, valuation rows ${{payload.market_data_breakdown.valuation_rows || 0}}.`);
  if (payload.row_count !== undefined && !payload.market_data_breakdown) lines.push(`Imported rows: ${{payload.row_count}}`);
  const warnings = payload.warnings || [];
  if (warnings.length) lines.push(`Warning: ${{warnings.at(-1)}}`);
  return lines.slice(0, 3).join('\\n');
}}
function fileToBase64(file) {{
  return new Promise((resolve, reject) => {{
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(',')[1] || '');
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  }});
}}
function instrumentLabel(item) {{ return item.instrument_display_name || item.contract_id || item.contract_path || 'Extracted CB'; }}
function itemSourceLabel(item) {{ return item.source_filename || item.source_path || item.source_file || 'Unknown source'; }}
function reviewItemSourcePath(item) {{
  return item?.source_path || item?.source_file || (item?.source_filename ? `data/raw/prospectuses/${{item.source_filename}}` : '');
}}
function normalizedSourcePath(value) {{
  return String(value || '').replaceAll('\\\\', '/').replace(/^\\.\\//, '').toLowerCase();
}}
function sourcePathsMatch(left, right) {{
  const a = normalizedSourcePath(left);
  const b = normalizedSourcePath(right);
  if (!a || !b) return false;
  if (a === b || a.endsWith('/' + b) || b.endsWith('/' + a)) return true;
  return a.split('/').at(-1) === b.split('/').at(-1);
}}
function itemSourceKey(item) {{ return normalizedSourcePath(reviewItemSourcePath(item)) || 'unknown-source'; }}
function extractionPercentFromPayload(payload) {{
  const scanned = Number(payload?.scanned || 0);
  if (!scanned) return 100;
  const completed = Number(payload.created_contracts || 0) + Number(payload.duplicates || 0) + Number(payload.needs_extraction || 0) + Number(payload.failed || 0);
  return Math.max(0, Math.min(100, Math.round((completed / scanned) * 100)));
}}
async function loadReviewQueue(options={{}}) {{
  const generation = ++reviewQueueLoadGeneration;
  const tbody = document.querySelector('#review-queue-table tbody');
  const inbox = document.getElementById('document-inbox');
  const stripEl = document.getElementById('prospectus-status-strip');
  tbody.innerHTML = '<tr><td colspan="6">Loading...</td></tr>';
  if (inbox) inbox.innerHTML = '<tr><td colspan="5" class="muted">Loading document inbox...</td></tr>';
  try {{
    const res = await fetch('/api/review-queue');
    const payload = await res.json();
    if (!res.ok) throw new Error(payload.error || res.statusText);
    if (generation !== reviewQueueLoadGeneration) return false;
    if (
      options.requiredActiveContractPath
      && selectedUniverseItem()?.contract_path !== options.requiredActiveContractPath
    ) return false;
    reviewItems = payload.items || [];
    const preferredContracts = new Set(options.preferredContractPaths || []);
    const preferredSources = options.preferredSourcePaths || [];
    let selectedIndex = 0;
    let preferredIndexes = [];
    if (preferredSources.length) {{
      preferredIndexes = reviewItems
        .map((item, index) => ({{item, index}}))
        .filter(entry => preferredSources.some(path => sourcePathsMatch(reviewItemSourcePath(entry.item), path)))
        .map(entry => entry.index);
      if (preferredIndexes.length) selectedIndex = preferredIndexes[0];
    }} else if (preferredContracts.size) {{
      const preferredIndex = reviewItems.findIndex(item => preferredContracts.has(item.contract_path));
      if (preferredIndex >= 0) selectedIndex = preferredIndex;
    }} else {{
      const firstNeedsReview = reviewItems.findIndex(item => item.contract_path && reviewBucket(item) === 'needs_review');
      if (firstNeedsReview >= 0) selectedIndex = firstNeedsReview;
    }}
    selectedReviewItem = reviewItems[selectedIndex] || null;
    selectedReviewIndexes = preferredIndexes.length
      ? new Set(preferredIndexes)
      : (selectedReviewItem ? new Set([selectedIndex]) : new Set());
    lastSelectedReviewIndex = selectedReviewItem ? selectedIndex : null;
    const counts = reviewItems.reduce((acc, item) => {{ const bucket = reviewBucket(item); acc[bucket] = (acc[bucket] || 0) + 1; return acc; }}, {{}});
    const countCards = [
      ['Pending extraction', counts.pending_extraction || 0],
      ['Needs review', counts.needs_review || 0],
      ['Reviewed', counts.reviewed || 0],
      ['Failed/blockers', counts.failed || 0]
    ].map(([k,v]) => `<div class="status-cell"><span class="muted">${{esc(k)}}</span><b>${{esc(v)}}</b></div>`).join('');
    if (stripEl) stripEl.innerHTML = countCards;
    tbody.innerHTML = reviewItems.length ? reviewItems.map((item, idx) => `<tr class="clickable-row" data-review-index="${{idx}}"><td>${{esc(reviewStatus(item))}}</td><td>${{esc(item.source_filename || item.source_path || item.source_file || '—')}}</td><td>${{esc((item.source_sha256 || item.sha256 || '').slice(0,16))}}</td><td>${{esc(item.contract_id || item.contract_path || '—')}}</td><td>${{esc(item.evidence_status || (item.missing_required_evidence || []).join(', ') || '—')}}</td><td>${{esc(item.message || item.blocker || '')}}</td></tr>`).join('') : '<tr><td colspan="6" class="muted">No review queue items found.</td></tr>';
    if (inbox) {{
      const rawItems = reviewItems.map((item, idx) => ({{item, idx}})).filter(entry => reviewBucket(entry.item) === 'pending_extraction' || !entry.item.contract_path);
      inbox.innerHTML = rawItems.length ? rawItems.map((entry) => {{
        const item = entry.item;
        const idx = entry.idx;
        const status = reviewStatus(item);
        const source = itemSourceLabel(item);
        const evidence = item.evidence_status || (item.missing_required_evidence || []).join(', ') || item.message || item.blocker || 'Awaiting extraction';
        const checked = selectedReviewIndexes.has(idx) ? 'checked' : '';
        const selectedText = selectedReviewIndexes.has(idx) ? `${{source}} is selected for extraction` : `Select ${{source}} for extraction`;
        return `<tr class="clickable-row ${{idx === selectedIndex ? 'active ' : ''}}${{selectedReviewIndexes.has(idx) ? 'selected selected-row' : ''}}" data-review-index="${{idx}}"><td><input type="checkbox" name="raw-pdf-row" ${{checked}} aria-label="${{esc(selectedText)}}"></td><td><b>${{esc(source)}}</b><div class="small muted">Click row to toggle; Shift-click selects a range.</div></td><td><span class="badge ${{badgeClass(status)}}">${{esc(status)}}</span></td><td>${{esc((item.source_sha256 || item.sha256 || '').slice(0,16) || '—')}}</td><td>${{esc(evidence)}}</td></tr>`;
      }}).join('') : '<tr><td colspan="5" class="muted">No raw PDFs awaiting extraction. Extracted CBs appear in the instrument tab.</td></tr>';
      inbox.querySelectorAll('[data-review-index]').forEach(row => row.addEventListener('click', async event => {{
        toggleReviewSelection(Number(row.dataset.reviewIndex), event);
        await openSelectedReviewItem();
      }}));
    }}
    renderExtractedInstruments(selectedIndex);
    tbody.querySelectorAll('[data-review-index]').forEach(row => row.addEventListener('click', async event => {{ toggleReviewSelection(Number(row.dataset.reviewIndex), event); await openSelectedReviewItem(); }}));
    renderReviewSelection();
    if (selectedReviewItem?.contract_path) await loadSelectedContractReview(); else renderSelectedPendingReview();
    return true;
  }} catch (err) {{
    if (generation === reviewQueueLoadGeneration) {{
      tbody.innerHTML = `<tr><td colspan="6" class="error">${{esc(err.message)}}</td></tr>`;
      if (inbox) inbox.innerHTML = `<tr><td colspan="5" class="error">${{esc(err.message)}}</td></tr>`;
    }}
    return false;
  }}
}}
function renderExtractedInstruments(activeIndex=null) {{
  const container = document.getElementById('extracted-instruments');
  if (!container) return;
  const entries = reviewItems.map((item, idx) => ({{item, idx}})).filter(entry => entry.item.contract_path);
  if (!entries.length) {{
    container.innerHTML = '<tr><td colspan="10" class="muted">No extracted CB instruments yet. Run extraction on raw PDFs first.</td></tr>';
    return;
  }}
  const sourceCounts = entries.reduce((acc, entry) => {{ const key = itemSourceKey(entry.item); acc[key] = (acc[key] || 0) + 1; return acc; }}, {{}});
  container.innerHTML = entries.map(entry => {{
    const item = entry.item;
    const status = reviewStatus(item);
    const source = itemSourceLabel(item);
    const sourceSuffix = sourceCounts[itemSourceKey(item)] > 1 ? ` (${{sourceCounts[itemSourceKey(item)]}} CBs)` : '';
    const missing = (item.missing_required_evidence || []).length ? `${{(item.missing_required_evidence || []).length}} missing` : (item.evidence_status || 'Evidence linked');
    return `<tr class="clickable-row ${{entry.idx === activeIndex ? 'active selected-row' : ''}}" data-review-index="${{entry.idx}}"><td><input type="radio" name="instrument-row" ${{entry.idx === activeIndex ? 'checked' : ''}} aria-label="Select ${{esc(instrumentLabel(item))}}"></td><td><b>${{esc(instrumentLabel(item))}}</b><div class="small muted">${{esc(item.contract_id || item.contract_path || '')}}</div></td><td><span class="badge ${{badgeClass(status)}}">${{esc(status)}}</span></td><td>${{esc(item.issuer_legal_name || item.instrument_legal_name || '')}}</td><td>${{esc(item.currency || '')}}</td><td>${{esc(item.maturity_date || '')}}</td><td>${{esc(item.underlying_ticker || '')}}</td><td>${{esc(item.conversion_price ?? '')}}</td><td>${{esc(source + sourceSuffix)}}</td><td>${{esc(missing)}}</td></tr>`;
  }}).join('');
  container.querySelectorAll('[data-review-index]').forEach(row => row.addEventListener('click', async event => {{
    selectReviewSelection(Number(row.dataset.reviewIndex));
    await openSelectedReviewItem();
  }}));
}}
async function openSelectedReviewItem() {{
  const contractPath = selectedReviewItem?.contract_path;
  if (!contractPath) {{
    contractReviewLoadGeneration += 1;
    latestContractReview = null;
    renderSelectedPendingReview();
    return;
  }}
  syncActiveUniverseContract(contractPath);
  activateDataSubtab('review');
  await loadSelectedContractReview();
}}
function renderReviewSelection() {{
  document.querySelectorAll('[data-review-index]').forEach(el => {{
    const idx = Number(el.dataset.reviewIndex);
    el.classList.toggle('selected', selectedReviewIndexes.has(idx));
    el.classList.toggle('selected-row', selectedReviewIndexes.has(idx));
    el.classList.toggle('active', selectedReviewItem === reviewItems[idx]);
    const control = el.querySelector('input[type="checkbox"], input[type="radio"]');
    if (control) {{
      control.checked = control.type === 'radio'
        ? selectedReviewItem === reviewItems[idx]
        : selectedReviewIndexes.has(idx);
    }}
  }});
  const selectedButton = document.getElementById('extract-selected-prospectuses');
  if (selectedButton) {{
    const selectedCount = selectedSourcePaths().length;
    const selectedLabel = selectedCount ? `Extract selected PDFs (${{selectedCount}} selected)` : 'Select one or more raw PDFs before extraction';
    selectedButton.textContent = `Extract selected (${{selectedCount}})`;
    selectedButton.setAttribute('aria-label', selectedLabel);
    selectedButton.title = selectedLabel;
    selectedButton.disabled = extractionRunning || selectedCount === 0;
    selectedButton.classList.toggle('disabled-control', selectedButton.disabled);
  }}
  const deleteButton = document.getElementById('delete-pending-raw-prospectus');
  if (deleteButton) {{
    const selectedCount = selectedSourcePaths().length;
    deleteButton.textContent = selectedCount > 1 ? `Delete selected (${{selectedCount}})` : 'Delete';
    deleteButton.setAttribute('aria-label', selectedCount > 1 ? `Delete ${{selectedCount}} selected pending PDFs` : 'Delete selected pending PDF');
  }}
  updateProspectusActionState();
}}
function selectReviewSelection(index) {{
  if (!reviewItems[index]) return;
  selectedReviewIndexes = new Set([index]);
  selectedReviewItem = reviewItems[index];
  lastSelectedReviewIndex = index;
  renderReviewSelection();
}}
function toggleReviewSelection(index, event={{}}) {{
  if (!reviewItems[index]) return;
  if (event.shiftKey && lastSelectedReviewIndex !== null) {{
    const start = Math.min(lastSelectedReviewIndex, index);
    const end = Math.max(lastSelectedReviewIndex, index);
    for (let idx = start; idx <= end; idx++) selectedReviewIndexes.add(idx);
  }} else {{
    if (selectedReviewIndexes.has(index)) selectedReviewIndexes.delete(index);
    else selectedReviewIndexes.add(index);
    lastSelectedReviewIndex = index;
  }}
  selectedReviewItem = selectedReviewIndexes.has(index) ? reviewItems[index] : reviewItems[Array.from(selectedReviewIndexes)[0]] || null;
  renderReviewSelection();
}}
function selectedSourcePaths() {{
  return Array.from(selectedReviewIndexes)
    .sort((a,b) => a-b)
    .map(idx => reviewItems[idx])
    .filter(item => item && (reviewBucket(item) === 'pending_extraction' || !item.contract_path))
    .map(reviewItemSourcePath)
    .filter(Boolean);
}}
function extractionStatusEl() {{ return document.getElementById('prospectus-extraction-status') || document.getElementById('contract-review-status'); }}
function renderExtractionEnvironment(env) {{
  const el = document.getElementById('extraction-environment-card');
  if (!el || !env) return;
  const textOk = env.text_backend_available ? 'available' : 'unavailable';
  const ocrOk = env.ocr_backend_available ? 'available' : 'unavailable';
  const warn = (env.warnings || []).length ? `<p class="small warn">${{esc((env.warnings || []).join(' · '))}}</p>` : '';
  el.innerHTML = `<h2>Extraction preflight</h2><p class="small">PDF extraction backend (${{esc(env.text_backend || 'pymupdf')}}): <b>${{textOk}}</b> · OCR fallback (${{esc(env.ocr_backend || 'pytesseract')}}): <b>${{ocrOk}}</b></p><p class="small muted">${{esc(env.recommended_runtime || 'Use the project .venv for prospectus extraction.')}}</p>${{warn}}`;
}}
function setExtractionProgress(active, text='', percent=0) {{
  setProgressBar(PROGRESS_COMPONENTS.extraction, active, text, percent);
}}
function setSourceLinkProgress(active, text='', percent=0) {{
  setProgressBar(PROGRESS_COMPONENTS.sourceLink, active, text, percent);
}}
function setMarketBuildProgress(active, text='', percent=0) {{
  setProgressBar(PROGRESS_COMPONENTS.marketBuild, active, text, percent);
  setSourceLinkProgress(active, text, percent);
}}
function setPricePreviewProgress(active, text='', percent=0, state='') {{
  setProgressBar(PROGRESS_COMPONENTS.pricePreview, active, text, percent);
  const wrap = document.getElementById(PROGRESS_COMPONENTS.pricePreview.wrapId);
  if (wrap) {{
    wrap.classList.toggle('complete', state === 'complete');
    wrap.classList.toggle('warning', state === 'warning');
    wrap.classList.toggle('failed', state === 'failed');
  }}
}}
function setPricePreviewControls(active) {{
  const running = Boolean(active);
  pricePreviewRunning = running;
  form.querySelectorAll('input:not([type="hidden"]), select').forEach(control => {{
    control.disabled = running;
  }});
  const previewButton = form.querySelector('button[type="submit"]');
  const saveButton = document.getElementById('save-assumptions');
  if (running) {{
    [previewButton, saveButton].forEach(button => {{
      if (button) button.disabled = true;
    }});
  }} else {{
    updateAssumptionReadiness();
  }}
  [previewButton, saveButton].forEach(button => {{
    if (button) button.classList.toggle('disabled-control', button.disabled);
  }});
  form.setAttribute('aria-busy', String(running));
}}
function setExtractionControls(active) {{
  const extractAll = document.getElementById('extract-all-prospectuses');
  const extractSelected = document.getElementById('extract-selected-prospectuses');
  const refresh = document.getElementById('refresh-review-queue');
  const inboxPane = document.querySelector('.document-inbox');
  [extractAll, refresh].forEach(btn => {{ if (btn) {{ btn.disabled = Boolean(active); btn.classList.toggle('disabled-control', Boolean(active)); }} }});
  if (extractSelected) {{
    extractSelected.disabled = Boolean(active) || selectedSourcePaths().length === 0;
    extractSelected.classList.toggle('disabled-control', extractSelected.disabled);
  }}
  if (inboxPane) inboxPane.setAttribute('aria-busy', active ? 'true' : 'false');
}}
async function extractPendingProspectuses(event=null, mode='all') {{
  event?.preventDefault?.();
  event?.stopPropagation?.();
  if (extractionRunning) return;
  const body = {{confirm:true}};
  const paths = mode === 'selected'
    ? selectedSourcePaths()
    : reviewItems
        .filter(item => reviewBucket(item) === 'pending_extraction' || !item.contract_path)
        .map(reviewItemSourcePath)
        .filter(Boolean);
  if (!paths.length) {{
    const status = extractionStatusEl();
    if (status) status.textContent = mode === 'selected'
      ? 'Select at least one prospectus first.'
      : 'No PDFs are waiting for extraction.';
    return;
  }}
  body.source_paths = paths;
  extractionRunning = true;
  setExtractionControls(true);
  const status = extractionStatusEl();
  if (status) status.textContent = mode === 'selected' ? `Extracting ${{body.source_paths.length}} PDF(s)…` : `Extracting ${{body.source_paths.length}} pending PDF(s)…`;
  setExtractionProgress(true, 'Reading PDF terms.', 5);
  try {{
    setExtractionProgress(true, 'Reading PDF terms.', 35);
    const res = await fetch('/api/prospectus-intake', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(body)}});
    setExtractionProgress(true, 'Validating evidence and writing review queue.', 85);
    const payload = await res.json();
    if (!res.ok) throw new Error(payload.error || res.statusText);
    renderExtractionEnvironment(payload.extraction_environment);
    const processedItems = Array.isArray(payload.processed_items) ? payload.processed_items : (payload.items || []);
    const createdPaths = Array.from(new Set(processedItems.filter(item => item.contract_path).map(item => item.contract_path)));
    const interventionCount = Number(payload.needs_extraction || 0) + Number(payload.failed || 0);
    const pct = extractionPercentFromPayload(payload);
    setExtractionProgress(true, `Found ${{createdPaths.length}} bond(s).`, pct);
    await loadUniverse({{preferredContractPaths: createdPaths, price: false}});
    if (createdPaths.length === 1) {{
      await matchUploadedMarketPrices({{contractPath:createdPaths[0]}});
    }} else {{
      await loadSources();
    }}
    await loadReviewQueue({{preferredContractPaths: createdPaths}});
    activateTab('data-management');
    if (createdPaths.length === 1) {{
      activateDataSubtab('review');
      await loadSelectedContractReview();
      focusDestination('data-management', 'review');
    }} else if (createdPaths.length > 1) {{
      activateDataSubtab('review');
      focusDestination('data-management', 'review');
    }}
    if (status) status.textContent = createdPaths.length && interventionCount
      ? `${{createdPaths.length}} bond(s) ready; ${{interventionCount}} PDF(s) still need OCR or manual review.`
      : createdPaths.length === 1
      ? 'Terms extracted. Review the highlighted items.'
      : createdPaths.length > 1
        ? `Extraction complete. ${{createdPaths.length}} bonds found; choose one to review.`
        : Number(payload.needs_extraction || 0) > 0
          ? 'The PDF needs OCR or a working text-extraction backend.'
          : Number(payload.failed || 0) > 0
            ? 'Terms could not be recognized. Manual review is required.'
            : 'No new bond terms were found.';
    setExtractionProgress(false, 'Extraction complete.', 100);
    return {{payload, createdPaths}};
  }} catch (err) {{
    if (status) status.innerHTML = '<span class="error">Extraction failed: ' + esc(err.message) + '</span>';
    setExtractionProgress(false, 'Extraction failed.', 100);
    return null;
  }}
  finally {{ extractionRunning = false; setExtractionControls(false); }}
}}
function selectedRawSourcePath() {{
  return reviewItemSourcePath(selectedReviewItem);
}}
function selectedPendingRawProspectuses() {{
  const seen = new Set();
  return Array.from(selectedReviewIndexes)
    .sort((a,b) => a-b)
    .map(index => reviewItems[index])
    .filter(item => item && !item.contract_path)
    .map(item => {{
      const source = reviewItemSourcePath(item);
      const filename = item.source_filename || source.split('/').at(-1) || '';
      return {{source, filename}};
    }})
    .filter(item => {{
      if (!item.source || !item.filename || seen.has(item.source)) return false;
      seen.add(item.source);
      return true;
    }});
}}
async function renameSelectedRawProspectus() {{
  const status = document.getElementById('contract-review-status');
  const source = selectedRawSourcePath();
  if (!source) {{ status.textContent = 'Select a raw prospectus row first.'; return; }}
  const current = selectedReviewItem?.source_filename || source.split('/').at(-1) || '';
  const newName = prompt('New raw prospectus filename (.pdf):', current);
  if (!newName || newName === current) return;
  try {{
    const res = await fetch('/api/raw-prospectus-action', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{action:'rename', source_path:source, new_filename:newName, confirm:true}})}});
    const payload = await res.json();
    if (!res.ok) throw new Error(payload.error || res.statusText);
    status.textContent = `Renamed raw PDF to ${{payload.new_path}}.`;
    await loadReviewQueue();
    await loadSources();
  }} catch (err) {{ status.innerHTML = '<span class="error">Rename blocked: ' + esc(err.message) + '</span>'; }}
}}
async function deleteSelectedPendingRawProspectus() {{
  const status = extractionStatusEl();
  const items = selectedPendingRawProspectuses();
  if (!items.length) {{ status.textContent = 'Select one or more pending raw PDFs first.'; return; }}
  const result = await confirmFileDeletions(items, async item => {{
    const res = await fetch('/api/raw-prospectus-action', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{action:'delete_raw', source_path:item.source, confirm:true, typed_confirmation:item.filename}})}});
    const payload = await res.json();
    if (!res.ok) throw new Error(payload.error || res.statusText);
    if (!payload.raw_deleted) throw new Error('backend did not delete the file');
  }});
  if (result.deleted) {{
    await loadReviewQueue();
    await loadSources();
  }}
  const parts = [`Deleted ${{result.deleted}} pending PDF(s).`];
  if (result.skipped) parts.push(`Skipped ${{result.skipped}}.`);
  if (result.failures.length) parts.push(`Could not delete ${{result.failures.length}}: ${{result.failures.map(item => item.filename).join(', ')}}.`);
  status.textContent = parts.join(' ');
}}
function updateProspectusActionState() {{
  const linked = Boolean(selectedReviewItem?.contract_path);
  const hasRaw = Boolean(selectedRawSourcePath());
  const pendingDeleteCount = selectedSourcePaths().length;
  const rename = document.getElementById('rename-raw-prospectus');
  const deletePending = document.getElementById('delete-pending-raw-prospectus');
  const detach = document.getElementById('detach-prospectus');
  const deleteReviewed = document.getElementById('delete-raw-prospectus');
  if (rename) {{ rename.disabled = linked || !hasRaw; rename.classList.toggle('disabled-control', rename.disabled); rename.title = linked ? 'Linked/reviewed PDFs require contract-aware audited actions; pending raw PDFs only.' : ''; }}
  if (deletePending) {{
    deletePending.disabled = pendingDeleteCount === 0;
    deletePending.title = pendingDeleteCount > 1 ? `Confirm deletion for ${{pendingDeleteCount}} selected PDFs` : 'Confirm deletion for the selected PDF';
    deletePending.classList.toggle('disabled-control', deletePending.disabled);
  }}
  if (detach) {{ detach.disabled = !linked; detach.classList.toggle('disabled-control', detach.disabled); }}
  if (deleteReviewed) {{ deleteReviewed.disabled = !linked; deleteReviewed.classList.toggle('disabled-control', deleteReviewed.disabled); }}
}}
function renderSelectedPendingReview() {{
  latestContractReview = null;
  const status = document.getElementById('contract-review-status');
  const groups = document.getElementById('term-review-groups');
  const evidence = document.getElementById('evidence-actions');
  const item = selectedReviewItem || {{}};
  const source = item.source_filename || item.source_path || item.source_file || 'No source selected';
  if (status) status.textContent = `${{source}} is waiting for extraction.`;
  if (groups) groups.innerHTML = '<p class="small muted">Upload and extraction must finish before terms can be reviewed.</p>';
  if (evidence) evidence.innerHTML = `<span class="badge ${{badgeClass(reviewStatus(item))}}">${{esc(reviewStatus(item))}}</span><p class="small">${{esc(item.message || item.blocker || 'Waiting for extraction.')}}</p>`;
  updateProspectusActionState();
  updateTermActionState();
  void renderTermsNextStep(null);
}}
function focusContractField(fieldName) {{
  const input = document.querySelector(`[data-contract-field="${{fieldName}}"]`);
  const disclosure = input?.closest('details');
  if (disclosure) disclosure.open = true;
  input?.scrollIntoView?.({{behavior:'smooth', block:'center'}});
  input?.focus?.();
}}
function usableEvidenceItems(field) {{
  const evidenceItems = Array.isArray(field.evidence) ? field.evidence : [];
  return evidenceItems.filter(item => Number(item?.page || 0) >= 1 && String(item?.snippet || '').trim() && String(item?.match_type || '').trim());
}}
function evidenceForTerm(field) {{
  const validItems = usableEvidenceItems(field);
  if (field.derived) return '<span class="badge good">calculated</span><div class="small muted">Issue price + brokerage</div>';
  if (!validItems.length && field.evidence_status === 'not_required') return '<span class="small muted">—</span>';
  if (!validItems.length) return '<span class="badge warn">source needed</span>';
  const first = validItems[0] || {{}};
  const count = validItems.length;
  const confidence = Number(first.confidence);
  const confidenceText = Number.isFinite(confidence)
    ? `${{Math.round(confidence <= 1 ? confidence * 100 : confidence)}}% confidence`
    : '';
  const meta = [
    first.page ? `p${{first.page}}` : '',
    confidenceText,
    count > 1 ? `${{count}} snippets` : '1 snippet'
  ].filter(Boolean).join(' · ');
  return `<details class="term-evidence"><summary>${{esc(meta)}}</summary><div class="term-evidence-snippet">${{esc(first.snippet)}}</div></details>`;
}}
function editableFieldInput(field) {{
  const value = field.value === null || field.value === undefined ? '' : String(field.value);
  const choices = Array.isArray(field.choices) ? field.choices : [];
  const hasCurrentChoice = choices.some(choice => String(choice.value) === value);
  const input = choices.length
    ? `<select data-contract-field="${{esc(field.field)}}"${{field.read_only ? ' disabled' : ''}}>${{!hasCurrentChoice && value ? `<option value="${{esc(value)}}" selected>${{esc(value)}} (current)</option>` : ''}}${{choices.map(choice => `<option value="${{esc(choice.value)}}" ${{String(choice.value) === value ? 'selected' : ''}}>${{esc(choice.label || choice.value)}}</option>`).join('')}}</select>`
    : `<input data-contract-field="${{esc(field.field)}}" value="${{esc(value)}}" ${{field.read_only ? 'readonly aria-readonly="true"' : ''}}>`;
  const help = field.help ? `<span class="term-help">${{esc(field.help)}}</span>` : '';
  const issueText = (field.issues || []).map(issue => issue.message).filter(Boolean).join(' ');
  const issue = issueText ? `<span class="term-help warn">${{esc(issueText)}}</span>` : '';
  return `<tr class="term-row ${{field.attention_required ? 'attention' : ''}}"><td><b>${{esc(field.label || field.field)}}</b>${{help}}${{issue}}</td><td>${{input}}</td><td>${{evidenceForTerm(field)}}</td></tr>`;
}}
function renderEvidenceActions(payload) {{
  const el = document.getElementById('evidence-actions');
  if (!el) return;
  const issues = payload.validation_issues || [];
  const errors = issues.filter(issue => issue.severity === 'error');
  const warnings = issues.filter(issue => issue.severity !== 'error');
  const blockerCount = Math.max(Number(payload.approval_blocker_count || 0), errors.length);
  const blockerLine = blockerCount
    ? `<p class="error">${{blockerCount}} approval blocker(s) must be resolved.</p>`
    : '<p class="small good">No approval blockers.</p>';
  const issueDetails = issues.length
    ? `<details><summary>${{issues.length}} validation note(s)</summary><ul>${{issues.map(issue => `<li class="${{issue.severity === 'error' ? 'error' : 'warn'}}">${{esc(issue.message)}}</li>`).join('')}}</ul></details>`
    : '';
  el.innerHTML = `<div class="status-strip"><div class="status-cell"><span class="muted">Status</span><b>${{esc(String(payload.status || 'review').replaceAll('_', ' '))}}</b></div><div class="status-cell"><span class="muted">Needs attention</span><b>${{esc(payload.attention_field_count || 0)}}</b></div><div class="status-cell"><span class="muted">Warnings</span><b>${{esc(warnings.length)}}</b></div></div>${{blockerLine}}${{issueDetails}}`;
  updateProspectusActionState();
}}
function renderTermReviewGroups(payload) {{
  const container = document.getElementById('term-review-groups');
  const hiddenFlat = document.getElementById('contract-term-fields');
  const fields = payload.editable_fields || [];
  if (hiddenFlat) hiddenFlat.innerHTML = '';
  if (!container) return;
  const table = rows => `<table class="term-table"><thead><tr><th>Term</th><th>Value</th><th>Source</th></tr></thead><tbody>${{rows.map(editableFieldInput).join('')}}</tbody></table>`;
  const attention = fields.filter(field => field.attention_required);
  const attentionPaths = new Set(attention.map(field => field.field));
  const hasDisplayValue = field => field.value !== null && field.value !== undefined && String(field.value) !== '';
  const hasFirstPut = fields.some(field => ['puts.0.date', 'puts.0.yield_to_put'].includes(field.field) && hasDisplayValue(field));
  const primary = fields.filter(field =>
    field.primary
    && !attentionPaths.has(field.field)
    && hasDisplayValue(field)
    && (!field.field.startsWith('puts.0.') || hasFirstPut)
  );
  const shown = new Set([...attentionPaths, ...primary.map(field => field.field)]);
  const sections = [];
  if (attention.length) sections.push(`<section class="term-group"><div class="term-section-heading"><h3>Needs attention</h3><span class="badge warn">${{attention.length}}</span></div>${{table(attention)}}</section>`);
  if (primary.length) sections.push(`<section class="term-group"><div class="term-section-heading"><h3>Key terms</h3><span class="muted small">${{primary.length}}</span></div>${{table(primary)}}</section>`);
  (payload.editable_groups || []).forEach(group => {{
    const remaining = (group.fields || []).filter(field => !shown.has(field.field));
    if (!remaining.length) return;
    sections.push(`<details class="term-group"><summary><span>${{esc(group.label)}}</span><span class="muted small">${{remaining.length}} more</span></summary>${{table(remaining)}}</details>`);
  }});
  container.innerHTML = sections.join('') || '<p class="small muted">No terms returned.</p>';
  container.querySelectorAll('[data-contract-field]').forEach(input => input.addEventListener('input', event => {{
    if (event.currentTarget.dataset.contractField === 'instrument.canonical_id') updateIdentifierStatusFromId();
    updateDerivedInvestorOffer();
    updateTermActionState();
    void renderTermsNextStep(latestContractReview);
  }}));
  updateDerivedInvestorOffer();
  updateTermActionState();
}}
function updateDerivedInvestorOffer() {{
  const issue = document.querySelector('[data-contract-field="bond.issue_price"]');
  const brokerage = document.querySelector('[data-contract-field="bond.brokerage"]');
  const offer = document.querySelector('[data-contract-field="bond.investor_offer_price"]');
  if (!offer) return;
  const issueValue = Number(issue?.value);
  const brokerageValue = Number(brokerage?.value);
  if (!issue?.value || brokerage?.value === '' || !Number.isFinite(issueValue) || !Number.isFinite(brokerageValue)) {{
    offer.value = '';
    return;
  }}
  offer.value = String(Number((issueValue + brokerageValue).toFixed(8)));
}}
function updateIdentifierStatusFromId() {{
  const idInput = document.querySelector('[data-contract-field="instrument.canonical_id"]');
  const statusInput = document.querySelector('[data-contract-field="instrument.canonical_id_type"]');
  const canonicalId = String(idInput?.value || '').trim().toUpperCase();
  if (statusInput && /^[A-Z]{{2}}[A-Z0-9]{{9}}[0-9]$/.test(canonicalId)) statusInput.value = 'ISIN';
}}
function reviewedContractStatus(payload) {{
  const status = String(payload?.status || '').toLowerCase();
  return status === 'reviewed' || status === 'approved' || status === 'complete';
}}
function updateTermActionState() {{
  const save = document.getElementById('save-contract-terms');
  const approve = document.getElementById('approve-contract-terms');
  const refresh = document.getElementById('refresh-contract-economics');
  if (!save || !approve || !refresh) return;
  const hasReview = Boolean(latestContractReview);
  const editCount = hasReview ? Object.keys(changedContractEdits()).length : 0;
  const blockers = Math.max(
    Number(latestContractReview?.approval_blocker_count || 0),
    Number(latestContractReview?.attention_field_count || 0)
  );
  const reviewed = reviewedContractStatus(latestContractReview);
  const canRefresh = Boolean(
    hasReview
    && latestContractReview?.source_file
    && latestContractReview?.refreshable_economics_missing
  );
  save.disabled = !hasReview || editCount === 0;
  approve.disabled = !hasReview || editCount > 0 || blockers > 0 || reviewed;
  refresh.hidden = !canRefresh;
  refresh.disabled = !canRefresh || editCount > 0;
  save.classList.toggle('disabled-control', save.disabled);
  approve.classList.toggle('disabled-control', approve.disabled);
  refresh.classList.toggle('disabled-control', refresh.disabled);
  save.title = editCount ? `Save ${{editCount}} changed term(s)` : 'No unsaved changes';
  approve.textContent = reviewed ? 'Approved' : 'Approve & continue';
  approve.title = editCount
    ? 'Save changes before approval'
    : blockers
      ? `Resolve ${{blockers}} highlighted item(s) before approval`
      : reviewed
        ? 'Terms are approved'
        : 'Approve terms and continue';
}}
async function renderTermsNextStep(payload) {{
  const el = document.getElementById('terms-next-step');
  if (!el) return;
  el.classList.remove('good', 'bad');
  if (!payload) {{
    el.classList.add('bad');
    el.innerHTML = '<p>Select a bond or upload a PDF to begin.</p>';
    return;
  }}
  if (Object.keys(changedContractEdits()).length) {{
    el.innerHTML = '<p>Save your changes before approving.</p>';
    return;
  }}
  if (!reviewedContractStatus(payload)) {{
    const count = Math.max(Number(payload.approval_blocker_count || 0), Number(payload.attention_field_count || 0));
    if (count) {{
      el.classList.add('bad');
      el.innerHTML = `<p>Review ${{count}} highlighted item(s) before approval.</p>`;
    }} else {{
      el.innerHTML = '<p>Terms are ready. Approve them to continue.</p>';
    }}
    return;
  }}
  const fieldValue = field => payload.editable_fields?.find(item => item.field === field)?.value;
  const canonicalType = String(fieldValue('instrument.canonical_id_type') || '').trim().toUpperCase();
  const canonicalId = String(fieldValue('instrument.canonical_id') || '').trim();
  if (!canonicalId || canonicalId.toUpperCase() === 'PENDING_ISIN') {{
    el.classList.add('bad');
    el.innerHTML = '<p>Add the final ISIN; Identifier status will switch to ISIN assigned. Then save and approve again.</p><button type="button" class="cmd-primary" data-focus-field="instrument.canonical_id">Add final ISIN</button>';
    el.querySelector('[data-focus-field]')?.addEventListener('click', event => focusContractField(event.currentTarget.dataset.focusField));
    return;
  }}
  if (canonicalType === 'PENDING_ISIN') {{
    el.classList.add('bad');
    el.innerHTML = '<p>Set Identifier status to ISIN assigned before matching market prices.</p><button type="button" class="cmd-primary" data-focus-field="instrument.canonical_id_type">Update identifier status</button>';
    el.querySelector('[data-focus-field]')?.addEventListener('click', event => focusContractField(event.currentTarget.dataset.focusField));
    return;
  }}
  const contractPath = payload.contract_path;
  const universeItem = universeItems.find(item => item.contract_path === contractPath);
  if (universeItem?.available_for_pricing || universeItem?.readiness?.status === 'ready') {{
    el.classList.add('good');
    el.innerHTML = '<p>Terms and market prices are ready.</p><button type="button" class="cmd-primary" data-next-tab="pm-view">View summary</button>';
  }} else {{
    const readiness = await loadMarketGenerationReadiness(contractPath);
    if (
      latestContractReview?.contract_path !== contractPath
      || Object.keys(changedContractEdits()).length
    ) return;
    if (readiness?.status === 'ready') {{
      el.classList.add('good');
      el.innerHTML = '<p>Terms are approved and market inputs are ready.</p><button type="button" class="cmd-primary" data-next-tab="data-management" data-next-subtab="match">Build valuation history</button>';
    }} else if (readiness?.status === 'no_overlap') {{
      el.classList.add('bad');
      el.innerHTML = '<p>Market files have no overlapping dates. Existing uploads will be kept.</p><button type="button" data-next-tab="data-management" data-next-subtab="upload">Upload prices for overlapping dates</button>';
    }} else if (readiness?.status === 'needs_identifier') {{
      el.classList.add('bad');
      el.innerHTML = '<p>Add the final ISIN in Review and approve before matching market prices.</p>';
    }} else {{
      el.innerHTML = '<p>Terms are approved. Add the missing market prices next.</p><button type="button" class="cmd-primary" data-next-tab="data-management" data-next-subtab="upload">Upload market prices</button>';
    }}
  }}
  el.querySelector('[data-next-tab]')?.addEventListener('click', async event => {{
    const tab = event.currentTarget.dataset.nextTab;
    const subtab = event.currentTarget.dataset.nextSubtab || '';
    syncActiveUniverseContract(contractPath);
    activateTab(tab);
    if (subtab) activateDataSubtab(subtab);
    if (subtab === 'match') await loadMarketGenerationReadiness(contractPath);
    if (tab === 'pm-view') await loadPricing();
    focusDestination(tab, subtab);
  }});
}}
function intendedReviewContractPath() {{
  return selectedReviewItem?.contract_path || form.elements.contract_path.value;
}}
async function loadSelectedContractReview() {{
  const status = document.getElementById('contract-review-status');
  const path = intendedReviewContractPath();
  const generation = ++contractReviewLoadGeneration;
  latestContractReview = null;
  updateTermActionState();
  if (!path) {{ status.textContent = 'No contract path selected.'; return null; }}
  const selectedLabel = selectedReviewItem?.contract_path === path
    ? instrumentLabel(selectedReviewItem)
    : String(path).split(/[\\/]/).at(-1).replace(/\.json$/i, '');
  const groups = document.getElementById('term-review-groups');
  const evidence = document.getElementById('evidence-actions');
  const nextStep = document.getElementById('terms-next-step');
  status.textContent = `Loading key terms for ${{selectedLabel}}...`;
  if (groups) groups.innerHTML = `<p class="small muted">Loading key terms for ${{esc(selectedLabel)}}...</p>`;
  if (evidence) evidence.innerHTML = '<p class="small muted">Loading review checks...</p>';
  if (nextStep) {{
    nextStep.classList.remove('good', 'bad');
    nextStep.innerHTML = '<p>Loading the selected bond...</p>';
  }}
  try {{
    const res = await fetch('/api/contract-review?contract_path=' + encodeURIComponent(path));
    const payload = await res.json();
    if (!res.ok) throw new Error(payload.error || res.statusText);
    if (generation !== contractReviewLoadGeneration || intendedReviewContractPath() !== path) return null;
    latestContractReview = payload;
    renderTermReviewGroups(payload);
    renderEvidenceActions(payload);
    updateTermActionState();
    const blockerCount = Math.max(Number(payload.approval_blocker_count || 0), Number(payload.attention_field_count || 0));
    status.textContent = blockerCount
      ? `${{blockerCount}} item(s) need attention before approval.`
      : reviewedContractStatus(payload)
        ? 'Terms approved.'
        : 'Ready to approve.';
    await renderTermsNextStep(payload);
    return payload;
  }} catch (err) {{
    if (generation === contractReviewLoadGeneration && intendedReviewContractPath() === path) {{
      status.innerHTML = '<span class="error">Load failed: ' + esc(err.message) + '</span>';
    }}
    return null;
  }}
}}
function changedContractEdits() {{
  const edits = {{}};
  document.querySelectorAll('[data-contract-field]').forEach(input => {{
    if (input.readOnly || input.disabled) return;
    const original = (latestContractReview?.editable_fields || []).find(f => f.field === input.dataset.contractField);
    const oldValue = original?.value === null || original?.value === undefined ? '' : String(original.value);
    if (String(input.value) !== oldValue) edits[input.dataset.contractField] = input.value;
  }});
  return edits;
}}
async function saveContractTerms() {{
  const status = document.getElementById('contract-review-status');
  if (!latestContractReview) {{ await loadSelectedContractReview(); if (!latestContractReview) return; }}
  const edits = changedContractEdits();
  if (!Object.keys(edits).length) {{ status.textContent = 'No changed contract fields to save.'; return; }}
  const contractPath = latestContractReview.contract_path;
  try {{
    const res = await fetch('/api/contract-review', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{contract_path:contractPath, edits, confirm:true}})}});
    const payload = await res.json();
    if (!res.ok) throw new Error(payload.error || res.statusText);
    latestContractReview = null;
    await loadUniverse({{preferredContractPaths:[contractPath], price:false}});
    await matchUploadedMarketPrices({{contractPath}});
    await loadReviewQueue({{preferredContractPaths:[contractPath]}});
    await loadSelectedContractReview();
    status.textContent = `Saved ${{payload.edited_fields.length}} change(s). Review any remaining highlights, then approve.`;
  }} catch (err) {{ status.innerHTML = '<span class="error">Save failed: ' + esc(err.message) + '</span>'; }}
}}
async function refreshContractEconomics() {{
  const status = document.getElementById('contract-review-status');
  if (!latestContractReview) {{ await loadSelectedContractReview(); if (!latestContractReview) return; }}
  if (Object.keys(changedContractEdits()).length) {{
    status.textContent = 'Save your changes before refreshing from the PDF.';
    return;
  }}
  const contractPath = latestContractReview.contract_path;
  try {{
    const res = await fetch('/api/contract-economics-refresh', {{
      method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{contract_path:contractPath, confirm:true}})
    }});
    const payload = await res.json();
    if (!res.ok) throw new Error(payload.error || res.statusText);
    if (payload.updated) {{
      await loadUniverse({{preferredContractPaths:[contractPath], price:false}});
    }}
    await loadReviewQueue({{preferredContractPaths:[contractPath]}});
    await loadSelectedContractReview();
    status.textContent = payload.updated
      ? `Updated ${{payload.added_fields.length}} missing and ${{payload.corrected_fields.length}} mismatched field(s) from the linked PDF. Review and approve again.`
      : 'No missing or safely reconcilable economics were found in the linked PDF.';
  }} catch (err) {{
    status.innerHTML = '<span class="error">PDF refresh failed: ' + esc(err.message) + '</span>';
  }}
}}
async function approveContractTerms() {{
  const status = document.getElementById('contract-review-status');
  if (!latestContractReview) {{ await loadSelectedContractReview(); if (!latestContractReview) return; }}
  const edits = changedContractEdits();
  if (Object.keys(edits).length) {{ status.textContent = 'Unsaved edits present. Save edits first, then approve.'; return; }}
  if (!confirm('Approve terms for pricing? This marks this CB as reviewed. Future edits will return it to Needs review.')) return;
  try {{
    const contractPath = latestContractReview.contract_path;
    const res = await fetch('/api/contract-approve', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{contract_path:latestContractReview.contract_path, confirm:true}})}});
    const payload = await res.json();
    if (!res.ok) throw new Error(payload.error || res.statusText);
    latestContractReview = null;
    await loadUniverse({{preferredContractPaths:[payload.contract_path], price:false}});
    await matchUploadedMarketPrices({{contractPath}});
    await loadReviewQueue({{preferredContractPaths:[payload.contract_path]}});
    await loadSelectedContractReview();
    status.textContent = 'Terms approved. Existing uploaded prices were checked automatically.';
    await renderTermsNextStep(latestContractReview);
  }} catch (err) {{ status.innerHTML = '<span class="error">Approval failed: ' + esc(err.message) + '</span>'; }}
}}
async function detachProspectus() {{
  const status = document.getElementById('contract-review-status');
  const path = selectedReviewItem?.contract_path || latestContractReview?.contract_path;
  if (!path) {{ status.textContent = 'Select a contract first.'; return; }}
  if (!confirm('Detach prospectus link from this contract? The raw file will not be deleted.')) return;
  try {{
    const res = await fetch('/api/prospectus-action', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{action:'detach', contract_path:path, confirm:true, confirm_detach:true}})}});
    const payload = await res.json();
    if (!res.ok) throw new Error(payload.error || res.statusText);
    status.textContent = `Detached prospectus link. Backup: ${{payload.backup_path}}.`;
    await loadReviewQueue();
  }} catch (err) {{ status.innerHTML = '<span class="error">Detach failed: ' + esc(err.message) + '</span>'; }}
}}
async function deleteRawProspectus() {{
  const status = document.getElementById('contract-review-status');
  const path = selectedReviewItem?.contract_path || latestContractReview?.contract_path;
  if (!path) {{ status.textContent = 'Select a contract first.'; return; }}
  const sourcePath = latestContractReview?.source_file || reviewItemSourcePath(selectedReviewItem);
  const filename = String(sourcePath || '').split('/').at(-1) || '';
  const confirmation = filename || latestContractReview?.contract_id || selectedReviewItem?.contract_id || '';
  if (!confirmation) {{ status.textContent = 'The linked source filename could not be resolved.'; return; }}
  const label = filename || `the source PDF for ${{confirmation}}`;
  if (!confirm(`Delete "${{label}}"?\\n\\nThis cannot be undone. Backend review and checksum checks still apply.`)) return;
  try {{
    const res = await fetch('/api/prospectus-action', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{action:'delete_raw', contract_path:path, confirm:true, typed_confirmation:confirmation}})}});
    const payload = await res.json();
    if (!res.ok) throw new Error(payload.error || res.statusText);
    status.textContent = payload.raw_deleted ? `Deleted ${{payload.source_path}}.` : `No raw file deleted; source was already absent or blocked.`;
    await loadReviewQueue();
    await loadSources();
  }} catch (err) {{ status.innerHTML = '<span class="error">Delete blocked: ' + esc(err.message) + '</span>'; }}
}}
function renderCharts(payload) {{
  drawYieldCurveChart('yield-curve-chart', payload.yield_curve);
  drawChart('price-chart', payload.series, metricLines('price_stack'), {{title:'Valuation Stack', xLabel:'Valuation date', yLabel:'Price / parity', linkGroup:'valuation-stack'}});
  drawChart('valuation-cheapness-mini-chart', payload.series, metricLines('cheapness_mini'), {{title:'Cheap/Rich mini-panel', xLabel:'Valuation date', yLabel:'Fair value - market price', zeroLine:true, linkGroup:'valuation-stack'}});
  drawChart('rv-cheapness-chart', payload.series, metricLines('relative_value_drivers', 'rv-cheapness'), {{title:'Cheap/Rich', xLabel:'Valuation date', yLabel:'FV - market', zeroLine:true, linkGroup:'rv-drivers'}});
  drawChart('rv-iv-chart', payload.series, metricLines('relative_value_drivers', 'rv-iv'), {{title:'Implied volatility', xLabel:'Valuation date', yLabel:'IV (%)', linkGroup:'rv-drivers'}});
  drawChart('rv-credit-spread-chart', payload.series, metricLines('relative_value_drivers', 'rv-credit-spread'), {{title:'Credit spread (bps)', xLabel:'Valuation date', yLabel:'Credit spread (bps)', linkGroup:'rv-drivers'}});
  drawChart('rv-stock-chart', payload.series, metricLines('relative_value_drivers', 'rv-stock'), {{title:'Underlying stock', xLabel:'Valuation date', yLabel:'Stock price', linkGroup:'rv-drivers'}});
  drawChart('volatility-overlay-chart', payload.series, metricLines('volatility_overlay'), {{title:'Volatility overlay', xLabel:'Valuation date', yLabel:'Volatility (%)'}});
  drawChart('fx-chart', payload.series, metricLines('market_fx'), {{title:'Market FX', xLabel:'Valuation date', yLabel:'FX rate'}});
  drawChart('assumptions-credit-spread-chart', payload.series, metricLines('credit_spread_bps'), {{title:'Credit spread assumption', xLabel:'Valuation date', yLabel:'Credit spread (bps)'}});
  drawChart('assumptions-rates-chart', payload.series, metricLines('assumption_rates_percent'), {{title:'Rates and volatility assumptions', xLabel:'Valuation date', yLabel:'Rate / volatility (%)'}});
  drawChart('raw-quote-chart', payload.raw_quote_history?.rows || [], [
    {{key:'mid_price', label:'CB mid', color:'#ff9d00'}},
    {{key:'bid_price', label:'Bid', color:'#f2f2f2'}},
    {{key:'ask_price', label:'Ask', color:'#ff5c5c'}}
  ], {{title:'All raw CB quote rows', xLabel:'Quote date/time', yLabel:'CB price'}});
}}
function scheduleResponsiveChartRender() {{
  clearTimeout(responsiveChartTimer);
  responsiveChartTimer = setTimeout(() => {{
    if (latestPayload) renderCharts(latestPayload);
    else document.querySelectorAll('svg.matlab-plot[data-empty-message]').forEach(svg => drawChartEmptyState(svg, svg.dataset.emptyMessage));
  }}, 120);
}}
function chartFrame(svg, fallbackWidth=900, fallbackHeight=240) {{
  const current = String(svg?.getAttribute('viewBox') || `0 0 ${{fallbackWidth}} ${{fallbackHeight}}`).split(/\\s+/).map(Number);
  const rect = svg?.getBoundingClientRect?.();
  const measuredWidth = Math.round(Number(rect?.width || svg?.clientWidth || 0));
  const measuredHeight = Math.round(Number(rect?.height || svg?.clientHeight || 0));
  const W = measuredWidth > 0 ? Math.max(240, measuredWidth) : (current[2] || fallbackWidth);
  const H = measuredHeight > 0 ? Math.max(160, measuredHeight) : (current[3] || fallbackHeight);
  if (svg && measuredWidth > 0 && measuredHeight > 0) svg.setAttribute('viewBox', `0 0 ${{W}} ${{H}}`);
  const compact = W < 620;
  const P = {{left:compact ? 54 : 72, right:compact ? 14 : 152, top:compact ? 32 : 34, bottom:compact ? 46 : 54}};
  return {{W, H, P, compact, fontSize:compact ? 12 : 14, titleFontSize:compact ? 13 : 14}};
}}
function chartLayout(svg, lineCount=0) {{
  const frame = chartFrame(svg);
  const P = {{...frame.P}};
  const legendColumns = frame.compact ? Math.min(2, Math.max(1, lineCount)) : 1;
  const legendRows = Math.ceil(Math.max(0, lineCount) / legendColumns);
  if (frame.compact && legendRows) P.top = 42 + legendRows * 17;
  return {{...frame, P, legendColumns, legendRows}};
}}
function drawChartEmptyState(svg, message) {{
  if (!svg) return;
  svg.dataset.emptyMessage = String(message || 'No chartable data');
  const {{W, H, P, fontSize}} = chartFrame(svg);
  const availableWidth = Math.max(120, W-P.left-P.right);
  const maxChars = Math.max(16, Math.floor(availableWidth / (fontSize * .62)));
  const words = String(message || 'No chartable data').trim().split(/\\s+/);
  const lines = [];
  words.forEach(word => {{
    const current = lines.at(-1) || '';
    if (!current || (current + ' ' + word).length > maxChars) lines.push(word);
    else lines[lines.length-1] = current + ' ' + word;
  }});
  if (lines.length > 4) {{ lines.splice(3); lines[3] = lines[3].replace(/[.,;:!?]*$/, '') + '…'; }}
  const lineHeight = fontSize + 5;
  const startY = Math.max(P.top + fontSize, Math.round(H/2 - ((lines.length-1)*lineHeight)/2));
  svg.innerHTML = `<text x="${{P.left}}" y="${{startY}}" fill="#a0a0a0" font-size="${{fontSize}}">${{lines.map((line, index) => `<tspan x="${{P.left}}" dy="${{index ? lineHeight : 0}}">${{esc(line)}}</tspan>`).join('')}}</text>`;
}}
function niceTicks(min, max, count=5) {{
  if (!Number.isFinite(min) || !Number.isFinite(max)) return [];
  if (min === max) {{ min -= 1; max += 1; }}
  const span = max - min;
  const rawStep = span / Math.max(1, count - 1);
  const mag = Math.pow(10, Math.floor(Math.log10(rawStep)));
  const norm = rawStep / mag;
  const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag;
  const start = Math.floor(min / step) * step;
  const end = Math.ceil(max / step) * step;
  const ticks = [];
  for (let v=start; v <= end + step*0.5; v += step) ticks.push(v);
  return ticks.slice(0, 8);
}}
function drawChart(id, rows, lines, opts={{}}) {{
  const svg = document.getElementById(id);
  if (!svg) return;
  const {{W, H, P, compact, fontSize, titleFontSize, legendColumns}} = chartLayout(svg, lines.length);
  const allRows = rows || [];
  const key = chartWindowKey(id, opts);
  const existingView = chartView[key];
  let viewStart = existingView ? Math.max(0, Math.min(allRows.length-1, existingView.start)) : 0;
  let viewEnd = existingView ? Math.max(viewStart + 1, Math.min(allRows.length, existingView.end)) : allRows.length;
  rows = allRows.slice(viewStart, viewEnd);
  if (!lines.length) {{ drawChartEmptyState(svg, 'Select at least one metric'); return; }}
  const points = [];
  rows.forEach((r) => lines.forEach(line => {{ const v = r[line.key]; if (v !== null && v !== undefined && Number.isFinite(Number(v))) points.push(Number(v)); }}));
  if (!points.length) {{ drawChartEmptyState(svg, 'No chartable data'); return; }}
  delete svg.dataset.emptyMessage;
  const rawMin = Math.min(...points), rawMax = Math.max(...points);
  const pad = (rawMax - rawMin || Math.abs(rawMax) || 1) * 0.08;
  const min = rawMin - pad, max = rawMax + pad, span = max-min || 1;
  const plotW = W-P.left-P.right, plotH = H-P.top-P.bottom;
  const rawXValues = rows.map((r, i) => opts.xValueKey ? Number(r?.[opts.xValueKey]) : i);
  const domainXValues = rawXValues.concat((opts.xDomainValues || []).map(Number)).filter(Number.isFinite);
  const rawXMin = domainXValues.length ? Math.min(...domainXValues) : 0;
  const rawXMax = domainXValues.length ? Math.max(...domainXValues) : Math.max(rows.length - 1, 1);
  const xMin = rawXMin === rawXMax ? rawXMin - 0.5 : rawXMin;
  const xMax = rawXMin === rawXMax ? rawXMax + 0.5 : rawXMax;
  const xSpan = xMax - xMin || 1;
  const x = value => P.left + (Number(value)-xMin)/xSpan*plotW;
  const xAt = i => x(Number.isFinite(rawXValues[i]) ? rawXValues[i] : i);
  const y = v => P.top + (max-Number(v))/span*plotH;
  const ticks = niceTicks(min, max, 5);
  let out = `<text x="${{P.left}}" y="20" fill="#d6d6d6" font-size="${{titleFontSize}}">${{esc(opts.title || '')}}</text>`;
  ticks.forEach(t => {{
    const yy = y(t);
    out += `<line x1="${{P.left}}" y1="${{yy.toFixed(1)}}" x2="${{W-P.right}}" y2="${{yy.toFixed(1)}}" stroke="#242424" stroke-width="1"/>`;
    out += `<text x="${{P.left-8}}" y="${{(yy+4).toFixed(1)}}" text-anchor="end" fill="#b8b8b8" font-size="${{fontSize}}">${{fmtUnit(t, opts.displayUnit || lines[0]?.displayUnit || (lines[0]?.pct ? 'percent' : ''))}}</text>`;
  }});
  const xTickCount = Math.min(compact ? 3 : 5, rows.length);
  for (let i=0; i<xTickCount; i++) {{
    const idx = xTickCount === 1 ? 0 : Math.round(i*(rows.length-1)/(xTickCount-1));
    const xx = xAt(idx);
    const tickLabel = opts.xTickFormatter ? opts.xTickFormatter(rows[idx], rawXValues[idx], idx) : (rows[idx]?.date || '');
    out += `<line x1="${{xx.toFixed(1)}}" y1="${{P.top}}" x2="${{xx.toFixed(1)}}" y2="${{H-P.bottom}}" stroke="#202020" stroke-width="1"/>`;
    out += `<text x="${{xx.toFixed(1)}}" y="${{H-30}}" text-anchor="middle" fill="#b8b8b8" font-size="${{fontSize}}">${{esc(tickLabel)}}</text>`;
  }}
  out += `<line x1="${{P.left}}" y1="${{H-P.bottom}}" x2="${{W-P.right}}" y2="${{H-P.bottom}}" stroke="#9a9a9a"/>`;
  out += `<line x1="${{P.left}}" y1="${{P.top}}" x2="${{P.left}}" y2="${{H-P.bottom}}" stroke="#9a9a9a"/>`;
  if (opts.zeroLine && min < 0 && max > 0) {{
    const zy = y(0);
    out += `<line x1="${{P.left}}" y1="${{zy.toFixed(1)}}" x2="${{W-P.right}}" y2="${{zy.toFixed(1)}}" stroke="#ffd43b" stroke-dasharray="5 4"/>`;
    out += `<text x="${{W-P.right+8}}" y="${{(zy+4).toFixed(1)}}" fill="#ffd43b" font-size="${{fontSize}}">zero</text>`;
  }}
  out += `<text x="${{P.left + plotW/2}}" y="${{H-8}}" text-anchor="middle" fill="#cfcfcf" font-size="${{fontSize}}">${{esc(opts.xLabel || 'Date')}}</text>`;
  out += `<text x="16" y="${{P.top + plotH/2}}" transform="rotate(-90 16 ${{P.top + plotH/2}})" text-anchor="middle" fill="#cfcfcf" font-size="${{fontSize}}">${{esc(opts.yLabel || 'Value')}}</text>`;
  lines.forEach((line, idx) => {{
    const coords = rows.map((r,i) => r[line.key] == null ? null : [xAt(i), y(r[line.key]), Number(r[line.key]), i]).filter(Boolean);
    if (coords.length) {{
      out += `<polyline fill="none" stroke="${{line.color}}" stroke-width="2" points="${{coords.map(p => p[0].toFixed(1)+','+p[1].toFixed(1)).join(' ')}}"/>`;
      coords.forEach((pt, j) => {{ if (coords.length <= 35 || j === coords.length-1) out += `<circle cx="${{pt[0].toFixed(1)}}" cy="${{pt[1].toFixed(1)}}" r="2.6" fill="#000" stroke="${{line.color}}" stroke-width="1.5"><title>${{esc(line.label)}} ${{fmtUnit(pt[2], line.displayUnit || (line.pct ? 'percent' : ''))}} on ${{esc(rows[pt[3]]?.date || '')}}</title></circle>`; }});
      const last = coords.at(-1);
      out += `<text x="${{Math.min(last[0]+6, W-P.right-40).toFixed(1)}}" y="${{(last[1]-5).toFixed(1)}}" fill="${{line.color}}" font-size="${{fontSize}}">${{fmtUnit(last[2], line.displayUnit || (line.pct ? 'percent' : ''))}}</text>`;
    }}
    const legendColumnWidth = compact ? (W-P.left-P.right)/legendColumns : 0;
    const lx = compact ? P.left+(idx%legendColumns)*legendColumnWidth : W-P.right+18;
    const ly = compact ? 35+Math.floor(idx/legendColumns)*17 : P.top+idx*Math.max(17, fontSize+5);
    const legendLineWidth = compact ? 10 : 16;
    const legendFontSize = compact ? 11 : fontSize;
    const maxLegendChars = compact ? Math.max(7, Math.floor((legendColumnWidth-18)/(legendFontSize*.58))) : Number.MAX_SAFE_INTEGER;
    const legendLabel = compact && String(line.label).length > maxLegendChars ? String(line.label).slice(0, Math.max(1, maxLegendChars-1))+'…' : String(line.label);
    out += `<line x1="${{lx}}" y1="${{ly}}" x2="${{lx+legendLineWidth}}" y2="${{ly}}" stroke="${{line.color}}" stroke-width="2"/><text x="${{lx+legendLineWidth+4}}" y="${{ly+4}}" fill="${{line.color}}" font-size="${{legendFontSize}}">${{esc(legendLabel)}}<title>${{esc(line.label)}}</title></text>`;
  }});
  svg.innerHTML = out;
  installChartInteractions(svg, key, allRows.length);
}}
function installChartInteractions(svg, id, rowCount) {{
  if (!rowCount || rowCount < 2) return;
  const localX = event => {{
    if (Number.isFinite(event.offsetX)) return event.offsetX;
    const rect = svg.getBoundingClientRect?.() || {{left:0, width:svg.clientWidth || 900}};
    return Math.max(0, Math.min(rect.width || svg.clientWidth || 900, Number(event.clientX || 0) - Number(rect.left || 0)));
  }};
  let dragStartX = null;
  svg.onpointerdown = event => {{ dragStartX = localX(event); svg.setPointerCapture?.(event.pointerId); }};
  svg.onpointerup = event => {{
    if (dragStartX === null) return;
    const dx = localX(event) - dragStartX;
    dragStartX = null;
    if (Math.abs(dx) < 8) return;
    const current = chartView[id] || {{start:0, end:rowCount}};
    const width = Math.max(1, current.end - current.start);
    const shift = Math.max(1, Math.round(-dx / 80));
    let start = Math.max(0, Math.min(rowCount - width, current.start + shift));
    chartView[id] = {{start, end:start + width}};
    if (latestPayload) renderCharts(latestPayload);
  }};
  svg.onpointercancel = () => {{ dragStartX = null; }};
  svg.onwheel = event => {{
    if (!isChartWheelZoomGesture(event)) return;
    event.preventDefault();
    const current = chartView[id] || {{start:0, end:rowCount}};
    const width = Math.max(2, current.end - current.start);
    const rect = svg.getBoundingClientRect?.() || {{width:svg.clientWidth || 900}};
    const centerRatio = Math.max(0, Math.min(1, localX(event) / Math.max(1, rect.width || svg.clientWidth || 900)));
    const center = current.start + width * centerRatio;
    const nextWidth = event.deltaY > 0 ? Math.min(rowCount, Math.ceil(width * 1.25)) : Math.max(2, Math.floor(width * 0.8));
    let start = Math.round(center - nextWidth * centerRatio);
    start = Math.max(0, Math.min(rowCount - nextWidth, start));
    chartView[id] = {{start, end:start + nextWidth}};
    if (latestPayload) renderCharts(latestPayload);
  }};
}}
function clampPlotX(value, xMin, xMax) {{
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return xMin;
  return Math.max(xMin, Math.min(xMax, numeric));
}}
function drawYieldCurveChart(id, curve) {{
  const rows = (curve?.points || []).map(p => ({{date:String(p.years)+'y', years:p.years, rate:p.rate, label:p.label}}));
  const latest = (curve?.matches || []).at(-1);
  const svg = document.getElementById(id);
  if (!svg) return;
  if (!curve?.enabled || !rows.length) {{
    const manual = latestPayload?.series?.at(-1)?.risk_free_rate;
    drawChartEmptyState(svg, `Yield curve disabled. Manual fallback RF: ${{fmt(manual,true)}}.`);
    return;
  }}
  drawChart(id, rows, [{{key:'rate', label:`${{curve.currency}} yield`, color:'#f2f2f2', pct:true}}], {{title:`${{curve.currency}} yield curve — ${{curve.source}}`, xLabel:'Tenor (years)', yLabel:'Yield', xValueKey:'years', xTickFormatter:(row, value) => `${{Number(value).toFixed(Number(value) < 1 ? 2 : 1)}}y`}});
  if (!latest) return;
  const {{W, H, P, compact, fontSize}} = chartLayout(svg, 1);
  const rates = rows.map(r => Number(r.rate));
  const rawMin = Math.min(...rates), rawMax = Math.max(...rates);
  const pad = (rawMax - rawMin || Math.abs(rawMax) || 1) * 0.08;
  const min = rawMin - pad, max = rawMax + pad, span = max-min || 1;
  const years = rows.map(r => Number(r.years));
  const minX = Math.min(...years), maxX = Math.max(...years), xSpan = maxX-minX || 1;
  const x = value => P.left + (Number(value)-minX)/xSpan*(W-P.left-P.right);
  const y = v => P.top + (max-Number(v))/span*(H-P.top-P.bottom);
  const markerYears = clampPlotX(Number(latest.target_years), minX, maxX);
  const xx=x(markerYears), yy=y(latest.risk_free_rate);
  const clampedNote = Math.abs(markerYears - Number(latest.target_years)) > 1e-9 ? ` (shown at available curve edge ${{markerYears.toFixed(2)}}y)` : '';
  const markerLabel = compact ? `Match ${{Number(latest.target_years).toFixed(2)}}y: ${{fmt(latest.risk_free_rate,true)}}` : `Matched maturity ${{Number(latest.target_years).toFixed(2)}}y: ${{fmt(latest.risk_free_rate,true)}}${{clampedNote}}`;
  const markerLabelX = compact ? P.left + 4 : Math.min(xx+8, W-250);
  svg.innerHTML += `<line x1="${{xx.toFixed(1)}}" y1="${{P.top}}" x2="${{xx.toFixed(1)}}" y2="${{H-P.bottom}}" stroke="#ff5555" stroke-dasharray="4 4"/>` +
    `<circle cx="${{xx.toFixed(1)}}" cy="${{yy.toFixed(1)}}" r="5" fill="#ff5555"><title>Matched maturity: ${{fmt(latest.risk_free_rate,true)}} at target ${{Number(latest.target_years).toFixed(2)}}y${{clampedNote}}</title></circle>` +
    `<text x="${{markerLabelX.toFixed(1)}}" y="${{Math.max(22, yy-10).toFixed(1)}}" fill="#ff7777" font-size="${{fontSize}}">${{markerLabel}}</text>`;
}}
if (window.location.pathname === '/help') activateTab('help');
loadUniverse();
loadReviewQueue();
loadSources();
</script>
</body>
</html>"""


def _json_default(value: Any) -> Any:
    converted = to_jsonable(value)
    if converted is value:
        return str(value)
    return converted


class CbTerminalRequestHandler(BaseHTTPRequestHandler):
    server_version = "CbTerminal/0.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        try:
            if parsed.path in ("/", "/index.html", "/help"):
                self._send_html(render_dashboard_html())
            elif parsed.path == "/health":
                self._send_json({"status": "ok", "service": "cb-terminal", "project_root": str(PROJECT_ROOT)})
            elif parsed.path == "/api/universe":
                self._send_json(build_universe_payload(_query_value(parse_qs(parsed.query), "universe_path", DEFAULT_UNIVERSE)))
            elif parsed.path == "/api/assumptions":
                query = parse_qs(parsed.query)
                self._send_json(
                    build_assumptions_payload(
                        _query_value(query, "contract_id", ""),
                        _query_value(query, "scenario_name", "base"),
                    )
                )
            elif parsed.path == "/api/review-queue":
                self._send_json(build_review_queue_payload())
            elif parsed.path in {"/api/sources", "/api/source-inventory"}:
                query = parse_qs(parsed.query)
                self._send_json(build_sources_payload(include_hashes=_query_bool(query, "include_hashes", False)))
            elif parsed.path == "/api/market-generation-readiness":
                query = parse_qs(parsed.query)
                self._send_json(market_generation_readiness_payload({"contract_path": _query_value(query, "contract_path", DEFAULT_CONTRACT)}))
            elif parsed.path == "/api/metric-views":
                self._send_json(build_metric_views_payload())
            elif parsed.path == "/api/contract-review":
                query = parse_qs(parsed.query)
                self._send_json(build_contract_review_payload(_query_value(query, "contract_path", DEFAULT_CONTRACT)))
            elif parsed.path == "/api/batch-price":
                self._send_json(_payload_from_query(parse_qs(parsed.query)))
            else:
                self._send_json({"error": "not found"}, status=404)
        except Exception as exc:  # keep local workbench debuggable
            self._send_json({"error": str(exc)}, status=400)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        try:
            body = self._read_json_body(MAX_UPLOAD_BODY_BYTES if parsed.path == "/api/upload" else MAX_JSON_BODY_BYTES)
            if parsed.path == "/api/assumptions":
                self._send_json(save_assumptions_payload(body))
            elif parsed.path == "/api/price-preview":
                self._send_json(preview_pricing_payload(body))
            elif parsed.path == "/api/nuke":
                self._send_json(build_nuke_payload(body))
            elif parsed.path == "/api/upload":
                self._send_json(upload_file_payload(body))
            elif parsed.path == "/api/contract-review":
                self._send_json(edit_contract_terms_payload(body))
            elif parsed.path == "/api/contract-approve":
                self._send_json(approve_contract_terms_payload(body))
            elif parsed.path == "/api/contract-economics-refresh":
                self._send_json(refresh_contract_economics_payload(body))
            elif parsed.path == "/api/prospectus-action":
                self._send_json(prospectus_action_payload(body))
            elif parsed.path == "/api/prospectus-intake":
                self._send_json(run_prospectus_extraction_payload(body))
            elif parsed.path == "/api/raw-prospectus-action":
                self._send_json(raw_prospectus_action_payload(body))
            elif parsed.path == "/api/source-action":
                self._send_json(source_action_payload(body))
            elif parsed.path == "/api/generate-valuation-market-history":
                self._send_json(generate_valuation_market_history_payload(body))
            else:
                self._send_json({"error": "not found"}, status=404)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=400)

    def do_PATCH(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        try:
            body = self._read_json_body(MAX_JSON_BODY_BYTES)
            if parsed.path == "/api/contract-review":
                self._send_json(edit_contract_terms_payload(body))
            else:
                self._send_json({"error": "not found"}, status=404)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=400)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _send_html(self, body: str, status: int = 200) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_json(self, payload: Mapping[str, Any], status: int = 200) -> None:
        encoded = dumps_json(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _read_json_body(self, max_bytes: int = MAX_JSON_BODY_BYTES) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length < 0:
            raise ValueError("Content-Length must be non-negative")
        if length > max_bytes:
            raise ValueError(f"JSON body too large; max {max_bytes} bytes")
        raw = self.rfile.read(length) if length else b"{}"
        if not raw:
            return {}
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("JSON object body required")
        return payload


def _payload_from_query(query: Mapping[str, list[str]]) -> dict[str, Any]:
    return build_batch_payload(
        contract_path=_query_value(query, "contract_path", DEFAULT_CONTRACT),
        market_history_path=_query_value(query, "market_history_path", DEFAULT_MARKET_HISTORY),
        raw_price_history_path=_query_optional_value(query, "raw_price_history_path"),
        volatility=_query_rate_decimal(query, "volatility", DEFAULT_VOLATILITY, unit="percent"),
        risk_free_rate=_query_rate_decimal(query, "risk_free_rate", DEFAULT_RISK_FREE_RATE, unit="percent"),
        credit_spread=_query_rate_decimal(query, "credit_spread", DEFAULT_CREDIT_SPREAD, unit="bps"),
        borrow_rate=_query_rate_decimal(query, "borrow_rate", DEFAULT_BORROW_RATE, unit="percent"),
        dividend_yield=_query_rate_decimal(query, "dividend_yield", DEFAULT_DIVIDEND_YIELD, unit="percent"),
        steps=_bounded_steps(_query_float(query, "steps", DEFAULT_STEPS)),
        use_yield_curve=_query_bool(query, "use_yield_curve", DEFAULT_USE_YIELD_CURVE),
        yield_curve_currency=_query_value(query, "yield_curve_currency", ""),
        use_history_assumptions=_query_bool(query, "use_history_assumptions", False),
        assumption_set_id=_query_optional_int(query, "assumption_set_id"),
        db_path=_query_db_path(query),
        model_mode=_query_value(query, "model_mode", DEFAULT_MODEL_MODE),
    )


def _query_db_path(query: Mapping[str, list[str]]) -> Path | None:
    raw = _query_optional_value(query, "db_path")
    if raw is None:
        return None
    return resolve_project_path(raw)


def _query_optional_value(query: Mapping[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    if not values or values[0] == "":
        return None
    return values[0]


def _query_optional_int(query: Mapping[str, list[str]], name: str) -> int | None:
    raw = _query_optional_value(query, name)
    return int(raw) if raw is not None else None


def _query_value(query: Mapping[str, list[str]], name: str, default: str) -> str:
    values = query.get(name)
    return values[0] if values and values[0] != "" else default


def _query_float(query: Mapping[str, list[str]], name: str, default: float) -> float:
    raw = _query_value(query, name, str(default))
    return float(raw)


def _query_rate_decimal(query: Mapping[str, list[str]], name: str, default: float, *, unit: str) -> float:
    raw_value = _query_optional_value(query, name)
    if raw_value is None:
        return float(default)
    value = float(raw_value)
    if _query_value(query, "input_units", "").strip().lower() == "display":
        return value / (10_000.0 if unit == "bps" else 100.0)
    return _rate_input_to_decimal(value, unit=unit)


def _query_bool(query: Mapping[str, list[str]], name: str, default: bool = False) -> bool:
    values = query.get(name)
    if not values:
        return default
    return values[0].strip().lower() in {"1", "true", "yes", "on"}


def _is_loopback_host(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"}


def serve(host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    if not _is_loopback_host(host) and os.environ.get("CB_ARB_ALLOW_REMOTE") != "1":
        raise ValueError("Refusing non-loopback bind without CB_ARB_ALLOW_REMOTE=1; mutating local-file APIs are intended for private localhost use.")
    if not _is_loopback_host(host):
        print("WARNING: CB Terminal exposes local-file mutation APIs; use only on a trusted private network.")
    httpd = ThreadingHTTPServer((host, port), CbTerminalRequestHandler)
    print(f"CB Terminal: http://{host}:{port}/")
    print("Health check: /health; universe API: /api/universe; JSON pricing API: /api/batch-price")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping CB Terminal")
    finally:
        httpd.server_close()
    return httpd
