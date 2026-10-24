import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cb_terminal.io.contract_loader import load_contract_json
from cb_terminal.web.server import build_contract_review_payload, edit_contract_terms_payload


class MultiplePutsReviewTests(unittest.TestCase):
    def test_third_scheduled_put_is_visible_and_editable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contract_path = root / "data/contracts/multiple_puts.json"
            contract_path.parent.mkdir(parents=True)
            raw = {
                "id": "multiple_puts",
                "status": "needs_review",
                "issuer": {"name": "Example Issuer", "ticker": "EX"},
                "bond": {
                    "currency": "USD", "settlement_currency": "USD", "stock_currency": "USD",
                    "pricing_face": 100, "issue_size": 1000000, "denomination": 1000,
                    "issue_price": 100, "coupon_rate": 0, "coupon_frequency": 0,
                    "pricing_date": "2026-01-01", "closing_date": "2026-01-05",
                    "maturity_date": "2031-01-01",
                },
                "redemption": {"maturity_price": 100},
                "conversion": {"underlying_ticker": "EX", "initial_conversion_price": 20},
                "puts": [
                    {"type": "holder", "model_type": "scheduled_put", "date": "2028-01-01", "price": 100},
                    {"type": "holder", "model_type": "scheduled_put", "date": "2029-01-01", "price": 101},
                    {"type": "holder", "model_type": "scheduled_put", "date": "2030-01-01", "price": None},
                ],
                "source_review": {"review_items": ["Check terms"] * 5},
            }
            contract_path.write_text(json.dumps(raw), encoding="utf-8")
            with patch("cb_terminal.web.server.PROJECT_ROOT", root):
                review = build_contract_review_payload("data/contracts/multiple_puts.json")
                fields = {field["field"]: field for field in review["editable_fields"]}
                self.assertEqual(review["put_model_types"], ["scheduled_put"] * 3)
                self.assertTrue(fields["puts.2.price"]["attention_required"])
                self.assertEqual(fields["puts.2.date"]["value"], "2030-01-01")
                self.assertTrue(fields["puts.2.calculated_yield_to_put"]["read_only"])
                with self.assertRaisesRegex(ValueError, "cannot be edited directly"):
                    edit_contract_terms_payload({
                        "contract_path": "data/contracts/multiple_puts.json",
                        "edits": {"puts.2.calculated_yield_to_put": 1.0}, "confirm": True,
                    })
                with patch("cb_terminal.web.server._sync_review_queue_contract_status"):
                    saved = edit_contract_terms_payload({
                        "contract_path": "data/contracts/multiple_puts.json",
                        "edits": {"puts.2.price": "102"}, "confirm": True,
                    })
                self.assertEqual(saved["new_values"]["puts.2.price"], 102.0)
                loaded = load_contract_json(contract_path)
                self.assertEqual([put.price for put in loaded.puts], [100, 101, 102])


if __name__ == "__main__":
    unittest.main()
