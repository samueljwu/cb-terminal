"""Raw prospectus file lifecycle service."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from cb_terminal.prospectus.source_indexes import rewrite_prospectus_indexes
from cb_terminal.storage.file_artifacts import sha256_file
from cb_terminal.storage.paths import ProjectPaths


class RawProspectusLifecycle:
    """Apply one set of rules to Data Sources and Prospectus Intake raw PDFs."""

    def __init__(
        self,
        project_root: str | Path,
        *,
        raw_dir: str = "data/raw/prospectuses",
        is_linked_to_contract: Callable[[Path], bool] | None = None,
        safe_filename: Callable[[str], str] | None = None,
    ) -> None:
        self.paths = ProjectPaths(project_root)
        self.raw_dir = raw_dir
        self._is_linked_to_contract = is_linked_to_contract or (lambda _path: False)
        self._safe_filename = safe_filename or (lambda value: Path(value).name)

    def resolve_pdf(self, value: str | Path) -> Path:
        source = self.paths.resolve_under(self.raw_dir, value, label="source_path")
        self.paths.require_extension(source, {".pdf"}, label="source_path")
        return source

    def rename_pending(self, source_path: str | Path, new_filename: str, *, digest: str = "") -> dict[str, Any]:
        source = self.resolve_pdf(source_path)
        if not source.exists():
            raise ValueError(f"raw prospectus not found: {self.paths.display(source)}")
        if self._is_linked_to_contract(source):
            raise ValueError("raw prospectus is linked to a contract; use the reviewed contract prospectus action instead")
        filename = self._safe_filename(new_filename)
        if Path(filename).suffix.lower() != ".pdf":
            raise ValueError("new_filename must be a PDF filename")
        destination = (source.parent / filename).resolve()
        try:
            destination.relative_to(self.paths.resolve(self.raw_dir))
        except ValueError as exc:
            raise ValueError(f"new_filename must stay under {self.raw_dir}") from exc
        if destination.exists():
            raise ValueError(f"destination already exists: {filename}")
        source.rename(destination)
        updated_indexes = rewrite_prospectus_indexes(self.paths.project_root, source, new_path=destination)
        return {
            "action": "rename",
            "kind": "raw_prospectus",
            "old_path": self.paths.display(source),
            "new_path": self.paths.display(destination),
            "filename": destination.name,
            "source_sha256": digest,
            "raw_deleted": False,
            "updated_indexes": updated_indexes,
        }

    def delete_pending(
        self,
        source_path: str | Path,
        *,
        typed_confirmation: str,
        digest: str = "",
        allow_missing: bool = False,
    ) -> dict[str, Any]:
        source = self.resolve_pdf(source_path)
        typed = str(typed_confirmation or "").strip()
        if typed != source.name:
            raise ValueError("delete_raw requires typed_confirmation matching the raw prospectus filename")
        if not source.exists():
            if not allow_missing:
                raise ValueError(f"raw prospectus not found: {self.paths.display(source)}")
            updated_indexes = rewrite_prospectus_indexes(self.paths.project_root, source, delete=True)
            return {
                "action": "remove",
                "kind": "raw_prospectus",
                "source_path": self.paths.display(source),
                "source_sha256": "",
                "raw_deleted": False,
                "already_missing": True,
                "updated_indexes": updated_indexes,
                "message": "Raw prospectus file was already missing; removed stale intake/source index rows.",
            }
        if self._is_linked_to_contract(source):
            raise ValueError("raw prospectus is linked to a contract; use the reviewed contract prospectus action instead")
        actual_digest = digest or sha256_file(source)
        source.unlink()
        updated_indexes = rewrite_prospectus_indexes(self.paths.project_root, source, delete=True)
        return {
            "action": "remove",
            "kind": "raw_prospectus",
            "source_path": self.paths.display(source),
            "source_sha256": actual_digest,
            "raw_deleted": True,
            "updated_indexes": updated_indexes,
        }
