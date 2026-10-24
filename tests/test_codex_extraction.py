import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from cb_terminal.prospectus.auto_ingest import auto_ingest_prospectuses
from cb_terminal.prospectus.codex_extraction import _quote_matches, contracts_from_codex_result, extract_pdf_with_codex
from cb_terminal.prospectus.extraction import ExtractionResult, PageText


def _term(field, value, quote):
    return {"field": field, "value": value, "state": "stated", "page": 1, "quote": quote}


class CodexExtractionTests(unittest.TestCase):
    def _example(self, source):
        page = "UMC two series. Zero coupon. 2028 bonds XS3515562695 due 13 April 2028. 2031 bonds XS3515578006 due 13 October 2031."
        extraction = ExtractionResult.from_pages(source_path=source, pages=[PageText(1, page)], method="pymupdf")
        result = {"offered_tranche_count": 2, "notes": [], "tranches": []}
        for label, isin, maturity, quote in (
            ("2028", "XS3515562695", "2028-04-13", "XS3515562695 due 13 April 2028"),
            ("2031", "XS3515578006", "2031-10-13", "XS3515578006 due 13 October 2031"),
        ):
            result["tranches"].append({"label": label, "notes": [], "terms": [
                _term("issuer.name", "United Microelectronics Corporation", "UMC two series"),
                _term("instrument.canonical_id", isin, quote),
                _term("bond.coupon_rate", "0", "Zero coupon"),
                _term("bond.maturity_date", maturity, quote),
            ]})
        return extraction, result

    def test_two_tranches_have_distinct_ids_and_verified_evidence(self):
        source = Path("UMC.pdf")
        extraction, result = self._example(source)
        contracts = contracts_from_codex_result(result, extraction, source_file=str(source))
        self.assertEqual([item["id"] for item in contracts], [
            "XS3515562695_contract", "XS3515578006_contract"])
        self.assertEqual([item["bond"]["maturity_date"] for item in contracts], [
            "2028-04-13", "2031-10-13"])
        self.assertEqual([item["instrument"]["display_name"] for item in contracts], [
            "UMC 0 28", "UMC 0 31"])
        self.assertEqual(contracts[0]["instrument"]["issuer_short_name"], "UMC")
        self.assertEqual(contracts[0]["source_review"]["term_evidence"]["bond.maturity_date"][0]["page"], 1)

    def test_single_cig_tranche_defaults_to_short_coupon_year_name(self):
        source = Path("CIG_Pricing Termsheet.pdf")
        page = "CIG SHANGHAI CO., LTD. Zero coupon. Bonds due 12 October 2027. Zero Coupon CNH Linked Convertible Bonds."
        extraction = ExtractionResult.from_pages(source_path=source, pages=[PageText(1, page)], method="pymupdf")
        result = {"offered_tranche_count": 1, "notes": [], "tranches": [{"label": "2027", "notes": [], "terms": [
            _term("issuer.name", "CIG SHANGHAI CO., LTD.", "CIG SHANGHAI CO., LTD."),
            _term("bond.coupon_rate", "0", "Zero coupon"),
            _term("bond.maturity_date", "2027-10-12", "Bonds due 12 October 2027"),
            _term("instrument.series_label", "Zero Coupon CNH Linked Convertible Bonds", "Zero Coupon CNH Linked Convertible Bonds"),
        ]}]}

        contract = contracts_from_codex_result(result, extraction, source_file=str(source))[0]
        self.assertEqual(contract["instrument"]["issuer_short_name"], "CIG")
        self.assertEqual(contract["instrument"]["display_name"], "CIG 0 27")

    def test_exchangeable_defaults_to_issuer_underlying_coupon_year(self):
        source = Path("Baidu EB.pdf")
        page = "Baidu, Inc. Zero Coupon Exchangeable Bonds due 2030, exchangeable into ordinary shares of Trip.com Group Limited (9961 HK)."
        extraction = ExtractionResult.from_pages(source_path=source, pages=[PageText(1, page)], method="pymupdf")
        result = {"offered_tranche_count": 1, "notes": [], "tranches": [{"label": "2030", "notes": [], "terms": [
            _term("issuer.name", "Baidu, Inc.", "Baidu, Inc."),
            _term("instrument.structure_type", "exchangeable_bond", "Exchangeable Bonds"),
            _term("exchangeable_terms.reference_company_name", "Trip.com Group Limited", "Trip.com Group Limited"),
            _term("conversion.underlying_ticker", "9961 HK", "9961 HK"),
            _term("bond.coupon_rate", "0", "Zero Coupon"),
            _term("bond.maturity_date", "2030-01-01", "Bonds due 2030"),
        ]}]}

        contract = contracts_from_codex_result(result, extraction, source_file=str(source))[0]
        self.assertEqual(contract["instrument"]["structure_type"], "exchangeable_bond")
        self.assertEqual(contract["exchangeable_terms"]["reference_company_name"], "Trip.com Group Limited")
        self.assertEqual(contract["instrument"]["display_name"], "Baidu/Trip 0 30")

    def test_page_quote_tolerates_pdf_spacing(self):
        extraction = ExtractionResult.from_pages(source_path=Path("sample.pdf"),
            pages=[PageText(1, "Conversion\nPrice: HK$ 128.82")], method="pymupdf")
        self.assertTrue(_quote_matches(extraction, 1, "Conversion Price HK$128.82"))

    def test_codex_launch_uses_resolved_windows_command(self):
        source = Path("UMC.pdf")
        extraction, result = self._example(source)
        command_path = r"C:\Program Files\Codex\codex.cmd"
        def run(command, **_kwargs):
            self.assertEqual(command[0], command_path)
            self.assertEqual(command[command.index("-m") + 1], "gpt-6-sol")
            Path(command[command.index("-o") + 1]).write_text(json.dumps(result), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")
        with patch("cb_terminal.prospectus.codex_extraction.shutil.which", return_value=command_path), patch(
            "cb_terminal.prospectus.codex_extraction.subprocess.run", side_effect=run
        ), patch.dict(sys.modules, {"fitz": None}):
            contracts = extract_pdf_with_codex(source, extraction)
        self.assertEqual(len(contracts), 2)
        self.assertEqual(contracts[0]["source_review"]["ai_model"], "gpt-6-sol")

    def test_reextract_preserves_gui_edits_and_creates_missing_sibling(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            raw = root / "raw"
            raw.mkdir()
            source = raw / "UMC.pdf"
            source.write_bytes(b"%PDF-1.7\n%%EOF\n")
            extraction, result = self._example(source)
            initial = contracts_from_codex_result(deepcopy(result), extraction, source_file=str(source))[:1]
            refreshed = contracts_from_codex_result(deepcopy(result), extraction, source_file=str(source))
            contracts_dir, reviews_dir, coverage_dir = (root / name for name in ("contracts", "reviews", "coverage"))
            with patch("cb_terminal.prospectus.auto_ingest._load_fixture_or_extract", return_value=extraction), patch(
                "cb_terminal.prospectus.auto_ingest.extract_pdf_with_codex", side_effect=[initial, refreshed]
            ):
                first = auto_ingest_prospectuses(prospectus_dir=raw, contracts_dir=contracts_dir,
                                                 reviews_dir=reviews_dir, coverage_dir=coverage_dir)
                self.assertEqual(first.created_contracts, 1)
                first_path = contracts_dir / "XS3515562695_contract.json"
                edited = json.loads(first_path.read_text(encoding="utf-8"))
                edited["conversion"]["initial_conversion_price"] = 180.5
                edited["source_review"]["human_edited_fields"] = ["conversion.initial_conversion_price"]
                edited["source_review"]["reviewer_notes"] = "Check conversion window"
                edited["source_review"]["extraction_fallback"] = "Old parser draft"
                first_path.write_text(json.dumps(edited), encoding="utf-8")
                second = auto_ingest_prospectuses(prospectus_dir=raw, contracts_dir=contracts_dir,
                                                  reviews_dir=reviews_dir, coverage_dir=coverage_dir, reextract=True)
            saved = json.loads(first_path.read_text(encoding="utf-8"))
            self.assertEqual((second.created_contracts, second.updated_contracts), (1, 1))
            self.assertEqual(saved["conversion"]["initial_conversion_price"], 180.5)
            self.assertEqual(saved["source_review"]["reviewer_notes"], "Check conversion window")
            self.assertNotIn("extraction_fallback", saved["source_review"])
            self.assertEqual(saved["bond"]["maturity_date"], "2028-04-13")
            self.assertTrue((contracts_dir / "XS3515578006_contract.json").exists())
            self.assertTrue((contracts_dir / "XS3515562695_contract.json.pre_ai_reextract.bak").exists())
            self.assertTrue((reviews_dir / "XS3515562695_contract_review.md.pre_ai_reextract.bak").exists())
            self.assertIn("Codex AI", (reviews_dir / "XS3515562695_contract_review.md").read_text(encoding="utf-8"))
            saved["status"] = "reviewed"
            first_path.write_text(json.dumps(saved), encoding="utf-8")
            with patch("cb_terminal.prospectus.auto_ingest._load_fixture_or_extract", return_value=extraction), patch(
                "cb_terminal.prospectus.auto_ingest.extract_pdf_with_codex", return_value=refreshed
            ):
                auto_ingest_prospectuses(prospectus_dir=raw, contracts_dir=contracts_dir,
                                        reviews_dir=reviews_dir, coverage_dir=coverage_dir, reextract=True)
            self.assertEqual(json.loads(first_path.read_text(encoding="utf-8")), saved)
            self.assertTrue((reviews_dir / "proposals" / "XS3515562695_contract_ai_proposal.json").exists())

    def test_missing_codex_cli_uses_flagged_legacy_parser(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            raw = root / "raw"
            raw.mkdir()
            source = raw / "UMC.pdf"
            source.write_bytes(b"%PDF-1.7\n%%EOF\n")
            extraction, result = self._example(source)
            legacy = contracts_from_codex_result(result, extraction, source_file=str(source))[:1]
            legacy[0]["source_review"].pop("ai_extractor_version")
            with patch("cb_terminal.prospectus.auto_ingest._load_fixture_or_extract", return_value=extraction), patch(
                "cb_terminal.prospectus.auto_ingest.codex_available", return_value=False
            ), patch("cb_terminal.prospectus.auto_ingest.draft_contracts_from_text", return_value=legacy), patch(
                "cb_terminal.prospectus.auto_ingest.extract_pdf_with_codex"
            ) as codex:
                report = auto_ingest_prospectuses(prospectus_dir=raw, contracts_dir=root / "contracts",
                                                 reviews_dir=root / "reviews", coverage_dir=root / "coverage")
            codex.assert_not_called()
            self.assertEqual(report.created_contracts, 1)
            saved = json.loads((root / "contracts" / "XS3515562695_contract.json").read_text(encoding="utf-8"))
            self.assertIn("legacy text parser", saved["source_review"]["extraction_fallback"])

    def test_codex_launch_failure_does_not_create_a_parser_draft(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            raw = root / "raw"
            raw.mkdir()
            source = raw / "UMC.pdf"
            source.write_bytes(b"%PDF-1.7\n%%EOF\n")
            extraction, _ = self._example(source)
            with patch("cb_terminal.prospectus.auto_ingest._load_fixture_or_extract", return_value=extraction), patch(
                "cb_terminal.prospectus.auto_ingest.codex_available", return_value=True
            ), patch("cb_terminal.prospectus.auto_ingest.extract_pdf_with_codex", side_effect=RuntimeError("login needed")), patch(
                "cb_terminal.prospectus.auto_ingest.draft_contracts_from_text"
            ) as parser:
                report = auto_ingest_prospectuses(prospectus_dir=raw, contracts_dir=root / "contracts",
                                                 reviews_dir=root / "reviews", coverage_dir=root / "coverage")
            parser.assert_not_called()
            self.assertEqual((report.created_contracts, report.failed), (0, 1))
            self.assertIn("login needed", report.items[0]["message"])


if __name__ == "__main__":
    unittest.main()
