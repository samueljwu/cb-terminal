"""Shared mutation helpers for prospectus intake indexes.

The raw PDF inbox is represented in both review_queue.json and
prospectus_inventory.json.  File lifecycle operations must update both indexes
through this module so Data Sources and Prospectus Intake cannot drift.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from cb_terminal.prospectus.inventory import prospectus_id_from_filename
from cb_terminal.storage.json_artifacts import read_json_list, write_json_atomic
from cb_terminal.storage.paths import ProjectPaths

REVIEW_QUEUE = "data/coverage/review_queue.json"
PROSPECTUS_INVENTORY = "data/coverage/prospectus_inventory.json"
PROSPECTUS_INDEXES = (REVIEW_QUEUE, PROSPECTUS_INVENTORY)


def source_item_matches(item: Mapping[str, Any], paths: ProjectPaths, old_path: Path) -> bool:
    old_display = paths.display(old_path)
    if item.get("source_path"):
        try:
            if paths.resolve(str(item["source_path"])) == old_path:
                return True
        except ValueError:
            pass
    if item.get("source_file"):
        try:
            if paths.resolve(str(item["source_file"])) == old_path:
                return True
        except ValueError:
            pass
    return bool(
        item.get("source_path") == old_display
        or item.get("source_file") == old_display
        or item.get("path") == old_display
        or item.get("source_filename") == old_path.name
    )


def rewrite_source_index(
    project_root: str | Path,
    index_path: str | Path,
    old_path: str | Path,
    *,
    new_path: str | Path | None = None,
    delete: bool = False,
) -> bool:
    paths = ProjectPaths(project_root)
    index_abs = paths.resolve(index_path)
    if not index_abs.exists():
        return False
    items = read_json_list(index_abs, default=[])
    old_abs = paths.resolve(old_path)
    new_abs = paths.resolve(new_path) if new_path is not None else None
    next_items: list[Any] = []
    changed = False
    for raw in items:
        if not isinstance(raw, dict):
            next_items.append(raw)
            continue
        if not source_item_matches(raw, paths, old_abs):
            next_items.append(raw)
            continue
        if delete:
            changed = True
            continue
        if new_abs is not None:
            item = dict(raw)
            rel = paths.display(new_abs)
            item["source_path"] = rel
            if "source_file" in item:
                item["source_file"] = rel
            if "path" in item:
                item["path"] = rel
            item["source_filename"] = new_abs.name
            item["prospectus_id"] = prospectus_id_from_filename(new_abs.name)
            next_items.append(item)
            changed = True
            continue
        next_items.append(raw)
    if changed:
        write_json_atomic(index_abs, next_items)
    return changed


def rewrite_prospectus_indexes(
    project_root: str | Path,
    old_path: str | Path,
    *,
    new_path: str | Path | None = None,
    delete: bool = False,
) -> list[str]:
    updated: list[str] = []
    for index in PROSPECTUS_INDEXES:
        if rewrite_source_index(project_root, index, old_path, new_path=new_path, delete=delete):
            updated.append(index)
    return updated


def upsert_pending_prospectus(project_root: str | Path, record: Mapping[str, Any]) -> list[str]:
    """Upsert one pending raw-prospectus record into both intake indexes."""

    updated: list[str] = []
    paths = ProjectPaths(project_root)
    rel = str(record.get("source_path") or "")
    digest = str(record.get("source_sha256") or record.get("sha256") or "")
    for index in PROSPECTUS_INDEXES:
        index_abs = paths.resolve(index)
        items = read_json_list(index_abs, default=[])
        next_items: list[Any] = []
        replaced = False
        for raw in items:
            if isinstance(raw, Mapping):
                raw_path = str(raw.get("source_path") or raw.get("source_file") or raw.get("path") or "")
                raw_digest = str(raw.get("source_sha256") or raw.get("sha256") or "")
                if raw_path == rel or (digest and raw_digest == digest):
                    next_items.append({**dict(raw), **dict(record)})
                    replaced = True
                    continue
            next_items.append(raw)
        if not replaced:
            next_items.append(dict(record))
        write_json_atomic(index_abs, next_items)
        updated.append(index)
    return updated
