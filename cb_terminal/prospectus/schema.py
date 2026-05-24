"""Standard required fields for normalized CB prospectus extraction.

These are the fields every prospectus draft should try to extract or explicitly
mark as missing/needs_review.  Unusual structures should be added as structured
extensions, not by weakening these core requirements.
"""

from __future__ import annotations

REQUIRED_PROSPECTUS_TERMS: tuple[dict[str, str], ...] = (
    {"key": "instrument.canonical_id", "label": "ISIN / canonical CB identifier"},
    {"key": "instrument.display_name", "label": "standard bond display name: issuer coupon year"},
    {"key": "issuer.name", "label": "issuer legal name"},
    {"key": "issuer.ticker", "label": "underlying ticker / stock code"},
    {"key": "bond.currency", "label": "CB economic currency"},
    {"key": "bond.settlement_currency", "label": "settlement currency"},
    {"key": "bond.stock_currency", "label": "underlying stock currency"},
    {"key": "bond.issue_size", "label": "principal / issue size"},
    {"key": "bond.denomination", "label": "denomination"},
    {"key": "bond.issue_price", "label": "issue price"},
    {"key": "bond.coupon_rate", "label": "coupon rate"},
    {"key": "bond.coupon_frequency", "label": "coupon frequency"},
    {"key": "bond.pricing_date", "label": "pricing date"},
    {"key": "bond.closing_date", "label": "closing / issue date"},
    {"key": "bond.maturity_date", "label": "maturity date"},
    {"key": "bond.day_count", "label": "day-count convention"},
    {"key": "redemption.maturity_price", "label": "maturity redemption price"},
    {"key": "conversion.initial_conversion_price", "label": "initial conversion price"},
    {"key": "conversion.conversion_premium", "label": "conversion premium over reference share price, if disclosed"},
    {"key": "conversion.underlying_ticker", "label": "underlying equity for conversion"},
    {"key": "conversion.start_date", "label": "conversion start date"},
    {"key": "conversion.end_date", "label": "conversion end date"},
    {"key": "conversion.fixed_exchange_rate", "label": "fixed conversion FX, if cross-currency"},
    {"key": "conversion.fixed_exchange_rate_units", "label": "fixed FX units, standardized as stock currency per CB currency"},
    {"key": "calls", "label": "issuer call/tax/cleanup events and trigger mechanics"},
    {"key": "puts", "label": "investor put/change-of-control/delisting events"},
    {"key": "dividend_protection", "label": "dividend protection / adjustment terms"},
    {"key": "quote_convention", "label": "clean/dirty market quote convention"},
)


def required_term_keys() -> list[str]:
    return [item["key"] for item in REQUIRED_PROSPECTUS_TERMS]


def required_review_items() -> list[str]:
    return [f"Extract/verify {item['label']} ({item['key']})." for item in REQUIRED_PROSPECTUS_TERMS]
