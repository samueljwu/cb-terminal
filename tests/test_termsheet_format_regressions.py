import copy
import unittest
from datetime import date

from cb_terminal.domain import Assumptions, MarketSnapshot
from cb_terminal.io.contract_loader import contract_from_dict
from cb_terminal.pricing import PricingEngine
from cb_terminal.prospectus.draft_contract import (
    _extract_conversion_end_date,
    _extract_soft_calls,
    draft_contracts_from_text,
)
from cb_terminal.prospectus.review import validate_contract_dict


MULTI_SERIES_TABLE = """
--- Page 1 ---
ALPHA MEMORY CORPORATION (8299 TT)
US$400 Million Currency-Linked Zero Coupon Convertible Bonds due 2031 (“Series A Bonds”)
US$400 Million Zero Coupon Convertible Bonds due 2031 (“Series B Bonds”)
Summary: Indicative Terms and Conditions
Issuer:
Alpha Memory Corporation, listed under the trading code “8299” on the Taipei Exchange (the “Issuer”)
Denomination:
US$200,000 per Bond and integral multiples of US$100,000 in excess thereof
Series A Bonds
Series B Bonds
Maturity Date:
On or about May 26, 2031 (5 years from the Closing Date)
Bondholder Put Date:
On or about May 26, 2028
On or about May 28, 2029
Issue Size:
US$400 million
US$400 million
Issue Price:
100.00% of the principal amount
100.00% of the principal amount
Put Price:
Settlement Equivalent of 99.50% of the principal amount
99.25% of the principal amount
Redemption Price at Maturity:
Settlement Equivalent of 98.76% of the principal amount
98.76% of the principal amount
Initial Conversion Premium:
25.0% over the Reference Share Price
30.0% over the Reference Share Price
Reference Share Price:
NT$2,735, the closing price of the Shares on the Taipei Exchange on May 18, 2026
Initial Conversion Price:
NT$3,418.75 per Share for the Series A Bonds
NT$3,555.50 per Share for the Series B Bonds

--- Page 2 ---
Fixed Exchange Rate:
NT$31.6350 / US$1.000
Conversion Period:
Convertible at any time on or after the next day immediately after the end of a three-month period following the Closing Date up to and including the 10th day prior to the Maturity Date.
Redemption at the Option of the Issuer for the Series A Bonds:
Issuer Call – callable after 2 years from the Closing Date at the applicable Early Redemption Amount if the closing price of the Shares for a period of 20 out of 30 consecutive trading days is at least 130% of the Conversion Price.
Clean Up Call – callable below 10% outstanding.
Redemption at the Option of the Issuer for the Series B Bonds:
Issuer Call – callable after 3 years from the Closing Date at the applicable Early Redemption Amount if the closing price of the Shares for a period of 20 out of 30 consecutive trading days is at least 130% of the Conversion Price.
Clean Up Call – callable below 10% outstanding.

--- Page 3 ---
Pricing / Trade Date:
On or about May 18, 2026
Closing / Settlement Date:
On or about May 26, 2026
Security Codes
Series A: ISIN: XS1111111111 Common Code: 111111111
Series B: ISIN: XS2222222222 Common Code: 222222222
"""


EXCHANGEABLE_TERM_SHEET = """
--- Page 1 ---
BETA PACIFIC FINANCE LIMITED
HK$4.7 billion Zero Coupon Exchangeable Bonds due 2027 Exchangeable into Ordinary Shares of Beta Airways (Ticker: 293 HK)
Issuer:
Beta Pacific Finance Limited
Guarantor:
Beta Pacific Limited
Offering:
HKD denominated Exchangeable Bonds referencing ordinary shares of Beta Airways Limited ("Beta Airways", "Company") with stock code: 293 HK
Currency:
Hong Kong Dollars (HKD)
Denomination:
HK$2,000,000 per Bond and integral multiples of HK$1,000,000 in excess thereof
Issue Size:
HK$4.7bn
Maturity Date:
On or about June 16, 2027
Issue Price:
100.125% of the principal amount
Coupon:
Zero
Redemption Price:
100.00% of the principal amount
Initial Exchange Premium:
10.0% over the Reference Share Price
Reference Share Price:
HK$11.98 per Share
Initial Exchange Price:
HK$13.18 per Share
Exchange Property:
The Exchange Property will initially comprise around 356,600,910 Shares.
Initial Exchange Ratio:
75,872.5341 Shares per HK$1,000,000 principal amount of any Bond
Exchange Period:
At any time on or after the 41st day following the Issue Date to the earliest of (i) where the Share Redemption Option is not exercised, the 30th Trading Day immediately preceding the Maturity Date, or (ii) where the Share Redemption Option is exercised, 10 days immediately preceding the Maturity Date.
Cash Election:
The Issuer may elect to pay the Cash Alternative Amount. Cash Averaging Period means a period of 20 consecutive Trading Days.
Issuer Call:
Callable at any time after 3 months from Settlement Date if the Closing Price for any 20 out of 30 consecutive Trading Days, the last of such Trading Days shall occur not more than 10 days prior to notice, was at least 120% of the Exchange Price.
Pricing / Trade Date:
June 9, 2026
Closing / Settlement Date:
June 16, 2026
Security Codes:
ISIN: XS3333333333 Common Code: 333333333
"""


