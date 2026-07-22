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
) -> tuple[DailyQuoteCandidate[PayloadT], str]:
    """Select a representative observed quote and return an audit reason.

    Two-sided markets take precedence over evaluated mids, which in turn take
    precedence over one-sided indications.  Within that quality tier, same-row
    equity prices identify quotes observed near the trusted daily equity close;
    a median/MAD screen then removes isolated CB prints only from that
    economically comparable cohort.
    """

    if not candidates:
        raise ValueError("daily quote selection requires at least one candidate")

    all_candidates = list(candidates)
    two_sided = [candidate for candidate in all_candidates if _is_two_sided(candidate)]
    direct_mid = [candidate for candidate in all_candidates if _is_direct_mid(candidate)]
    if two_sided:
        quality_pool = two_sided
        quality_reason = "two_sided"
    elif direct_mid:
        quality_pool = direct_mid
        quality_reason = "direct_mid_fallback"
    else:
        quality_pool = all_candidates
        quality_reason = "one_sided_fallback"

    reason_parts = ["robust_consensus", quality_reason]

    ignored_for_quality = len(all_candidates) - len(quality_pool)
    if ignored_for_quality:
        reason_parts.append(f"lower_quality_quotes_ignored:{ignored_for_quality}")

    context_pool = quality_pool
    if _valid_stock_close(stock_close):
        close = float(stock_close)
        with_stock = [candidate for candidate in quality_pool if _positive_finite(candidate.stock_price)]
        if with_stock:
            candidates_with_gaps = [
                (candidate, abs(float(candidate.stock_price) - close) / close) for candidate in with_stock
            ]
            minimum_gap = min(gap for _, gap in candidates_with_gaps)
            # Keep a small cohort around the closest stock snapshot.  The
            # minimum-relative window avoids making a single exact stock value
            # the whole decision when several dealers quoted at nearly the same
            # underlying level.
            maximum_gap = max(0.005, minimum_gap + 0.0025)
            context_pool = [candidate for candidate, gap in candidates_with_gaps if gap <= maximum_gap]
            reason_parts.append(f"stock_close_context:{close:g}")
            missing_stock_count = len(quality_pool) - len(with_stock)
            if missing_stock_count:
                reason_parts.append(f"quote_stock_missing_candidates:{missing_stock_count}")
            different_snapshot_count = len(with_stock) - len(context_pool)
            if different_snapshot_count:
                reason_parts.append(f"different_stock_snapshots_ignored:{different_snapshot_count}")
            if minimum_gap > 0.02:
                reason_parts.append(f"quote_stock_far_from_close:{minimum_gap:.1%}")
        else:
            reason_parts.append("quote_stock_missing")

    # CB prices observed at materially different underlying levels are not
    # comparable bad-print candidates.  Apply the robust price screen only
    # after the economically comparable stock cohort has been identified.
    selection_pool, outlier_count = _exclude_mid_outliers(context_pool)
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


def _exclude_mid_outliers(
    candidates: Sequence[DailyQuoteCandidate[PayloadT]],
) -> tuple[list[DailyQuoteCandidate[PayloadT]], int]:
    if len(candidates) < 3:
        return list(candidates), 0
    center = float(median(candidate.mid_price for candidate in candidates))
    absolute_deviations = [abs(candidate.mid_price - center) for candidate in candidates]
    mad = float(median(absolute_deviations))
    # Three robust standard deviations, with a conservative floor.  The floor
    # avoids labelling ordinary sub-two-point dealer dispersion as bad data when
    # the MAD collapses to zero because several quotes are identical.
    cutoff = max(3.0 * 1.4826 * mad, abs(center) * 0.02, 1.0)
    retained = [candidate for candidate in candidates if abs(candidate.mid_price - center) <= cutoff]
    return retained, len(candidates) - len(retained)


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
