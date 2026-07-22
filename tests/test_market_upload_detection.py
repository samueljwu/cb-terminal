import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cb_terminal.storage.price_history_store import PriceHistoryStore
from cb_terminal.web import server


class MarketUploadDetectionTests(unittest.TestCase):
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
                server._attach_auto_market_data_parse_summary(result, source, {})

        self.assertEqual(result["detected_market_data_types"], ["cb_quote_history"])
        self.assertEqual(result["market_data_breakdown"]["cb_quote_rows"], 1)
        self.assertEqual(result["warnings"], [])


if __name__ == "__main__":
    unittest.main()