DISJOINT_CONVERSION_TERM_SHEET = """
--- Page 1 ---
OMEGA INNOVATIONS LIMITED (100 HK)
HK$6.5 billion Zero Coupon Convertible Bonds due 2027
Issuer:
Omega Innovations Limited
Guarantor:
Omega Group Inc.
Currency:
Hong Kong Dollars
Denomination:
HK$2,000,000 and integral multiples of HK$1,000,000 in excess thereof
Offer Size:
HK$6,500 million
Maturity Date:
July 14, 2027
Issue Price:
100.00% of the principal amount
Coupon:
Zero
Redemption Price at Maturity:
102.75% of the principal amount
Initial Conversion Premium:
25.0% over the Reference Share Price
Reference Share Price:
HK$268.00 per Share
Initial Conversion Price:
HK$335.00 per Share
Conversion Period:
Convertible at any time during (1) First Conversion Period: after the Closing Date until September 21, 2026; and (2) Second Conversion Period: after May 15, 2027 up to the date falling 10 days prior to the Maturity Date.
Issuer Call:
Callable after 20 Trading Days following the Closing Date at the Early Redemption Amount if the closing price of the Shares for any 10 out of 20 consecutive Trading Days, the last of which occurs not more than 10 days prior to notice, was at least 120% of the applicable Early Redemption Amount for each HK$1,000,000 divided by the conversion ratio.
Pricing / Trade Date:
July 10, 2026
Closing Date:
July 16, 2026
Security Codes:
ISIN: XS4444444444 Common Code: 444444444
"""


LONG_DATED_CONDITIONAL_TERM_SHEET = """
--- Page 1 ---
GAMMA GROUP LIMITED (992 HK)
US$2 billion Zero Coupon Convertible Bonds due 2033
Issuer:
Gamma Group Limited
Currency:
United States Dollars
Denomination:
US$200,000 and integral multiples of US$1,000 in excess thereof
Offer Size:
US$2,000 million
Maturity Date:
June 25, 2033
Issue Price:
100.00% of the principal amount
Coupon:
Zero
Redemption Price at Maturity:
100.00% of the principal amount
Reference Share Price:
HK$24.88
Conversion Premium:
47.5%
Initial Conversion Price:
HK$36.70 per Share
Fixed Exchange Rate:
HK$7.8332 / US$1.00
Conversion Period:
Convertible (A) at any time after the 6th anniversary of the Settlement Date up to the 10th day prior to the Maturity Date; and (B) at any time after 40 days after the Settlement Date if a notice is given pursuant to a tax call or clean-up call, or a Relevant Event occurs.
Pricing / Trade Date:
June 17, 2026
Settlement Date:
June 25, 2026
Security Codes:
ISIN: XS5555555555 Common Code: 555555555
Existing Bonds to be Repurchased: US$675 million 2.50% Convertible Bonds due 2029 (ISIN: XS9999999999).
Accrued Interest runs from and including February 26, 2026 to the settlement date.
"""


