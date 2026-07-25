"""Shared, auditable selection of one convertible-bond quote per day.

The selector deliberately returns an observed quote rather than a synthetic
average.  It uses the cross-dealer median only to identify a representative
quote and to reject isolated bad prints.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import time
from statistics import median
from typing import Generic, Sequence, TypeVar

from cb_terminal.io.outliers import robust_scale_outlier_keys


PayloadT = TypeVar("PayloadT")


@dataclass(frozen=True)
class DailyQuoteCandidate(Generic[PayloadT]):
    """Normalized quote fields plus the caller's original row payload."""

    payload: PayloadT
    mid_price: float
    bid_price: float | None
    ask_price: float | None
    stock_price: float | None
    as_of_time: time | None
    stable_key: tuple[str, str, int, str, str]


def is_clean_quote_values(
    *,
    mid_price: float | None,
    bid_price: float | None,
    ask_price: float | None,
    stock_price: float | None,
    min_price: float,
    max_price: float,
    max_bid_ask_spread: float,
    max_bid_ask_spread_pct: float,
    require_positive_stock_if_present: bool,
) -> bool:
    """Apply the common quote sanity checks used by both ingestion paths."""

    if not _finite(mid_price):
        return False
    mid = float(mid_price)
    if not (min_price <= mid <= max_price):
        return False
    if bid_price is not None and (not _finite(bid_price) or float(bid_price) <= 0.0):
        return False
    if ask_price is not None and (not _finite(ask_price) or float(ask_price) <= 0.0):
        return False
    if bid_price is not None and ask_price is not None:
        bid = float(bid_price)
        ask = float(ask_price)
        if ask < bid:
            return False
        spread = ask - bid
        if spread > max_bid_ask_spread or spread / mid > max_bid_ask_spread_pct:
            return False
        # A stale/evaluated mid can occasionally be exported beside a fresh
        # dealer market.  Do not let that unrelated number drive the daily
        # consensus when it is outside the observed bid/ask interval.  Allow a
        # tiny tolerance for spreadsheet rounding only.
        tolerance = max(1e-6, abs(mid) * 1e-8)
        if mid < bid - tolerance or mid > ask + tolerance:
            return False
    if stock_price is not None:
        if not _finite(stock_price):
            return False
        if require_positive_stock_if_present and float(stock_price) <= 0.0:
            return False
    return True


