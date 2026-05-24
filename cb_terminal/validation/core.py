"""Small validation helpers used by the stdlib-only core."""


class ValidationError(ValueError):
    """Raised when normalized inputs are internally inconsistent."""


def require_positive(name: str, value: float) -> None:
    if value <= 0:
        raise ValidationError(f"{name} must be positive, got {value!r}")


def require_non_negative(name: str, value: float) -> None:
    if value < 0:
        raise ValidationError(f"{name} must be non-negative, got {value!r}")
