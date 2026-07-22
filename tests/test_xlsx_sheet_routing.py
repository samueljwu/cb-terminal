import unittest

from cb_terminal.io.market_data_history import _parse_market_data_xlsx_tables
from cb_terminal.io.price_history import _parse_price_history_xlsx_tables


class XlsxSheetRoutingTests(unittest.TestCase):
    def test_quote_workbook_ignores_readme_sheet(self):
        tables = [
            ("README", [["Export notes"], ["Do not delete this sheet"]]),
            (
                "CB Quotes",
                [
                    ["Reference Security", "Date", "ISIN", "Bid Price", "Ask Price"],
                    ["TEST Corp", "05/22/26", "XS0000000001", "101", "102"],
                ],
            ),
        ]

        rows = _parse_price_history_xlsx_tables(tables, source_file="mixed.xlsx")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].instrument_id, "XS0000000001")
        self.assertEqual(rows[0].source_sheet, "CB Quotes")

    def test_market_workbook_ignores_readme_sheet(self):
        tables = [
            ("README", [["Export notes"], ["Daily closes follow"]]),
            (
                "Market Data",
                [
                    ["Bloomberg export"],
                    [],
                    [],
                    ["", "1234 HK Equity", "USDHKD Curncy"],
                    ["", "Last Price", "Last Price"],
                    ["Date", "PX_LAST", "PX_LAST"],
                    ["2026-05-22", "50", "7.85"],
                ],
            ),
        ]

        points = _parse_market_data_xlsx_tables(tables, source_file="mixed.xlsx")

        self.assertEqual(len(points), 2)
        self.assertEqual({point.instrument_type for point in points}, {"equity", "fx"})
        self.assertTrue(all(point.source_sheet == "Market Data" for point in points))

    def test_unrecognized_workbook_still_fails_closed(self):
        tables = [("README", [["Export notes"], ["No market data here"]])]

        with self.assertRaisesRegex(ValueError, "no XLSX worksheet"):
            _parse_price_history_xlsx_tables(tables, source_file="notes.xlsx")
        with self.assertRaisesRegex(ValueError, "no XLSX worksheet"):
            _parse_market_data_xlsx_tables(tables, source_file="notes.xlsx")


if __name__ == "__main__":
    unittest.main()
