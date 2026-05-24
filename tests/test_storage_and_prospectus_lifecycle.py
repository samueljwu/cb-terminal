import json
import tempfile
import unittest
from pathlib import Path

from cb_terminal.prospectus.lifecycle import RawProspectusLifecycle
from cb_terminal.storage.paths import ProjectPaths


class ProjectPathsTests(unittest.TestCase):
    def test_resolve_blocks_project_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = ProjectPaths(tmp)
            self.assertEqual(paths.display("data/file.txt"), "data/file.txt")
            with self.assertRaises(ValueError):
                paths.resolve("../outside.txt")


class RawProspectusLifecycleTests(unittest.TestCase):
    def test_delete_pending_updates_review_queue_and_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_dir = root / "data/raw/prospectuses"
            coverage_dir = root / "data/coverage"
            raw_dir.mkdir(parents=True)
            coverage_dir.mkdir(parents=True)
            source = raw_dir / "pending.pdf"
            source.write_bytes(b"pdf bytes")
            row = {"source_path": "data/raw/prospectuses/pending.pdf", "source_filename": "pending.pdf", "source_sha256": "abc"}
            for name in ("review_queue.json", "prospectus_inventory.json"):
                (coverage_dir / name).write_text(json.dumps([row, {"source_path": "data/raw/prospectuses/other.pdf"}]), encoding="utf-8")

            result = RawProspectusLifecycle(root).delete_pending("data/raw/prospectuses/pending.pdf", typed_confirmation="pending.pdf")

            self.assertTrue(result["raw_deleted"])
            self.assertFalse(source.exists())
            self.assertEqual(set(result["updated_indexes"]), {"data/coverage/review_queue.json", "data/coverage/prospectus_inventory.json"})
            for name in ("review_queue.json", "prospectus_inventory.json"):
                rows = json.loads((coverage_dir / name).read_text(encoding="utf-8"))
                self.assertEqual(rows, [{"source_path": "data/raw/prospectuses/other.pdf"}])

    def test_rename_pending_updates_review_queue_and_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_dir = root / "data/raw/prospectuses"
            coverage_dir = root / "data/coverage"
            raw_dir.mkdir(parents=True)
            coverage_dir.mkdir(parents=True)
            source = raw_dir / "pending.pdf"
            source.write_bytes(b"pdf bytes")
            row = {"source_path": "data/raw/prospectuses/pending.pdf", "source_filename": "pending.pdf", "path": "data/raw/prospectuses/pending.pdf"}
            for name in ("review_queue.json", "prospectus_inventory.json"):
                (coverage_dir / name).write_text(json.dumps([row]), encoding="utf-8")

            result = RawProspectusLifecycle(root).rename_pending("data/raw/prospectuses/pending.pdf", "renamed.pdf")

            self.assertEqual(result["new_path"], "data/raw/prospectuses/renamed.pdf")
            self.assertFalse(source.exists())
            self.assertTrue((raw_dir / "renamed.pdf").exists())
            for name in ("review_queue.json", "prospectus_inventory.json"):
                rows = json.loads((coverage_dir / name).read_text(encoding="utf-8"))
                self.assertEqual(rows[0]["source_path"], "data/raw/prospectuses/renamed.pdf")
                self.assertEqual(rows[0]["source_filename"], "renamed.pdf")
                self.assertEqual(rows[0]["path"], "data/raw/prospectuses/renamed.pdf")


if __name__ == "__main__":
    unittest.main()
