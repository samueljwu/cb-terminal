"""Shared file metadata helpers for source artifacts."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_file(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_metadata(path: str | Path) -> dict[str, Any]:
    artifact = Path(path)
    stat = artifact.stat()
    return {
        "filename": artifact.name,
        "byte_size": stat.st_size,
        "sha256": sha256_file(artifact),
    }
