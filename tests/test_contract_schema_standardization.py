import unittest

from cb_terminal.domain.identity import contract_display_id
from cb_terminal.io.instrument_registry import cb_display_name, derive_issuer_short_name, exchangeable_display_prefix
from cb_terminal.prospectus.draft_contract import draft_contract_from_text
from cb_terminal.prospectus.schema import required_term_keys
from cb_terminal.web.server import _instrument_display_parts


class ContractSchemaStandardizationTests(unittest.TestCase):
    def test_cb_display_name_uses_issuer_coupon_year(self):
        self.assertEqual(cb_display_name("Issuer", "2031-04-01", 0.0), "Issuer 0 31")
        self.assertEqual(cb_display_name("Issuer", "2029-01-01", 0.0125), "Issuer 1.25 29")
        self.assertEqual(cb_display_name("Issuer", "2029-01-01", 0.5, coupon_in_percent_points=True), "Issuer 0.5 29")
        self.assertEqual(cb_display_name("Issuer", "2029-01-01", None), "Issuer ? 29")

    def test_short_issuer_name_uses_supported_acronym(self):
        self.assertEqual(derive_issuer_short_name("CIG SHANGHAI CO., LTD."), "CIG")
        self.assertEqual(derive_issuer_short_name("United Microelectronics Corporation", source_file="UMC - Pricing Termsheet.pdf"), "UMC")
        self.assertEqual(derive_issuer_short_name("Example Issuer Limited", source_file="example.pdf"), "Example Issuer")
        self.assertEqual(derive_issuer_short_name("Baidu, Inc."), "Baidu")

    def test_exchangeable_prefix_uses_underlying_short_name(self):
        self.assertEqual(exchangeable_display_prefix("Baidu", "Trip.com Group Limited"), "Baidu/Trip")
        self.assertEqual(exchangeable_display_prefix("Baidu", None, underlying_ticker="9961 HK"), "Baidu/9961")
        raw = {
            "instrument": {"issuer_short_name": "Baidu", "structure_type": "exchangeable_bond",
                           "display_name": "Baidu/Trip 0 30", "maturity_year": 2030},
            "exchangeable_terms": {"reference_company_name": "Trip.com Group Limited"},
            "bond": {"coupon_rate": 0, "maturity_date": "2030-01-01"},
        }
        self.assertEqual(contract_display_id(raw), "Baidu/Trip 0 30")
        self.assertEqual(_instrument_display_parts(raw)["instrument_short_name"], "Baidu/Trip 0 30")

    def test_contract_display_id_uses_coupon_percentage_points(self):
        raw = {"instrument": {"issuer_short_name": "CIG", "maturity_year": 2027},
               "bond": {"coupon_rate": 0.5, "maturity_date": "2027-10-12"}}
        self.assertEqual(contract_display_id(raw), "CIG 0.5 27")
        self.assertEqual(_instrument_display_parts(raw)["instrument_short_name"], "CIG 0.5 27")

    def test_prospectus_drafts_carry_required_term_checklist(self):
        draft = draft_contract_from_text(
            """
            Example Issuer Limited
            US$100,000,000 Zero Coupon Convertible Bonds due 2031
            The Bonds will mature on 1 April 2031 and will be redeemed at 100 per cent.
            """,
            source_file="example.pdf",
        )
        keys = draft["source_review"]["required_term_keys"]

        self.assertEqual(keys, required_term_keys())
        self.assertIn("bond.maturity_date", keys)
        self.assertIn("conversion.initial_conversion_price", keys)
        self.assertIn("quote_convention", keys)
        self.assertEqual(draft["instrument"]["display_name"], "Example Issuer 0 31")


if __name__ == "__main__":
    unittest.main()
