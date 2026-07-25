import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cb_terminal.web.server import edit_contract_terms_payload


class ContractReviewEditTests(unittest.TestCase):
    def _write_contract(self, root: Path) -> Path:
        path = root / "data/contracts/example.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "id": "example",
                    "status": "reviewed",
                    "bond": {
                        "issue_price": 100.0,
                        "brokerage": 0.5,
                        "investor_offer_price": 100.5,
                    },
                    "puts": [
                        {
                            "type": "change_of_control",
                            "model_type": "event_put",
                            "price": 100.0,
                        }
                    ],
                    "source_review": {"review_status": "reviewed"},
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_edit_recomputes_offer_and_returns_contract_to_review(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = self._write_contract(root)
            with (
                patch("cb_terminal.web.server.PROJECT_ROOT", root),
                patch("cb_terminal.web.server.validate_contract_dict", return_value=[]),
                patch("cb_terminal.web.server.loads_contract_json"),
                patch("cb_terminal.web.server._sync_review_queue_contract_status"),
            ):
                payload = edit_contract_terms_payload(
                    {
                        "contract_path": "data/contracts/example.json",
                        "edits": {"bond.brokerage": "0.75"},
                        "confirm": True,
                    }
                )
                first_edit = json.loads(path.read_text(encoding="utf-8"))
                edit_contract_terms_payload(
                    {
                        "contract_path": "data/contracts/example.json",
                        "edits": {"bond.issue_price": "101.0"},
                        "confirm": True,
                    }
                )

            edited = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(first_edit["bond"]["investor_offer_price"], 100.75)
            self.assertEqual(edited["bond"]["investor_offer_price"], 101.75)
            self.assertEqual(edited["status"], "needs_review")
            self.assertEqual(
                edited["source_review"]["review_status"],
                "edited_needs_human_review",
            )
            self.assertIn("bond.investor_offer_price", payload["edited_fields"])
            self.assertEqual(
                set(edited["source_review"]["human_edited_fields"]),
                {
                    "bond.brokerage",
                    "bond.investor_offer_price",
                    "bond.issue_price",
                },
            )

    def test_edit_rejects_direct_offer_and_event_put_yield(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._write_contract(root)
            with patch("cb_terminal.web.server.PROJECT_ROOT", root):
                with self.assertRaisesRegex(ValueError, "cannot be edited directly"):
                    edit_contract_terms_payload(
                        {
                            "contract_path": "data/contracts/example.json",
                            "edits": {"bond.investor_offer_price": "101"},
                            "confirm": True,
                        }
                    )
                with self.assertRaisesRegex(ValueError, "scheduled holder puts"):
                    edit_contract_terms_payload(
                        {
                            "contract_path": "data/contracts/example.json",
                            "edits": {
                                "puts.0.yield_to_put": "1.5",
                                "puts.0.yield_to_put_frequency": "1",
                            },
                            "confirm": True,
                        }
                    )


if __name__ == "__main__":
    unittest.main()
