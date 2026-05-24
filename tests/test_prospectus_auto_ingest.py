import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from cb_terminal.prospectus.auto_ingest import (
    approve_reviewed_contract,
    auto_ingest_prospectuses,
    contract_instrument_key,
    sha256_file,
)

ROOT = Path(__file__).resolve().parents[1]

GENERIC_PAGES = {
    "pages": [
        {
            "page": 1,
            "text": """
            Example Issuer Ltd.
            US$500,000,000 Zero Coupon Convertible Bonds due 2030
            The Bonds are issued in denominations of US$200,000 each.
            The initial Conversion Price is HK$42.50 per Share.
            The fixed exchange rate is HK$7.80 = US$1.00.
            The Bonds will mature on 15 May 2030 and will be redeemed at 100 per cent.
            The conversion period shall commence on 30 June 2026 and end on 5 May 2030.
            The Shares are listed on The Stock Exchange of Hong Kong under stock code 1234.
            """,
        }
    ]
}


class AutoIngestProspectusTests(unittest.TestCase):
    def test_auto_ingest_creates_needs_review_contract_from_page_fixture(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            raw = root / "data/raw/prospectuses"
            contracts = root / "data/contracts"
            reviews = root / "data/prospectus_reviews"
            fixtures = root / "data/prospectus_text"
            coverage = root / "data/coverage"
            raw.mkdir(parents=True)
            fixtures.mkdir(parents=True)
            pdf = raw / "Example Issuer - Final Offering Circular.pdf"
            pdf.write_bytes(b"%PDF-1.7\n%%EOF\n")
            fixture = fixtures / "example_issuer_final_offering_circular_pages_seed.json"
            fixture.write_text(json.dumps(GENERIC_PAGES), encoding="utf-8")

            report = auto_ingest_prospectuses(
                prospectus_dir=raw,
                contracts_dir=contracts,
                reviews_dir=reviews,
                coverage_dir=coverage,
                fixture_dir=fixtures,
            )

            self.assertEqual(report.created_contracts, 1)
            self.assertEqual(report.duplicates, 0)
            self.assertEqual(report.needs_extraction, 0)
            contract_path = contracts / "example_issuer_ltd_2030_cb.json"
            self.assertTrue(contract_path.exists())
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
            self.assertEqual(contract["status"], "needs_review")
            self.assertEqual(contract["issuer"]["name"], "Example Issuer Ltd.")
            self.assertEqual(contract["bond"]["currency"], "USD")
            self.assertEqual(contract["bond"]["stock_currency"], "HKD")
            self.assertEqual(contract["conversion"]["underlying_ticker"], "1234 HK")
            self.assertEqual(contract["source_review"]["raw_prospectus_sha256"], sha256_file(pdf))
            self.assertTrue((reviews / "example_issuer_ltd_2030_cb_review.md").exists())
            queue = json.loads((coverage / "review_queue.json").read_text(encoding="utf-8"))
            self.assertEqual(queue[0]["status"], "contract_available_needs_review")

    def test_auto_ingest_writes_multiple_contracts_for_multi_series_pdf(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            raw = root / "data/raw/prospectuses"
            contracts = root / "data/contracts"
            reviews = root / "data/prospectus_reviews"
            fixtures = root / "data/prospectus_text"
            coverage = root / "data/coverage"
            raw.mkdir(parents=True)
            fixtures.mkdir(parents=True)
            pdf = raw / "JX Advanced Metals - Final Offering Circular.pdf"
            pdf.write_bytes(b"%PDF-1.7\n%%EOF\n")
            fixture = fixtures / "jx_advanced_metals_final_offering_circular_pages_seed.json"
            fixture.write_text(
                json.dumps(
                    {
                        "pages": [
                            {
                                "page": 3,
                                "text": """
                                OFFERING CIRCULAR
                                JX Advanced Metals Corporation
                                ¥125,000,000,000 Zero Coupon Convertible Bonds due 2029
                                OFFER PRICE: 113.25%
                                ¥125,000,000,000 Zero Coupon Convertible Bonds due 2031
                                OFFER PRICE: 114.00%
                                This offering circular relates to the issue by JX Advanced Metals Corporation of ¥125,000,000,000 in aggregate principal amount
                                of Zero Coupon Convertible Bonds due 2029 (the "2029 Bonds") and ¥125,000,000,000 in aggregate principal amount of Zero Coupon Convertible Bonds due 2031.
                                The Stock Acquisition Rights may acquire shares at an initial conversion price of ¥4,860 per Share, in the case of the 2029 Bonds,
                                and ¥4,860 per Share, in the case of the 2031 Bonds. The Bonds will be redeemed at 100% of their principal amount on June 4, 2029 in the case
                                of the 2029 Bonds, and June 3, 2031 in the case of the 2031 Bonds.
                                """,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            report = auto_ingest_prospectuses(
                prospectus_dir=raw,
                contracts_dir=contracts,
                reviews_dir=reviews,
                coverage_dir=coverage,
                fixture_dir=fixtures,
            )

            self.assertEqual(report.created_contracts, 2)
            self.assertTrue((contracts / "jx_advanced_metals_corporation_2029_cb.json").exists())
            self.assertTrue((contracts / "jx_advanced_metals_corporation_2031_cb.json").exists())
            queue = json.loads((coverage / "review_queue.json").read_text(encoding="utf-8"))
            self.assertEqual({item.get("contract_id") for item in queue}, {"jx_advanced_metals_corporation_2029_cb", "jx_advanced_metals_corporation_2031_cb"})
            for item in queue:
                self.assertEqual(item["status"], "contract_available_needs_review")
                contract = json.loads(Path(item["contract_path"]).read_text(encoding="utf-8"))
                self.assertIn("term_evidence", contract["source_review"])
                self.assertIn("bond.maturity_date", contract["source_review"]["term_evidence"])

    def test_auto_ingest_deduplicates_same_instrument_and_records_duplicate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            raw = root / "data/raw/prospectuses"
            contracts = root / "data/contracts"
            reviews = root / "data/prospectus_reviews"
            fixtures = root / "data/prospectus_text"
            coverage = root / "data/coverage"
            raw.mkdir(parents=True)
            fixtures.mkdir(parents=True)
            for name in ["Example Issuer - Final Offering Circular.pdf", "Example Issuer duplicate.pdf"]:
                (raw / name).write_bytes(b"%PDF-1.7\n%%EOF\n" + name.encode())
            for prospectus_id in ["example_issuer_final_offering_circular", "example_issuer_duplicate"]:
                (fixtures / f"{prospectus_id}_pages_seed.json").write_text(json.dumps(GENERIC_PAGES), encoding="utf-8")

            report = auto_ingest_prospectuses(
                prospectus_dir=raw,
                contracts_dir=contracts,
                reviews_dir=reviews,
                coverage_dir=coverage,
                fixture_dir=fixtures,
            )

            self.assertEqual(report.created_contracts, 1)
            self.assertEqual(report.duplicates, 1)
            contracts_written = list(contracts.glob("*.json"))
            self.assertEqual(len(contracts_written), 1)
            queue = json.loads((coverage / "review_queue.json").read_text(encoding="utf-8"))
            duplicate_items = [item for item in queue if item["status"] == "duplicate_skipped"]
            self.assertEqual(len(duplicate_items), 1)
            self.assertEqual(duplicate_items[0]["duplicate_of"], "example_issuer_ltd_2030_cb")

    def test_unextractable_new_pdf_enters_review_queue_without_contract(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            raw = root / "raw"
            raw.mkdir()
            (raw / "No Text - Final Offering Circular.pdf").write_bytes(b"%PDF-1.7\n%%EOF\n")
            report = auto_ingest_prospectuses(
                prospectus_dir=raw,
                contracts_dir=root / "contracts",
                reviews_dir=root / "reviews",
                coverage_dir=root / "coverage",
                fixture_dir=root / "fixtures",
            )
            self.assertEqual(report.created_contracts, 0)
            self.assertEqual(report.needs_extraction, 1)
            self.assertIn("text_backend_available", report.extraction_environment)
            queue = json.loads((root / "coverage/review_queue.json").read_text(encoding="utf-8"))
            self.assertEqual(queue[0]["status"], "needs_extraction_backend")
            self.assertEqual(queue[0]["blocker"], "backend_failed")
            self.assertEqual(queue[0]["extraction"]["status"], "backend_failed")
            self.assertIsNone(queue[0]["contract_path"])

    def test_blank_text_layer_pdf_enters_needs_ocr_queue_with_page_stats(self):
        try:
            import fitz  # type: ignore
        except Exception:
            self.skipTest("PyMuPDF unavailable")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            raw = root / "raw"
            raw.mkdir()
            pdf = raw / "Scanned Style - Final Offering Circular.pdf"
            doc = fitz.open()
            doc.new_page()
            doc.save(pdf)
            doc.close()
            report = auto_ingest_prospectuses(
                prospectus_dir=raw,
                contracts_dir=root / "contracts",
                reviews_dir=root / "reviews",
                coverage_dir=root / "coverage",
                fixture_dir=root / "fixtures",
            )
            queue = json.loads((root / "coverage/review_queue.json").read_text(encoding="utf-8"))
        self.assertEqual(report.needs_extraction, 1)
        self.assertEqual(queue[0]["status"], "needs_ocr")
        self.assertEqual(queue[0]["blocker"], "scanned_pdf_or_low_text")
        self.assertEqual(queue[0]["extraction"]["status"], "needs_ocr")
        self.assertEqual(queue[0]["extraction"]["page_text_stats"]["low_text_page_count"], 1)

    def test_auto_ingest_can_limit_to_selected_source_paths_without_dropping_queue_items(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            raw = root / "data/raw/prospectuses"
            contracts = root / "data/contracts"
            reviews = root / "data/prospectus_reviews"
            fixtures = root / "data/prospectus_text"
            coverage = root / "data/coverage"
            raw.mkdir(parents=True)
            fixtures.mkdir(parents=True)
            alpha = raw / "Alpha - Final Offering Circular.pdf"
            beta = raw / "Beta - Final Offering Circular.pdf"
            alpha.write_bytes(b"%PDF-1.7 alpha\n%%EOF")
            beta.write_bytes(b"%PDF-1.7 beta\n%%EOF")
            coverage.mkdir(parents=True)
            (coverage / "review_queue.json").write_text(
                json.dumps(
                    [
                        {
                            "prospectus_id": "alpha_final_offering_circular",
                            "source_path": str(alpha),
                            "source_filename": alpha.name,
                            "status": "needs_extraction_backend",
                            "contract_path": None,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            fixture = fixtures / "beta_final_offering_circular_pages_seed.json"
            fixture.write_text(json.dumps(GENERIC_PAGES), encoding="utf-8")

            report = auto_ingest_prospectuses(
                prospectus_dir=raw,
                contracts_dir=contracts,
                reviews_dir=reviews,
                coverage_dir=coverage,
                fixture_dir=fixtures,
                source_paths=[beta],
            )

            self.assertEqual(report.scanned, 1)
            self.assertEqual(report.created_contracts, 1)
            queue = json.loads((coverage / "review_queue.json").read_text(encoding="utf-8"))
            self.assertEqual({item["source_filename"] for item in queue}, {alpha.name, beta.name})
            beta_item = next(item for item in queue if item["source_filename"] == beta.name)
            self.assertEqual(beta_item["status"], "contract_available_needs_review")

    def test_approve_reviewed_contract_can_delete_raw_only_after_reviewed_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            raw = root / "data/raw/prospectuses/raw.pdf"
            raw.parent.mkdir(parents=True)
            raw.write_bytes(b"%PDF-1.7\n%%EOF\n")
            contract_path = root / "data/contracts/contract.json"
            contract_path.parent.mkdir(parents=True)
            contract = {
                "id": "example_issuer_ltd_2030_cb",
                "status": "needs_review",
                "source_file": str(raw),
                "source_review": {"raw_prospectus_sha256": sha256_file(raw)},
                "issuer": {"name": "Example Issuer Ltd."},
                "bond": {"maturity_date": "2030-05-15"},
                "conversion": {"underlying_ticker": "1234 HK"},
            }
            contract_path.write_text(json.dumps(contract), encoding="utf-8")
            with self.assertRaises(ValueError):
                approve_reviewed_contract(contract_path, delete_raw=True)
            contract["status"] = "reviewed"
            contract_path.write_text(json.dumps(contract), encoding="utf-8")
            result = approve_reviewed_contract(contract_path, delete_raw=True)
            self.assertTrue(result["raw_deleted"])
            self.assertFalse(raw.exists())

    def test_approve_reviewed_contract_resolves_project_relative_raw_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            raw = root / "data/raw/prospectuses/raw.pdf"
            raw.parent.mkdir(parents=True)
            raw.write_bytes(b"%PDF-1.7\n%%EOF\n")
            contract_path = root / "data/contracts/contract.json"
            contract_path.parent.mkdir(parents=True)
            contract_path.write_text(
                json.dumps(
                    {
                        "id": "example_issuer_ltd_2030_cb",
                        "status": "reviewed",
                        "source_file": "data/raw/prospectuses/raw.pdf",
                        "source_review": {"raw_prospectus_sha256": sha256_file(raw)},
                    }
                ),
                encoding="utf-8",
            )

            result = approve_reviewed_contract(contract_path, delete_raw=True)

            self.assertTrue(result["raw_deleted"])
            self.assertFalse(raw.exists())

    def test_approve_reviewed_contract_refuses_existing_source_outside_raw_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            outside = root / "outside.pdf"
            outside.write_bytes(b"%PDF-1.7\n%%EOF\n")
            contract_path = root / "data/contracts/contract.json"
            contract_path.parent.mkdir(parents=True)
            contract_path.write_text(
                json.dumps(
                    {
                        "id": "bad_contract",
                        "status": "reviewed",
                        "source_file": str(outside),
                        "source_review": {"raw_prospectus_sha256": sha256_file(outside)},
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "escapes raw root"):
                approve_reviewed_contract(contract_path, delete_raw=True)

            self.assertTrue(outside.exists())

    def test_auto_ingest_cli_writes_queue_and_supports_delete_reviewed_raw(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            raw = root / "data/raw/prospectuses"
            fixtures = root / "data/prospectus_text"
            raw.mkdir(parents=True)
            fixtures.mkdir(parents=True)
            (raw / "Example Issuer - Final Offering Circular.pdf").write_bytes(b"%PDF-1.7\n%%EOF\n")
            (fixtures / "example_issuer_final_offering_circular_pages_seed.json").write_text(json.dumps(GENERIC_PAGES), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "cb_terminal.cli.prospectus_auto_ingest",
                    "--project-root",
                    str(root),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn("created_contracts=1", result.stdout)
            self.assertTrue((root / "data/coverage/review_queue.json").exists())

    def test_contract_instrument_key_uses_issuer_underlying_and_maturity(self):
        raw = {
            "issuer": {"name": "Example Issuer Ltd."},
            "conversion": {"underlying_ticker": "1234 HK"},
            "bond": {"maturity_date": "2030-05-15"},
        }
        self.assertEqual(contract_instrument_key(raw), "example issuer ltd.|1234 hk|2030-05-15")


if __name__ == "__main__":
    unittest.main()
