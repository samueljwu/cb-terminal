import csv
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

from cb_terminal.storage.price_history_store import PriceHistoryStore
from cb_terminal.web import server


class MarketUploadDetectionTests(unittest.TestCase):
    def test_merge_rejects_duplicate_dates_instead_of_collapsing_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = root / "existing.csv"
            incoming = root / "incoming.csv"
            merged = root / "merged.csv"
            existing.write_text(
                "date,stock_price,bond_price\n"
                "2026-05-22,50,101.5\n"
                "2026-05-22,51,102.0\n",
                encoding="utf-8",
            )
            incoming.write_text(
                "date,stock_price,bond_price\n"
                "2026-05-23,52,103.0\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "duplicate market-history date"):
                server._merge_valuation_market_history_files(existing, incoming, merged)

            self.assertFalse(merged.exists())

    def test_concurrent_valuation_uploads_do_not_lose_each_others_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = root / "linked.csv"
            first = root / "first.csv"
            second = root / "second.csv"
            header = "date,stock_price,bond_price\n"
            existing.write_text(header + "2026-05-22,50,101.5\n", encoding="utf-8")
            first.write_text(header + "2026-05-23,51,102.0\n", encoding="utf-8")
            second.write_text(header + "2026-05-24,52,103.0\n", encoding="utf-8")
            state = {"linked_path": existing}
            start = threading.Barrier(2)
            contract = MagicMock(id="TEST_contract")

            def linked_history(_contract_path):
                current = state["linked_path"]
                time.sleep(0.05)
                return current

            def output_path(_contract_id, explicit):
                return root / Path(str(explicit)).name

            def validate(path, _contract):
                rows = server.load_market_history_csv(path)
                result = MagicMock(row_count=len(rows), checked_identity_rows=0)
                result.raise_for_errors.return_value = None
                return result

            def link_source(_kind, source, _payload):
                state["linked_path"] = source
                return {"source_path": str(source)}

            def promote(path):
                start.wait()
                return server._promote_uploaded_valuation_history(
                    path,
                    "data/contracts/test.json",
                    {"confirm_overwrite": True},
                )

            with (
                patch.object(server, "load_contract_json", return_value=contract),
                patch.object(server, "validate_market_history_file_for_contract", side_effect=validate),
                patch.object(server, "_valuation_history_output_path", side_effect=output_path),
                patch.object(server, "_linked_market_history_path_for_contract", side_effect=linked_history),
                patch.object(server, "_link_source_to_universe", side_effect=link_source),
            ):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = list(executor.map(promote, (first, second)))

            with state["linked_path"].open("r", newline="", encoding="utf-8") as handle:
                dates = [row["date"] for row in csv.DictReader(handle)]

        self.assertEqual(len(results), 2)
        self.assertEqual(dates, ["2026-05-22", "2026-05-23", "2026-05-24"])

    def test_same_date_alternate_spread_units_replace_the_old_representation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = root / "existing.csv"
            incoming = root / "incoming.csv"
            merged = root / "merged.csv"
            existing.write_text(
                "date,stock_price,bond_price,credit_spread\n"
                "2026-05-23,51,102.0,0.02\n",
                encoding="utf-8",
            )
            incoming.write_text(
                "date,stock_price,bond_price,credit_spread_bps\n"
                "2026-05-23,51.5,102.25,300\n",
                encoding="utf-8",
            )

            server._merge_valuation_market_history_files(existing, incoming, merged)
            with merged.open("r", newline="", encoding="utf-8") as handle:
                row = next(csv.DictReader(handle))

        self.assertEqual(row["credit_spread"], "")
        self.assertEqual(row["credit_spread_bps"], "300")
        self.assertEqual(row["bond_price"], "102.25")

    def test_generated_history_preserves_linked_dates_and_fields_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = root / "linked.csv"
            output = root / "generated.csv"
            revision = root / "generated-1.csv"
            existing.write_text(
                "date,stock_price,bond_price,volatility\n"
                "2026-05-22,50,101.5,0.35\n"
                "2026-05-23,51,102.0,0.36\n",
                encoding="utf-8",
            )
            output.write_text("owned by another linked contract\n", encoding="utf-8")
            generated_rows = [
                {"date": "2026-05-23", "stock_price": 51.5, "bond_price": 102.25},
                {"date": "2026-05-24", "stock_price": 52, "bond_price": 103.0},
            ]
            requirements = {
                "contract_path": "data/contracts/test.json",
                "contract": {},
                "terms_approved": True,
                "contract_id": "TEST_contract",
                "cb_instrument_id": "XS0000000001",
                "equity_instrument_id": "TEST US Equity",
                "fx_instrument_id": "",
                "stock_currency": "USD",
                "bond_price_currency": "USD",
                "fx_convention": "stock_to_bond",
            }
            readiness = {
                "components": {
                    "cb_quote_history": {"status": "ready"},
                    "stock_history": {"status": "ready"},
                    "fx_history": {"status": "not_required"},
                },
                "missing": [],
                "requirements": {},
            }
            store = MagicMock()
            store.build_valuation_market_rows.return_value = generated_rows
            validation = MagicMock(row_count=3, checked_identity_rows=0)
            validation.raise_for_errors.return_value = None
            canonical_persist = {"canonical_series_id": 7, "canonical_row_count": 3}
            commit_events = []

            def persist_generated(*_args):
                commit_events.append("canonical")
                return canonical_persist

            def link_generated(*_args):
                commit_events.append("universe")
                return {}

            with (
                patch.object(server, "_contract_market_requirements", return_value=requirements),
                patch.object(server, "_price_history_store", return_value=store),
                patch.object(server, "market_generation_readiness_payload", return_value=readiness),
                patch.object(server, "_valuation_history_output_path", return_value=output),
                patch.object(server, "_linked_market_history_path_for_contract", return_value=existing),
                patch.object(server, "validate_market_history_file_for_contract", return_value=validation),
                patch.object(server, "_link_source_to_universe", side_effect=link_generated),
                patch.object(
                    server,
                    "_persist_generated_valuation_rows_to_canonical_store",
                    side_effect=persist_generated,
                ) as persist_rows,
            ):
                result = server.generate_valuation_market_history_payload(
                    {
                        "confirm": True,
                        "confirm_overwrite": True,
                        "output_path": "data/price_history/generated/generated.csv",
                    }
                )

            with revision.open("r", newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            reserved_output = output.read_text(encoding="utf-8")

        self.assertEqual(result["status"], "ready")
        self.assertEqual(Path(result["output_path"]), revision)
        self.assertEqual(reserved_output, "owned by another linked contract\n")
        self.assertEqual(
            result["merge_summary"],
            {
                "existing_date_count": 2,
                "incoming_date_count": 2,
                "added_date_count": 1,
                "updated_date_count": 1,
                "preserved_date_count": 1,
                "merged_date_count": 3,
            },
        )
        self.assertEqual([row["date"] for row in rows], ["2026-05-22", "2026-05-23", "2026-05-24"])
        self.assertEqual(rows[0]["bond_price"], "101.5")
        self.assertEqual(rows[1]["bond_price"], "102.25")
        self.assertEqual(rows[1]["volatility"], "0.36")
        persisted_rows = persist_rows.call_args.args[2]
        self.assertEqual([row["date"] for row in persisted_rows], ["2026-05-22", "2026-05-23", "2026-05-24"])
        self.assertEqual(commit_events, ["canonical", "universe"])

    def test_valuation_history_upload_adds_new_dates_and_updates_only_matching_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = root / "existing.csv"
            incoming = root / "incoming.csv"
            merged = root / "merged.csv"
            header = "date,stock_price,bond_price,cb_instrument_id,cb_reference_security,volatility\n"
            existing.write_text(
                header
                + "2026-05-22,50,101.5,XS0000000001,Test CB,0.35\n"
                + "2026-05-23,51,102.0,XS0000000001,Test CB,0.36\n",
                encoding="utf-8",
            )
            incoming.write_text(
                header
                + "2026-05-23,51.5,102.25,XS0000000001,Test CB,\n"
                + "2026-05-24,52,103.0,XS0000000001,Test CB,0.38\n",
                encoding="utf-8",
            )

            summary = server._merge_valuation_market_history_files(existing, incoming, merged)
            with merged.open("r", newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(
            summary,
            {
                "existing_date_count": 2,
                "incoming_date_count": 2,
                "added_date_count": 1,
                "updated_date_count": 1,
                "preserved_date_count": 1,
                "merged_date_count": 3,
            },
        )
        self.assertEqual([row["date"] for row in rows], ["2026-05-22", "2026-05-23", "2026-05-24"])
        self.assertEqual(rows[0]["bond_price"], "101.5")
        self.assertEqual(rows[1]["bond_price"], "102.25")
        self.assertEqual(rows[1]["volatility"], "0.36")

    def test_successful_quote_detection_hides_expected_alternate_parser_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "quotes.csv"
            source.write_text(
                "Reference Security,Date,Time,Ticker,Dealer,Source,ISIN,Security,Bid Price,Ask Price,Stock Price,Currency\n"
                "TEST Corp,05/22/26,15:30,TEST,CSEC,RUN,XS0000000001,TEST 0 01/01/31,101,102,50,HKD\n",
                encoding="utf-8",
            )
            store = PriceHistoryStore(root / "quotes.sqlite")
            result = {"sha256": "test"}

            with (
                patch.object(server, "_price_history_store", return_value=store),
                patch.object(server, "load_market_data_file", side_effect=ValueError("not an equity/FX layout")),
            ):
                server._attach_auto_market_data_parse_summary(
                    result,
                    source,
                    {"contract_id": "WRONG_SELECTED_contract"},
                )
                imported = store.latest_quotes(instrument_id="XS0000000001", limit=1)

        self.assertEqual(result["detected_market_data_types"], ["cb_quote_history"])
        self.assertEqual(result["market_data_breakdown"]["cb_quote_rows"], 1)
        self.assertEqual(result["warnings"], [])
        self.assertEqual(imported[0]["contract_id"], "")

    def test_previously_uploaded_quote_matches_when_contract_appears_later(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "quotes.csv"
            source.write_text(
                "Reference Security,Date,ISIN,Bid Price,Ask Price\n"
                "LATE Corp,05/22/26,XS0000000099,101,102\n",
                encoding="utf-8",
            )
            detected = {"detected_market_data_types": ["cb_quote_history"]}
            with (
                patch.object(server, "_load_json_list", return_value=[]),
                patch.object(server, "_iter_contract_json_files", return_value=[]),
            ):
                before = server._market_source_matches_for_upload(source, detected)

            contract_path = server.PROJECT_ROOT / "data" / "contracts" / "_late_contract.json"
            contract = {
                "id": "LATE_contract",
                "instrument": {
                    "canonical_id_type": "ISIN",
                    "canonical_id": "XS0000000099",
                },
            }
            with (
                patch.object(server, "_load_json_list", return_value=[]),
                patch.object(
                    server,
                    "_iter_contract_json_files",
                    return_value=[(contract_path, contract)],
                ),
            ):
                after = server._market_source_matches_for_upload(source, detected)

        self.assertEqual(before["cb_quotes"][0]["matched_contract_paths"], [])
        self.assertEqual(
            after["cb_quotes"][0]["matched_contract_paths"],
            [str(Path("data/contracts/_late_contract.json"))],
        )


if __name__ == "__main__":
    unittest.main()
