"""Small validation helpers used by the stdlib-only core."""

from __future__ import annotations

import math


class ValidationError(ValueError):
    """Raised when normalized inputs are internally inconsistent."""


def require_positive(name: str, value: float) -> None:
    if not math.isfinite(float(value)) or value <= 0:
        raise ValidationError(f"{name} must be positive, got {value!r}")


def require_non_negative(name: str, value: float) -> None:
    if not math.isfinite(float(value)) or value < 0:
        raise ValidationError(f"{name} must be non-negative, got {value!r}")


def require_finite(name: str, value: float) -> None:
    if not math.isfinite(float(value)):
        raise ValidationError(f"{name} must be finite, got {value!r}")
