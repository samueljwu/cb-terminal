"""Shared JSON artifact persistence helpers."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from cb_terminal.core.time import backup_timestamp
from cb_terminal.domain.serialization import dump_json, dumps_json

T = TypeVar("T")


def read_json(path: str | Path, *, default: T | None = None) -> Any | T | None:
    artifact = Path(path)
    if not artifact.exists():
        return default
    return json.loads(artifact.read_text(encoding="utf-8"))


def read_json_list(path: str | Path, *, default: list[Any] | None = None) -> list[Any]:
    value = read_json(path, default=[] if default is None else default)
    return value if isinstance(value, list) else ([] if default is None else default)


def read_json_mapping(path: str | Path, *, default: dict[str, Any] | None = None) -> dict[str, Any]:
    value = read_json(path, default={} if default is None else default)
    return value if isinstance(value, dict) else ({} if default is None else default)


def write_json_atomic(path: str | Path, value: Any, *, backup: bool = False) -> Path:
    artifact = Path(path)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    if backup and artifact.exists():
        backup_path = artifact.with_name(f"{artifact.name}.{backup_timestamp()}.bak")
        backup_path.write_text(dumps_json(read_json(artifact), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{artifact.name}.", suffix=".tmp", dir=artifact.parent)
    tmp_path = Path(tmp_name)
    try:
        with open(fd, "w", encoding="utf-8") as handle:
            dump_json(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        tmp_path.replace(artifact)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
    return artifact


def update_json_list(path: str | Path, transform: Callable[[list[Any]], list[Any]], *, backup: bool = False) -> list[Any]:
    current = read_json_list(path, default=[])
    updated = transform(list(current))
    write_json_atomic(path, updated, backup=backup)
    return updated
