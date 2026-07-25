"""Small, conservative helpers for identifying multiplicative data artifacts."""

from __future__ import annotations

import math
from statistics import median
from typing import Hashable, Iterable, Mapping, TypeVar


KeyT = TypeVar("KeyT", bound=Hashable)


def robust_scale_outlier_keys(
    values_by_group: Mapping[KeyT, Iterable[float]],
    *,
    minimum_groups: int = 3,
    minimum_factor: float = 2.0,
) -> set[KeyT]:
    """Return minority groups whose positive values are on a different scale.

    Financial price-unit errors are multiplicative, so the screen operates in
    log space.  It only acts when at least ``minimum_groups`` independent
    contributors are present and the retained cluster is a strict majority.
    The factor floor deliberately leaves ordinary market dispersion alone even
    when the median absolute deviation collapses to zero.
    """

    if minimum_groups < 3:
        raise ValueError("minimum_groups must be at least 3")
    if not math.isfinite(minimum_factor) or minimum_factor <= 1.0:
        raise ValueError("minimum_factor must be finite and greater than 1")

    representative_logs: dict[KeyT, float] = {}
    for key, values in values_by_group.items():
        logs = [
            math.log(number)
            for value in values
            if _positive_finite(value)
            for number in (float(value),)
        ]
        if logs:
            representative_logs[key] = float(median(logs))

    if len(representative_logs) < minimum_groups:
        return set()

    center = float(median(representative_logs.values()))
    absolute_deviations = [abs(value - center) for value in representative_logs.values()]
    mad = float(median(absolute_deviations))
    cutoff = max(3.0 * 1.4826 * mad, math.log(minimum_factor))
    retained = {
        key
        for key, value in representative_logs.items()
        if abs(value - center) <= cutoff
    }

    # A balanced or fragmented set is ambiguous, not an outlier decision.
    if len(retained) * 2 <= len(representative_logs):
        return set()
    return set(representative_logs) - retained


def _positive_finite(value: object) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0.0
