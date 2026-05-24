import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ProjectMetadataTests(unittest.TestCase):
    def test_project_blueprint_exists_and_names_major_contracts(self):
        text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("name: cb-terminal", text)
        self.assertIn("Prospectus-derived terms", text)
        self.assertIn("Market history", text)
        self.assertIn("Assumptions", text)
        self.assertIn("cb_terminal/web", text)
        self.assertIn("cb_terminal/prospectus", text)
        self.assertIn("raw-prospectus inventory", text)
        self.assertIn("universe batch runner", text)

    def test_product_rename_has_no_legacy_codebase_text(self):
        self.assertTrue((ROOT / "cb_terminal").is_dir())
        self.assertFalse((ROOT / ("cb" + "_arb")).exists())
        self.assertIn('name = "cb-terminal"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"))

        forbidden = [
            "cb" + "-arb",
            "CB " + "Arb",
            "CB " + "Arbi" + "trage",
            "cb" + "_arb",
            "Cb" + "Arb",
            "convertible-bond " + "arbi" + "trage",
            "Convertible-bond " + "arbi" + "trage",
        ]
        scan_suffixes = {".py", ".md", ".toml", ".json", ".txt", ".csv", ".html", ".yml", ".yaml", ".example"}
        skipped_dirs = {".venv", ".git", "__pycache__", ".pytest_cache"}
        offenders = []
        for path in ROOT.rglob("*"):
            if any(part in skipped_dirs for part in path.relative_to(ROOT).parts):
                continue
            if not path.is_file() or path.suffix not in scan_suffixes:
                continue
            text = path.read_text(encoding="utf-8")
            for old_name in forbidden:
                if old_name in text:
                    offenders.append(f"{path.relative_to(ROOT)}: {old_name}")
        self.assertEqual([], offenders)

    def test_restore_and_review_docs_exist(self):
        self.assertTrue((ROOT / "RESTORE.md").exists())
        self.assertTrue((ROOT / "REVIEW_PROTOCOL.md").exists())
    def test_readme_stays_compact_and_public_facing(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("## Workflow", text)
        self.assertIn("## Status", text)
        self.assertIn("scripts/check_public_tree.py", text)
        self.assertNotIn("one-row anchor sanity-check fixture only", text)
        self.assertNotIn("XS3236970433_valuation_market_history.csv", text)
        self.assertNotIn("--output /tmp/XS3236970433_joined_market_history.csv", text)


if __name__ == "__main__":
    unittest.main()
