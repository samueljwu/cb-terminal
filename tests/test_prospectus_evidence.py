import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from cb_terminal.prospectus.evidence import (
    OPTIONAL_EVIDENCE_FIELDS,
    REQUIRED_EVIDENCE_FIELDS,
    attach_source_evidence,
    build_term_evidence,
    evidence_summary,
)
from cb_terminal.prospectus.extraction import ExtractionResult, PageText
from cb_terminal.prospectus.draft_contract import draft_contract_from_text
from cb_terminal.prospectus.review import build_review_report, validate_contract_dict

ROOT = Path(__file__).resolve().parents[1]
ISSUER_PDF = ROOT / "data/raw/prospectuses/Issuer - Final Offering Circular (CB) - March 25, 2026.pdf"


SAMPLE_PAGE_1 = """
Issuer Corporation
US$2,000,000,000 Zero Coupon Convertible Bonds due 2031
The Bonds are issued in denominations of US$200,000 each.
The issue price is 100 per cent. of principal amount.
The pricing date is 25 March 2026. The closing date is 1 April 2026. The Bonds will mature on 1 April 2031.
At maturity, the Bonds will be redeemed at 100 per cent. of principal amount.
The Shares are listed on the Taiwan Stock Exchange under stock code 6669.
ISIN: XS3236970433. Common Code: 323697043.
"""

SAMPLE_PAGE_2 = """
The initial Conversion Price is NT$4,286.40 per Share.
The fixed exchange rate is NT$31.951 = US$1.00.
The conversion period shall commence on 12 May 2026 and end on 22 March 2031.
The Company may redeem the Bonds on or after 1 April 2029 if the closing price
of the Shares for 20 out of 30 consecutive Trading Days is at least 130 per cent.
of the Conversion Price then in effect.
"""


def sample_extraction() -> ExtractionResult:
    return ExtractionResult.from_pages(
        source_path=Path("issuer.pdf"),
        pages=[PageText(1, SAMPLE_PAGE_1), PageText(2, SAMPLE_PAGE_2)],
        method="unit-test-pages",
    )


