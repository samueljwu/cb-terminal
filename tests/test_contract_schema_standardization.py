import unittest

from cb_terminal.io.instrument_registry import cb_display_name
from cb_terminal.prospectus.draft_contract import draft_contract_from_text
from cb_terminal.prospectus.schema import required_term_keys


class ContractSchemaStandardizationTests(unittest.TestCase):
    def test_cb_display_name_uses_issuer_coupon_year(self):
        self.assertEqual(cb_display_name("Issuer", "2031-04-01", 0.0), "Issuer 0 31")
        self.assertEqual(cb_display_name("Issuer", "2029-01-01", 0.0125), "Issuer 1.25 29")

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