def select_daily_quote_candidate(
    candidates: Sequence[DailyQuoteCandidate[PayloadT]],
    stock_close: float | None,
    *,
    stock_fx_rate: float | None = None,
    fx_convention: str = "",
) -> tuple[DailyQuoteCandidate[PayloadT], str]:
    """Select a representative observed quote and return an audit reason.

    High-confidence stock-price scale artifacts are screened before quote
    quality is ranked.  Two-sided markets then take precedence over evaluated
    mids, which in turn take precedence over one-sided indications.  Within
    that quality tier, same-row equity prices identify quotes observed near the
    trusted daily equity close; a median/MAD screen then removes isolated CB
    prints only from that economically comparable cohort.
    """

    if not candidates:
        raise ValueError("daily quote selection requires at least one candidate")

    all_candidates = list(candidates)
    stock_screened, stock_unit_outlier_count = _exclude_stock_unit_outliers(
        all_candidates,
        stock_close,
        stock_fx_rate=stock_fx_rate,
        fx_convention=fx_convention,
    )
    reason_parts = ["robust_consensus"]
    if stock_unit_outlier_count:
        reason_parts.append(f"quote_stock_unit_outliers_excluded:{stock_unit_outlier_count}")

    # Establish economic comparability before applying quote-quality
    # precedence.  Otherwise one two-sided quote carrying a wrong-unit stock
    # value can suppress a coherent set of evaluated mids at the trusted close.
    context_pool = stock_screened
    if _valid_stock_close(stock_close):
        close = float(stock_close)
        with_stock = [candidate for candidate in stock_screened if _positive_finite(candidate.stock_price)]
        if with_stock:
            missing_stock_count = len(stock_screened) - len(with_stock)
            if missing_stock_count:
                reason_parts.append(f"quote_stock_missing_candidates:{missing_stock_count}")
            candidates_with_gaps = [
                (candidate, abs(float(candidate.stock_price) - close) / close) for candidate in with_stock
            ]
            minimum_gap = min(gap for _, gap in candidates_with_gaps)
            if minimum_gap <= 0.35:
                maximum_gap = max(0.005, minimum_gap + 0.0025)
                context_pool = [
                    candidate
                    for candidate, gap in candidates_with_gaps
                    if gap <= maximum_gap
                ]
                reason_parts.append(f"stock_close_context:{close:g}")
                different_snapshot_count = len(with_stock) - len(context_pool)
                if different_snapshot_count:
                    reason_parts.append(f"different_stock_snapshots_ignored:{different_snapshot_count}")
            else:
                # If every parsed stock value is implausibly far from the
                # close, the merely "closest" bad value is not meaningful.
                # Keep the valid CB quotes and fall back to price consensus.
                reason_parts.append(f"stock_close_context_rejected:{close:g}")
                reason_parts.append(f"all_quote_stock_context_unusable:{len(with_stock)}")
            if minimum_gap > 0.02:
                reason_parts.append(f"quote_stock_far_from_close:{minimum_gap:.1%}")
        else:
            reason_parts.append("quote_stock_missing")

    two_sided = [candidate for candidate in context_pool if _is_two_sided(candidate)]
    direct_mid = [candidate for candidate in context_pool if _is_direct_mid(candidate)]
    if two_sided:
        quality_pool = two_sided
        quality_reason = "two_sided"
    elif direct_mid:
        quality_pool = direct_mid
        quality_reason = "direct_mid_fallback"
    else:
        quality_pool = context_pool
        quality_reason = "one_sided_fallback"

    reason_parts.insert(1, quality_reason)
    ignored_for_quality = len(context_pool) - len(quality_pool)
    if ignored_for_quality:
        reason_parts.append(f"lower_quality_quotes_ignored:{ignored_for_quality}")

    # CB prices observed at materially different underlying levels are not
    # comparable bad-print candidates.  Apply the robust price screen only
    # after the economically comparable stock cohort has been identified.
    selection_pool, outlier_count = _exclude_mid_outliers(quality_pool)
    consensus_mid = float(median(candidate.mid_price for candidate in selection_pool))
    chosen = min(
        selection_pool,
        key=lambda candidate: (
            abs(candidate.mid_price - consensus_mid),
            _stock_gap(candidate, stock_close),
            _spread(candidate),
            _missing_time_penalty(candidate.as_of_time),
            _negative_time_seconds(candidate.as_of_time),
            candidate.stable_key,
        ),
    )

    if outlier_count:
        reason_parts.append(f"outliers_excluded:{outlier_count}")
    missing_timestamp_count = sum(candidate.as_of_time is None for candidate in all_candidates)
    if missing_timestamp_count:
        reason_parts.append(f"missing_timestamps:{missing_timestamp_count}/{len(all_candidates)}")
    if chosen.as_of_time is None:
        reason_parts.append("missing_timestamp")
    return chosen, ";".join(reason_parts)


def _exclude_stock_unit_outliers(
    candidates: Sequence[DailyQuoteCandidate[PayloadT]],
    stock_close: float | None,
    *,
    stock_fx_rate: float | None,
    fx_convention: str,
) -> tuple[list[DailyQuoteCandidate[PayloadT]], int]:
    candidate_list = list(candidates)
    if _valid_stock_close(stock_close):
        close = float(stock_close)
        artifacts = {
            id(candidate)
            for candidate in candidate_list
            if _positive_finite(candidate.stock_price)
            and (
                not _same_price_scale(float(candidate.stock_price), close)
                or _looks_fx_converted(
                    float(candidate.stock_price),
                    close,
                    stock_fx_rate,
                    fx_convention,
                )
            )
        }
        non_artifact_stock = any(
            _positive_finite(candidate.stock_price) and id(candidate) not in artifacts
            for candidate in candidate_list
        )
        missing_stock = any(not _positive_finite(candidate.stock_price) for candidate in candidate_list)
        if artifacts and (non_artifact_stock or missing_stock):
            retained = [
                candidate
                for candidate in candidate_list
                if id(candidate) not in artifacts
            ]
            return retained, len(candidate_list) - len(retained)
        # All populated stock values may share the same wrong currency.  Their
        # CB quotes still contain information, so abandon stock context rather
        # than deleting the entire day.
        return candidate_list, 0

    values_by_contributor: dict[tuple[object, ...], list[float]] = {}
    for candidate in candidate_list:
        if not _positive_finite(candidate.stock_price):
            continue
        values_by_contributor.setdefault(_contributor_key(candidate), []).append(
            float(candidate.stock_price)
        )
    outlier_keys = robust_scale_outlier_keys(
        values_by_contributor,
        minimum_groups=3,
        minimum_factor=2.0,
    )
    if not outlier_keys:
        return candidate_list, 0
    retained = [
        candidate
        for candidate in candidate_list
        if not _positive_finite(candidate.stock_price)
        or _contributor_key(candidate) not in outlier_keys
    ]
    return retained, len(candidate_list) - len(retained)


