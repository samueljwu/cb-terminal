import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cb_terminal.domain import FXConvention
from cb_terminal.storage.price_history_store import PriceHistoryStore
from cb_terminal.web.server import _latest_traded_yield_payload


class MarketFxReadinessTests(unittest.TestCase):
    def test_linked_traded_yield_receives_uploaded_fx_for_quote_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            quotes = root / "quotes.csv"
            fx = root / "fx.csv"
            quotes.write_text(
                "contract_id,isin,reference_security,date,time,dealer,security,market_price\n"
                "test,XS0000000001,Test CB,2026-10-22,15:00,BVAL,TEST 0 2031,101.5\n",
                encoding="utf-8",
            )
            fx.write_text(
                "date,instrument_id,value\n2026-10-22,USDTWD Curncy,31.75\n",
                encoding="utf-8",
            )
            store = PriceHistoryStore(root / "prices.sqlite")
            store.import_file(quotes)
            store.import_market_data_file(fx)
            requirements = {
                "cb_instrument_id": "XS0000000001",
                "equity_instrument_id": "",
                "fx_instrument_id": "USDTWD Curncy",
                "fx_convention": "STOCK_PER_CB",
                "stock_currency": "TWD",
                "bond_price_currency": "USD",
                "contract": SimpleNamespace(cash_fx_linked=True),
            }
            with patch("cb_terminal.web.server.calculate_market_yields", return_value={}) as calculate:
                result = _latest_traded_yield_payload(requirements, store)
            self.assertEqual(result["as_of_date"], "2026-10-22")
            market = calculate.call_args.kwargs["market"]
            self.assertEqual(market.fx_rate, 31.75)
            self.assertEqual(market.fx_convention, FXConvention.STOCK_PER_CB)
            self.assertEqual(market.as_of_date.isoformat(), "2026-10-22")

            fx.unlink()
            with patch("cb_terminal.web.server.calculate_market_yields") as calculate:
                self.assertIsNone(_latest_traded_yield_payload(requirements, store))
            calculate.assert_not_called()