class TermsheetFormatRegressionTests(unittest.TestCase):
    def test_two_column_multi_series_table_produces_distinct_contracts(self):
        drafts = draft_contracts_from_text(MULTI_SERIES_TABLE, source_file="multi-series.pdf")

        self.assertEqual(len(drafts), 2)
        by_series = {draft["instrument"]["series_label"]: draft for draft in drafts}
        series_a = by_series["Series A"]
        series_b = by_series["Series B"]

        self.assertEqual(series_a["instrument"]["canonical_id"], "XS1111111111")
        self.assertEqual(series_b["instrument"]["canonical_id"], "XS2222222222")
        self.assertEqual(series_a["conversion"]["initial_conversion_price"], 3418.75)
        self.assertEqual(series_b["conversion"]["initial_conversion_price"], 3555.50)
        self.assertEqual(series_a["bond"]["economic_currency"], "TWD")
        self.assertEqual(series_b["bond"]["economic_currency"], "USD")
        self.assertEqual(series_a["bond"]["denomination_increment"], 100000.0)
        self.assertEqual(series_a["conversion"]["fixed_exchange_rate"], 31.635)
        self.assertEqual(series_a["conversion"]["start_date"], "2026-08-27")
        self.assertEqual(series_a["conversion"]["start_date_rule"], "day_after_3_calendar_months_following_closing")
        self.assertEqual(series_a["conversion"]["end_date_rule"], "10_calendar_days_before_maturity")
        self.assertEqual(series_a["puts"][0]["price"], 99.5)
        self.assertEqual(series_b["puts"][0]["price"], 99.25)
        self.assertEqual(series_a["calls"][0]["start_date"], "2028-05-27")
        self.assertEqual(series_b["calls"][0]["start_date"], "2029-05-27")
        self.assertEqual(series_a["calls"][0]["trigger_days"], 20)
        self.assertEqual(series_a["calls"][0]["trigger_window_days"], 30)

    def test_validation_rejects_investor_economic_inconsistencies(self):
        draft = draft_contracts_from_text(MULTI_SERIES_TABLE)[0]
        broken = copy.deepcopy(draft)
        broken["bond"]["pricing_date"] = "2026-06-01"
        broken["conversion"]["initial_conversion_price"] = 4000.0

        issues = validate_contract_dict(broken)
        error_fields = {issue.field for issue in issues if issue.severity == "error"}
        self.assertIn("bond.pricing_date", error_fields)
        self.assertIn("conversion.initial_conversion_price", error_fields)

    def test_loader_refuses_missing_pricing_date_instead_of_using_today(self):
        draft = draft_contracts_from_text(MULTI_SERIES_TABLE)[0]
        draft["bond"]["pricing_date"] = None

        with self.assertRaisesRegex(ValueError, "pricing_date is required"):
            contract_from_dict(draft)

    def test_loader_refuses_missing_coupon_instead_of_silently_using_zero(self):
        draft = draft_contracts_from_text(MULTI_SERIES_TABLE)[0]
        draft["bond"]["coupon_rate"] = None

        with self.assertRaisesRegex(ValueError, "coupon_rate is required"):
            contract_from_dict(draft)

    def test_positive_coupon_requires_a_payment_frequency(self):
        draft = draft_contracts_from_text(MULTI_SERIES_TABLE)[0]
        draft["bond"]["coupon_rate"] = 5.0
        draft["bond"]["coupon_frequency"] = 0

        errors = [issue for issue in validate_contract_dict(draft) if issue.severity == "error"]
        self.assertTrue(any(issue.field == "bond.coupon_frequency" for issue in errors))
        with self.assertRaisesRegex(ValueError, "positive.*coupon_frequency"):
            contract_from_dict(draft)

    def test_conversion_windows_must_stay_inside_bond_and_outer_conversion_dates(self):
        draft = draft_contracts_from_text(DISJOINT_CONVERSION_TERM_SHEET)[0]
        draft["conversion"]["windows"][0]["start_date"] = "2025-07-17"
        draft["conversion"]["windows"][1]["end_date"] = "2028-07-04"

        error_fields = {issue.field for issue in validate_contract_dict(draft) if issue.severity == "error"}

        self.assertIn("conversion.windows.0.start_date", error_fields)
        self.assertIn("conversion.windows.1.end_date", error_fields)
        with self.assertRaisesRegex(ValueError, "conversion.windows"):
            contract_from_dict(draft)

    def test_conversion_end_search_is_scoped_to_conversion_period(self):
        text = """
        Subscription Period: from 1 July 2026 to and including 1 August 2026.
        Conversion Period: from and including 1 September 2026 to and including 1 September 2030.
        Redemption at the Option of the Issuer: none.
        """

        self.assertEqual(_extract_conversion_end_date(text), "2030-09-01")

    def test_conversion_end_accepts_offering_circular_convertible_prose(self):
        text = (
            "The Bonds will be convertible into common shares during the period "
            "from and including July 2, 2026 to and including March 22, 2031 "
            "(subject to certain restrictions)."
        )

        self.assertEqual(_extract_conversion_end_date(text), "2031-03-22")

    def test_trading_day_relative_call_start_never_resolves_to_weekend(self):
        calls = _extract_soft_calls(
            "Issuer Call: callable after 4 Trading Days following Closing Date if the Closing Price is at least 120% of the Conversion Price. Clean Up Call: none.",
            closing_date="2026-07-13",
        )

        self.assertEqual(calls[0]["start_date"], "2026-07-20")

    def test_loader_preserves_soft_call_observation_window(self):
        draft = draft_contracts_from_text(MULTI_SERIES_TABLE)[0]
        contract = contract_from_dict(draft)

        self.assertEqual(contract.calls[0].trigger_days, 20)
        self.assertEqual(contract.calls[0].trigger_window_days, 30)
        self.assertEqual(contract.calls[0].observation_rule, "20_of_30_consecutive_trading_days")
        self.assertEqual(contract.calls[0].price_rule, "early_redemption_amount")
        self.assertEqual(contract.metadata["term_extensions"]["conversion_end_date_rule"], "10_calendar_days_before_maturity")

    def test_consecutive_day_call_keeps_notice_lookback_distinct_from_observation_window(self):
        text = MULTI_SERIES_TABLE.replace(
            "for a period of 20 out of 30 consecutive trading days is",
            "for each of the 20 consecutive Trading Days, the last of which occurs not more than 30 days prior to the date of notice, is",
        )
        draft = draft_contracts_from_text(text)[0]
        call = draft["calls"][0]

        self.assertEqual(call["trigger_days"], 20)
        self.assertEqual(call["trigger_window_days"], 20)
        self.assertEqual(call["observation_rule"], "20_consecutive_trading_days")
        self.assertEqual(call["last_observation_max_days_before_notice"], 30)

        loaded = contract_from_dict(draft)
        self.assertEqual(loaded.calls[0].last_observation_max_days_before_notice, 30)

    def test_currency_linked_contract_is_explicitly_flagged_in_review_and_pricing(self):
        draft = next(
            item
            for item in draft_contracts_from_text(MULTI_SERIES_TABLE)
            if item["bond"]["economic_currency"] != item["bond"]["currency"]
        )

        review_warnings = [issue.message for issue in validate_contract_dict(draft) if issue.severity == "warning"]
        self.assertTrue(any("economically linked" in message for message in review_warnings))

        contract = contract_from_dict(draft)
        result = PricingEngine().price(
            contract,
            MarketSnapshot(stock_price=2735.0),
            Assumptions(
                volatility=0.30,
                risk_free_rate=0.03,
                credit_spread=0.02,
                steps=30,
                valuation_date=date(2026, 5, 18),
            ),
        )
        self.assertTrue(any("economically linked to TWD" in warning for warning in result.diagnostics.warnings))

    def test_exchangeable_bond_keeps_reference_company_property_and_elections(self):
        draft = draft_contracts_from_text(EXCHANGEABLE_TERM_SHEET)[0]

        self.assertEqual(draft["instrument"]["structure_type"], "exchangeable_bond")
        self.assertEqual(draft["instrument"]["canonical_id"], "XS3333333333")
        self.assertEqual(draft["guarantor"]["name"], "Beta Pacific Limited")
        self.assertEqual(draft["conversion"]["start_date"], "2026-07-27")
        self.assertIn("conditional_30_trading_days", draft["conversion"]["end_date_rule"])
        self.assertEqual(draft["calls"][0]["start_date"], "2026-09-17")
        self.assertEqual(draft["calls"][0]["last_observation_max_days_before_notice"], 10)
        terms = draft["exchangeable_terms"]
        self.assertEqual(terms["reference_company_name"], "Beta Airways Limited")
        self.assertEqual(terms["initial_exchange_property_shares"], 356600910.0)
        self.assertEqual(terms["initial_exchange_ratio"], 75872.5341)
        self.assertTrue(terms["issuer_cash_election"])
        self.assertTrue(terms["share_redemption_option"])

    def test_disjoint_conversion_periods_survive_loading_and_dynamic_call_terms(self):
        draft = draft_contracts_from_text(DISJOINT_CONVERSION_TERM_SHEET)[0]

        self.assertEqual(draft["bond"]["coupon_rate"], 0.0)
        self.assertEqual(
            [(item["start_date"], item["end_date"]) for item in draft["conversion"]["windows"]],
            [("2026-07-17", "2026-09-21"), ("2027-05-16", "2027-07-04")],
        )
        call = draft["calls"][0]
        self.assertEqual(call["trigger_days"], 10)
        self.assertEqual(call["trigger_window_days"], 20)
        self.assertEqual(call["trigger_basis"], "early_redemption_amount_divided_by_conversion_ratio")
        self.assertEqual(call["price_rule"], "early_redemption_amount")

        loaded = contract_from_dict(draft)
        self.assertEqual(
            loaded.conversion.windows,
            (
                (date(2026, 7, 17), date(2026, 9, 21)),
                (date(2027, 5, 16), date(2027, 7, 4)),
            ),
        )

    def test_new_security_code_and_long_dated_conversion_beat_old_bond_contamination(self):
        draft = draft_contracts_from_text(LONG_DATED_CONDITIONAL_TERM_SHEET)[0]

        self.assertEqual(draft["instrument"]["canonical_id"], "XS5555555555")
        self.assertEqual(draft["bond"]["closing_date"], "2026-06-25")
        self.assertEqual(draft["conversion"]["start_date"], "2032-06-26")
        self.assertEqual(draft["conversion"]["conditional_early_start_date"], "2026-08-05")
        self.assertEqual(
            draft["conversion"]["conditional_early_conditions"],
            ["tax_call_notice", "cleanup_call_notice", "relevant_event"],
        )


if __name__ == "__main__":
    unittest.main()
