import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from cb_terminal.prospectus.review import ReviewIssue
from cb_terminal.storage.canonical_store import CanonicalStore
from cb_terminal.web.server import approve_contract_terms_payload, build_contract_review_payload, edit_contract_terms_payload


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

    def test_pm_name_edit_is_shown_after_save_and_reload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = self._write_contract(root)
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["instrument"] = {
                "display_name": "Old PM name",
                "issuer_short_name": "Old Issuer",
                "maturity_year": 2028,
            }
            raw["issuer"] = {"name": "Old Issuer Limited"}
            raw["bond"].update({"coupon_rate": 0, "maturity_date": "2028-04-13"})
            path.write_text(json.dumps(raw), encoding="utf-8")

            with (
                patch("cb_terminal.web.server.PROJECT_ROOT", root),
                patch("cb_terminal.web.server.validate_contract_dict", return_value=[]),
                patch("cb_terminal.web.server.loads_contract_json"),
                patch("cb_terminal.web.server._sync_review_queue_contract_status"),
            ):
                edit_contract_terms_payload({
                    "contract_path": "data/contracts/example.json",
                    "edits": {"instrument.display_name": "New PM name"},
                    "confirm": True,
                })
                review = build_contract_review_payload("data/contracts/example.json")

            saved = json.loads(path.read_text(encoding="utf-8"))
            pm_field = next(field for field in review["editable_fields"]
                            if field["field"] == "instrument.display_name")
            self.assertEqual(saved["instrument"]["display_name"], "New PM name")
            self.assertEqual(pm_field["value"], "New PM name")
            self.assertEqual(review["instrument_display_name"], "New PM name")
            self.assertEqual(review["instrument_short_name"], "Old Issuer 0 28")

    def test_isin_edit_rekeys_contract_and_review_references(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = root / "data/contracts/draft_contract.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({
                "id": "draft_contract", "status": "needs_review",
                "instrument": {"canonical_id_type": "PENDING_ISIN", "canonical_id": ""},
                "source_review": {},
            }), encoding="utf-8")
            review = root / "data/reviews/draft_contract_review.md"
            review.parent.mkdir(parents=True)
            review.write_text("# draft_contract\n", encoding="utf-8")
            queue = root / "data/coverage/review_queue.json"
            queue.parent.mkdir(parents=True)
            queue.write_text(json.dumps([{
                "contract_id": "draft_contract",
                "contract_path": "data/contracts/draft_contract.json",
                "review_path": "data/reviews/draft_contract_review.md",
            }]), encoding="utf-8")
            with (
                patch("cb_terminal.web.server.PROJECT_ROOT", root),
                patch("cb_terminal.web.server.validate_contract_dict", return_value=[]),
                patch("cb_terminal.web.server.loads_contract_json"),
            ):
                result = edit_contract_terms_payload({
                    "contract_path": "data/contracts/draft_contract.json",
                    "edits": {"instrument.canonical_id_type": "ISIN", "instrument.canonical_id": "XS3462009369"},
                    "confirm": True,
                })
            new_path = root / "data/contracts/XS3462009369_contract.json"
            self.assertEqual(result["contract_id"], "XS3462009369_contract")
            self.assertTrue(new_path.exists())
            self.assertFalse(path.exists())
            self.assertEqual(json.loads(new_path.read_text(encoding="utf-8"))["id"], "XS3462009369_contract")
            self.assertTrue((root / "data/reviews/XS3462009369_contract_review.md").exists())
            updated_queue = json.loads(queue.read_text(encoding="utf-8"))[0]
            self.assertEqual(updated_queue["contract_id"], "XS3462009369_contract")
            self.assertEqual(updated_queue["contract_path"], "data/contracts/XS3462009369_contract.json")

    def test_overwriting_existing_isin_updates_internal_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = root / "data/contracts/XS3462009369_contract.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({
                "id": "XS3462009369_contract", "isin": "XS3462009369", "status": "reviewed",
                "instrument": {"canonical_id_type": "ISIN", "canonical_id": "XS3462009369"},
                "source_review": {},
            }), encoding="utf-8")
            CanonicalStore(root / "data/cb_terminal.sqlite").upsert_contract(
                json.loads(path.read_text(encoding="utf-8")),
                source_path="data/contracts/XS3462009369_contract.json",
            )
            with (
                patch("cb_terminal.web.server.PROJECT_ROOT", root),
                patch("cb_terminal.web.server.validate_contract_dict", return_value=[]),
                patch("cb_terminal.web.server.loads_contract_json"),
            ):
                result = edit_contract_terms_payload({
                    "contract_path": "data/contracts/XS3462009369_contract.json",
                    "edits": {"instrument.canonical_id": "XS3515562695"},
                    "confirm": True,
                })
            new_path = root / "data/contracts/XS3515562695_contract.json"
            saved = json.loads(new_path.read_text(encoding="utf-8"))
            self.assertEqual(result["contract_id"], "XS3515562695_contract")
            self.assertEqual(saved["id"], "XS3515562695_contract")
            self.assertEqual(saved["isin"], "XS3515562695")
            self.assertFalse(path.exists())
            with closing(sqlite3.connect(root / "data/cb_terminal.sqlite")) as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM contracts WHERE contract_id=?", ("XS3462009369_contract",)).fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT count(*) FROM contracts WHERE contract_id=?", ("XS3515562695_contract",)).fetchone()[0], 1)

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

    def test_double_checked_unsourced_value_can_be_approved(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = self._write_contract(root)
            warning = ReviewIssue("warning", "source_review.term_evidence", "Missing source evidence")
            request = {"contract_path": "data/contracts/example.json", "confirm": True}
            with (
                patch("cb_terminal.web.server.PROJECT_ROOT", root),
                patch("cb_terminal.web.server.validate_contract_dict", return_value=[warning]),
                patch("cb_terminal.web.server.missing_approval_evidence_fields", return_value=["bond.issue_price"]),
                patch("cb_terminal.web.server.loads_contract_json"),
                patch("cb_terminal.web.server._sync_review_queue_contract_status"),
            ):
                with self.assertRaisesRegex(ValueError, "double-check unsourced values"):
                    approve_contract_terms_payload(request)
                approve_contract_terms_payload({**request, "source_values_checked": True})
                review = build_contract_review_payload(request["contract_path"])
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "reviewed")
            self.assertEqual(saved["source_review"]["source_values_checked"]["fields"], {"bond.issue_price": 100.0})
            issue_price = next(item for item in review["editable_fields"] if item["field"] == "bond.issue_price")
            self.assertEqual(issue_price["evidence_status"], "checked")
            self.assertFalse(issue_price["attention_required"])
            with (
                patch("cb_terminal.web.server.PROJECT_ROOT", root),
                patch("cb_terminal.web.server.validate_contract_dict", return_value=[ReviewIssue("error", "bond.issue_price", "invalid")]),
                patch("cb_terminal.web.server.missing_approval_evidence_fields", return_value=["bond.issue_price"]),
            ):
                with self.assertRaisesRegex(ValueError, "contract approval blocked"):
                    approve_contract_terms_payload({**request, "source_values_checked": True})



if __name__ == "__main__":
    unittest.main()