class ProspectusEvidenceTests(unittest.TestCase):
    def test_extraction_result_preserves_page_level_text(self):
        extraction = sample_extraction()
        self.assertEqual(extraction.page_count, 2)
        self.assertEqual([page.page_number for page in extraction.pages], [1, 2])
        self.assertIn("--- Page 2 ---", extraction.text)
        self.assertIn("Conversion Price", extraction.text)

    def test_build_term_evidence_finds_page_snippets_for_core_issuer_terms(self):
        contract = draft_contract_from_text(SAMPLE_PAGE_1 + SAMPLE_PAGE_2, source_file="issuer.pdf")
        evidence = build_term_evidence(contract, sample_extraction())
        for field in [
            "issuer.name",
            "bond.issue_size",
            "bond.denomination",
            "bond.maturity_date",
            "conversion.initial_conversion_price",
            "conversion.fixed_exchange_rate",
            "conversion.start_date",
            "conversion.end_date",
            "calls[0].trigger_ratio",
        ]:
            self.assertIn(field, evidence, field)
            self.assertGreaterEqual(evidence[field][0]["confidence"], 0.7, evidence[field])
            self.assertIn("page", evidence[field][0])
            self.assertIn("snippet", evidence[field][0])
        self.assertEqual(evidence["conversion.initial_conversion_price"][0]["page"], 2)
        self.assertIn("NT$4,286.40", evidence["conversion.initial_conversion_price"][0]["snippet"])

    def test_optional_issuance_economics_receive_evidence_without_expanding_required_gate(self):
        page_1 = SAMPLE_PAGE_1 + """
Brokerage:
0.50% of the aggregate allocated amount, payable by investors.
Yield to Maturity:
2.75% per annum, calculated on a semi-annual basis.
Bondholder Put Date:
1 April 2029
Put Price:
101.00% of principal amount.
Yield to Put:
1.50% per annum, calculated annually.
"""
        extraction = ExtractionResult.from_pages(
            source_path=Path("issuer.pdf"),
            pages=[PageText(1, page_1), PageText(2, SAMPLE_PAGE_2)],
            method="unit-test-pages",
        )
        contract = draft_contract_from_text(page_1 + SAMPLE_PAGE_2, source_file="issuer.pdf")
        enriched = attach_source_evidence(contract, extraction)
        evidence = enriched["source_review"]["term_evidence"]

        self.assertIn("bond.brokerage", evidence)
        self.assertIn("redemption.yield_to_maturity", evidence)
        self.assertIn("puts[0].yield_to_put", evidence)
        self.assertTrue(set(OPTIONAL_EVIDENCE_FIELDS).isdisjoint(REQUIRED_EVIDENCE_FIELDS))
        summary = evidence_summary(enriched)
        self.assertEqual(summary["optional_missing_fields"], [])
        self.assertEqual(summary["missing_required_fields"], [])

    def test_attach_source_evidence_records_gaps_and_keeps_needs_review_until_human_approval(self):
        contract = draft_contract_from_text(SAMPLE_PAGE_1 + SAMPLE_PAGE_2, source_file="issuer.pdf")
        enriched = attach_source_evidence(contract, sample_extraction())
        source_review = enriched["source_review"]
        self.assertEqual(source_review["review_status"], "evidence_collected_needs_human_review")
        self.assertEqual(enriched["status"], "needs_review")
        self.assertIn("term_evidence", source_review)
        summary = evidence_summary(enriched)
        self.assertGreaterEqual(summary["covered_required_fields"], 8)
        self.assertEqual(summary["missing_required_fields"], [])
        self.assertEqual(set(REQUIRED_EVIDENCE_FIELDS) - set(source_review["term_evidence"]), set())

    def test_review_validation_warns_when_required_source_evidence_is_missing(self):
        contract = draft_contract_from_text(SAMPLE_PAGE_1, source_file="issuer.pdf")
        issues = validate_contract_dict(contract)
        evidence_issues = [issue for issue in issues if issue.field == "source_review.term_evidence"]
        self.assertTrue(evidence_issues)
        self.assertEqual(evidence_issues[0].severity, "warning")

    def test_approval_evidence_gate_ignores_non_valuation_metadata_and_absent_optional_dates(self):
        contract = draft_contract_from_text(SAMPLE_PAGE_1 + SAMPLE_PAGE_2, source_file="issuer.pdf")
        enriched = attach_source_evidence(contract, sample_extraction())
        evidence = enriched["source_review"]["term_evidence"]
        for non_pricing_field in ["bond.description", "bond.closing_date", "conversion.start_date", "conversion.end_date"]:
            evidence.pop(non_pricing_field, None)
        summary = evidence_summary(enriched)
        self.assertEqual(summary["approval_missing_fields"], [])
        self.assertTrue(set(["bond.description", "bond.closing_date", "conversion.start_date", "conversion.end_date"]).issubset(summary["missing_required_fields"]))

    def test_generic_termsheet_extracts_soft_call_terms_and_evidence(self):
        text = """
        SUMMARY TERMS & CONDITIONS - May 14, 2026
        Issuer WuXi AppTec Co., Ltd. Stock Code: 2359 HK.
        Securities Offered Renminbi denominated, CNH linked, United States Dollar settled Zero Coupon Convertible Bonds due 2027.
        Deal Size RMB 6,780 million. Denomination RMB 2,000,000 per Bond.
        Maturity Date On or about May 22, 2027. Issue Price 103.5% of the principal amount.
        Initial Conversion Price HKD 153.00 per H Share.
        Fixed Exchange Rate CNH 0.8664 = HKD 1.00. Redemption Price at Maturity 100.00%.
        Conversion Period Convertible at any time after Closing Date until 10 working days prior to Maturity Date.
        Issuer Call Yes, all but not some only of the Bonds, at the U.S. Dollar Equivalent of the principal amount,
        at any time after June 21, 2026 but prior to the Maturity Date, provided that no such redemption may be made unless
        the Closing Price of an H Share for any 15 H Share Stock Exchange Business Days within a period of 30 consecutive
        H Share Stock Exchange Business Days was, for each such 15 H Share Stock Exchange Business Days, at least 120 per cent.
        of the Conversion Price then applicable.
        """
        contract = draft_contract_from_text(text, source_file="wuxi_termsheet.pdf")
        self.assertEqual(contract["calls"][0]["model_type"], "soft_call")
        self.assertEqual(contract["calls"][0]["start_date"], "2026-06-22")
        self.assertAlmostEqual(contract["calls"][0]["trigger_ratio"], 1.2)
        enriched = attach_source_evidence(contract, ExtractionResult.from_pages(source_path="wuxi_termsheet.pdf", pages=[PageText(1, text)], method="unit-test-pages"))
        self.assertIn("calls[0].trigger_ratio", enriched["source_review"]["term_evidence"])
        self.assertNotIn("calls[0].trigger_ratio", evidence_summary(enriched)["approval_missing_fields"])

    def test_review_report_includes_source_evidence_matrix_and_gaps(self):
        contract = draft_contract_from_text(SAMPLE_PAGE_1 + SAMPLE_PAGE_2, source_file="issuer.pdf")
        enriched = attach_source_evidence(contract, sample_extraction())
        report = build_review_report(enriched, sample_extraction())
        self.assertIn("## Source evidence matrix", report)
        self.assertIn("conversion.initial_conversion_price", report)
        self.assertIn("page 2", report)
        self.assertIn("## Evidence gaps", report)
        self.assertIn("No required evidence gaps detected", report)

    def test_cli_can_write_page_evidence_json_when_text_fixture_is_supplied(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture = Path(tmpdir) / "pages.json"
            fixture.write_text(
                json.dumps(
                    {
                        "pages": [
                            {"page": 1, "text": SAMPLE_PAGE_1},
                            {"page": 2, "text": SAMPLE_PAGE_2},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            out_contract = Path(tmpdir) / "issuer.json"
            out_review = Path(tmpdir) / "review.md"
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "cb_terminal.cli.prospectus_extract",
                    "--pdf",
                    str(ISSUER_PDF),
                    "--output",
                    str(out_contract),
                    "--review-output",
                    str(out_review),
                    "--text-fixture",
                    str(fixture),
                    "--require-evidence",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            data = json.loads(out_contract.read_text(encoding="utf-8"))
            self.assertIn("term_evidence", data["source_review"])
            self.assertIn("source_evidence_covered=", result.stdout)
            self.assertIn("Source evidence matrix", out_review.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
