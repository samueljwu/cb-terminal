"""Time helpers for auditable CB Terminal artifacts."""

from __future__ import annotations

from datetime import datetime, timezone


def utc_now() -> datetime:
    """Return the current UTC time with microseconds stripped for stable JSON."""

    return datetime.now(timezone.utc).replace(microsecond=0)


def utc_now_iso() -> str:
    """Return an ISO-8601 UTC timestamp using Python's +00:00 suffix."""

    return utc_now().isoformat()


def utc_now_z() -> str:
    """Return an ISO-8601 UTC timestamp using a compact Z suffix."""

    return utc_now_iso().replace("+00:00", "Z")


def backup_timestamp() -> str:
    """Return a filename-safe UTC timestamp for backup artifacts."""

    return utc_now().strftime("%Y%m%dT%H%M%SZ")
