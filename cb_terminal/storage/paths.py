"""Project-confined path helpers for CB Terminal artifacts."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable


class ProjectPaths:
    """Resolve and display paths without allowing escapes from a project root."""

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root).resolve()

    def resolve(self, value: str | Path) -> Path:
        path = Path(value)
        if not path.is_absolute():
            path = self.project_root / path
        resolved = path.resolve()
        try:
            resolved.relative_to(self.project_root)
        except ValueError as exc:
            raise ValueError(f"path escapes project root: {value}") from exc
        return resolved

    def display(self, path: str | Path) -> str:
        resolved = self.resolve(path)
        try:
            return resolved.relative_to(self.project_root).as_posix()
        except ValueError as exc:  # pragma: no cover - resolve already guards this
            raise ValueError(f"path escapes project root: {path}") from exc

    def safe_display_optional(self, value: object) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        return self.display(raw)

    def resolve_under(self, root: str | Path, value: str | Path, *, label: str = "path") -> Path:
        base = self.resolve(root)
        candidate = self.resolve(value)
        try:
            candidate.relative_to(base)
        except ValueError as exc:
            raise ValueError(f"{label} must be under {self.display(base)}") from exc
        return candidate

    def require_extension(self, path: Path, allowed: Iterable[str], *, label: str = "path") -> Path:
        allowed_lower = {suffix.lower() for suffix in allowed}
        if path.suffix.lower() not in allowed_lower:
            allowed_text = ", ".join(sorted(allowed_lower))
            raise ValueError(f"{label} extension must be one of: {allowed_text}")
        return path


def display_path(project_root: str | Path, path: str | Path) -> str:
    return ProjectPaths(project_root).display(path)
