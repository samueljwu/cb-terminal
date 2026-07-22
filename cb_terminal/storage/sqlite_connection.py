"""Shared SQLite connection lifecycle helpers."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def managed_sqlite_connection(path: str | Path) -> Iterator[sqlite3.Connection]:
    """Yield a configured connection, preserving transactions and always closing it."""

    connection = sqlite3.connect(path)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        with connection:
            yield connection
    finally:
        connection.close()