def _exclude_mid_outliers(
    candidates: Sequence[DailyQuoteCandidate[PayloadT]],
) -> tuple[list[DailyQuoteCandidate[PayloadT]], int]:
    mids_by_contributor: dict[tuple[object, ...], list[float]] = {}
    for candidate in candidates:
        mids_by_contributor.setdefault(_contributor_key(candidate), []).append(candidate.mid_price)
    if len(mids_by_contributor) < 3:
        return list(candidates), 0
    representative_mids = [
        float(median(values))
        for values in mids_by_contributor.values()
    ]
    center = float(median(representative_mids))
    absolute_deviations = [abs(value - center) for value in representative_mids]
    mad = float(median(absolute_deviations))
    # Three robust standard deviations, with a conservative floor.  The floor
    # avoids labelling ordinary sub-two-point dealer dispersion as bad data when
    # the MAD collapses to zero because several quotes are identical.
    cutoff = max(3.0 * 1.4826 * mad, abs(center) * 0.02, 1.0)
    retained = [candidate for candidate in candidates if abs(candidate.mid_price - center) <= cutoff]
    return retained, len(candidates) - len(retained)


def _contributor_key(candidate: DailyQuoteCandidate[object]) -> tuple[object, ...]:
    dealer = str(candidate.stable_key[3] or "").strip().casefold()
    if dealer:
        return ("dealer", dealer)
    # Without a parsed dealer/sender, rows cannot prove independence.  Collapse
    # unattributed rows so repeated chat exports cannot manufacture a majority.
    return ("unattributed",)


def _same_price_scale(value: float, anchor: float) -> bool:
    ratio = value / anchor
    return 0.5 <= ratio <= 2.0


def _looks_fx_converted(
    value: float,
    stock_close: float,
    stock_fx_rate: float | None,
    fx_convention: str,
) -> bool:
    if not _positive_finite(stock_fx_rate):
        return False
    convention = str(fx_convention or "").strip().upper()
    rate = float(stock_fx_rate)
    if convention == "STOCK_PER_CB":
        converted = value * rate
    elif convention == "CB_PER_STOCK":
        converted = value / rate
    else:
        return False
    direct_gap = abs(value - stock_close) / stock_close
    converted_gap = abs(converted - stock_close) / stock_close
    return direct_gap > 0.05 and converted_gap <= 0.05


def _is_two_sided(candidate: DailyQuoteCandidate[object]) -> bool:
    return candidate.bid_price is not None and candidate.ask_price is not None


def _is_direct_mid(candidate: DailyQuoteCandidate[object]) -> bool:
    return candidate.bid_price is None and candidate.ask_price is None


def _spread(candidate: DailyQuoteCandidate[object]) -> float:
    if candidate.bid_price is None or candidate.ask_price is None:
        return math.inf
    return float(candidate.ask_price) - float(candidate.bid_price)


def _stock_gap(candidate: DailyQuoteCandidate[object], stock_close: float | None) -> float:
    if not _valid_stock_close(stock_close) or not _positive_finite(candidate.stock_price):
        return math.inf
    return abs(float(candidate.stock_price) - float(stock_close))


def _missing_time_penalty(value: time | None) -> int:
    return 1 if value is None else 0


def _negative_time_seconds(value: time | None) -> int:
    if value is None:
        return 0
    return -(value.hour * 3600 + value.minute * 60 + value.second)


def _valid_stock_close(value: float | None) -> bool:
    return _positive_finite(value)


def _positive_finite(value: float | None) -> bool:
    return _finite(value) and float(value) > 0.0


def _finite(value: float | None) -> bool:
    if value is None:
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False
