import tempfile
import unittest
from datetime import date
from pathlib import Path

from cb_terminal.io.price_history import PriceQuoteRow, load_price_history_file
from cb_terminal.io.price_history_preprocess import select_daily_quotes
from cb_terminal.storage.price_history_store import PriceHistoryStore


TIANQI_ISIN = "XS3291776451"
WIWYNN_ISIN = "XS3236970433"


def _quote(
    *,
    instrument_id: str,
    as_of_date: date,
    dealer: str,
    bid: float | None,
    ask: float | None,
    stock: float,
    source_row: int,
) -> PriceQuoteRow:
    if bid is not None and ask is not None:
        mid = (bid + ask) / 2.0
    else:
        mid = bid if bid is not None else ask
    return PriceQuoteRow(
        reference_security=f"{instrument_id} Corp",
        as_of_date=as_of_date,
        dealer=dealer,
        bid_price=bid,
        ask_price=ask,
        mid_price=mid,
        stock_price=stock,
        instrument_id=instrument_id,
        source_file="synthetic-quotes.csv",
        source_sheet="csv",
        source_row=source_row,
    )


class RobustDailyQuoteSelectionTests(unittest.TestCase):
    def test_tianqi_like_bad_print_does_not_win_stock_close_match(self):
        as_of_date = date(2026, 4, 30)
        rows = [
            _quote(
                instrument_id=TIANQI_ISIN,
                as_of_date=as_of_date,
                dealer="Dealer A",
                bid=124.0,
                ask=125.0,
                stock=66.45,
                source_row=2,
            ),
            _quote(
                instrument_id=TIANQI_ISIN,
                as_of_date=as_of_date,
                dealer="Dealer B",
                bid=122.0,
                ask=123.0,
                stock=66.45,
                source_row=3,
            ),
            _quote(
                instrument_id=TIANQI_ISIN,
                as_of_date=as_of_date,
                dealer="Dealer C",
                bid=122.5,
                ask=123.5,
                stock=66.45,
                source_row=4,
            ),
            # Same underlying value as the official close, but an isolated CB
            # price roughly five points below the dealer consensus.
            _quote(
                instrument_id=TIANQI_ISIN,
                as_of_date=as_of_date,
                dealer="Bad Print",
                bid=116.58,
                ask=117.58,
                stock=66.45,
                source_row=5,
            ),
            _quote(
                instrument_id=TIANQI_ISIN,
                as_of_date=as_of_date,
                dealer="Bid Only",
                bid=121.5,
                ask=None,
                stock=66.45,
                source_row=6,
            ),
        ]

        selected = select_daily_quotes(rows, isin=TIANQI_ISIN, stock_closes={as_of_date: 66.45})

        self.assertEqual(len(selected), 1)
        self.assertAlmostEqual(selected[0].bond_price, 123.0)
        self.assertEqual(selected[0].cb_quote_dealer, "Dealer C")
        self.assertIn("two_sided", selected[0].selection_reason)
        self.assertIn("outliers_excluded:1", selected[0].selection_reason)
        self.assertIn("lower_quality_quotes_ignored:1", selected[0].selection_reason)
        self.assertIn("missing_timestamp", selected[0].selection_reason)

    def test_tianqi_like_stock_move_is_not_mistaken_for_a_cb_price_outlier(self):
        as_of_date = date(2026, 5, 8)
        rows = [
            _quote(
                instrument_id=TIANQI_ISIN,
                as_of_date=as_of_date,
                dealer="Close Snapshot",
                bid=117.5,
                ask=119.5,
                stock=62.25,
                source_row=2,
            ),
            _quote(
                instrument_id=TIANQI_ISIN,
                as_of_date=as_of_date,
                dealer="Earlier A",
                bid=123.5,
                ask=125.5,
                stock=67.0,
                source_row=3,
            ),
            _quote(
                instrument_id=TIANQI_ISIN,
                as_of_date=as_of_date,
                dealer="Earlier B",
                bid=124.25,
                ask=125.25,
                stock=67.0,
                source_row=4,
            ),
            _quote(
                instrument_id=TIANQI_ISIN,
                as_of_date=as_of_date,
                dealer="Earlier C",
                bid=124.0,
                ask=126.5,
                stock=67.0,
                source_row=5,
            ),
        ]

        selected = select_daily_quotes(rows, isin=TIANQI_ISIN, stock_closes={as_of_date: 62.25})

        self.assertAlmostEqual(selected[0].bond_price, 118.5)
        self.assertEqual(selected[0].cb_quote_dealer, "Close Snapshot")
        self.assertIn("different_stock_snapshots_ignored:3", selected[0].selection_reason)
        self.assertNotIn("outliers_excluded", selected[0].selection_reason)

    def test_wiwynn_like_outlier_and_bid_only_indication_are_not_selected(self):
        as_of_date = date(2026, 5, 12)
        rows = [
            _quote(
                instrument_id=WIWYNN_ISIN,
                as_of_date=as_of_date,
                dealer="Dealer A",
                bid=178.8,
                ask=179.8,
                stock=5115.0,
                source_row=2,
            ),
            _quote(
                instrument_id=WIWYNN_ISIN,
                as_of_date=as_of_date,
                dealer="Dealer B",
                bid=179.0,
                ask=179.8,
                stock=5115.0,
                source_row=3,
            ),
            _quote(
                instrument_id=WIWYNN_ISIN,
                as_of_date=as_of_date,
                dealer="Dealer C",
                bid=179.5,
                ask=180.5,
                stock=5115.0,
                source_row=4,
            ),
            _quote(
                instrument_id=WIWYNN_ISIN,
                as_of_date=as_of_date,
                dealer="Bad Print",
                bid=168.25,
                ask=169.25,
                stock=5115.0,
                source_row=5,
            ),
            _quote(
                instrument_id=WIWYNN_ISIN,
                as_of_date=as_of_date,
                dealer="Bid Only",
                bid=183.55,
                ask=None,
                stock=5115.0,
                source_row=6,
            ),
        ]

        selected = select_daily_quotes(rows, isin=WIWYNN_ISIN, stock_closes={as_of_date: 5115.0})

        self.assertAlmostEqual(selected[0].bond_price, 179.4)
        self.assertEqual(selected[0].cb_quote_dealer, "Dealer B")
        self.assertIn("outliers_excluded:1", selected[0].selection_reason)
        self.assertIn("lower_quality_quotes_ignored:1", selected[0].selection_reason)

    def test_one_sided_only_day_is_retained_but_explicitly_flagged(self):
        as_of_date = date(2026, 5, 14)
        rows = [
            _quote(
                instrument_id=WIWYNN_ISIN,
                as_of_date=as_of_date,
                dealer="Bid Only",
                bid=133.75,
                ask=None,
                stock=900.0,
                source_row=2,
            )
        ]

        selected = select_daily_quotes(rows, isin=WIWYNN_ISIN, stock_closes={as_of_date: 900.0})

        self.assertAlmostEqual(selected[0].bond_price, 133.75)
        self.assertIn("one_sided_fallback", selected[0].selection_reason)
        self.assertIn("missing_timestamp", selected[0].selection_reason)

    def test_evaluated_mid_is_not_mislabeled_as_one_sided(self):
        as_of_date = date(2026, 5, 22)
        row = PriceQuoteRow(
            reference_security="Wiwynn BVAL",
            as_of_date=as_of_date,
            dealer="BVAL",
            mid_price=135.758,
            stock_price=5345.0,
            instrument_id=WIWYNN_ISIN,
            source_file="synthetic-quotes.csv",
            source_sheet="csv",
            source_row=2,
        )

        selected = select_daily_quotes([row], isin=WIWYNN_ISIN, stock_closes={as_of_date: 5345.0})

        self.assertAlmostEqual(selected[0].bond_price, 135.758)
        self.assertIn("direct_mid_fallback", selected[0].selection_reason)
        self.assertNotIn("one_sided_fallback", selected[0].selection_reason)

    def test_explicit_mid_outside_bid_ask_is_rejected(self):
        as_of_date = date(2026, 5, 22)
        inconsistent = PriceQuoteRow(
            reference_security="Bad mixed snapshot",
            as_of_date=as_of_date,
            dealer="Dealer A",
            bid_price=120.0,
            ask_price=121.0,
            mid_price=200.0,
            stock_price=50.0,
            instrument_id=WIWYNN_ISIN,
            source_file="synthetic-quotes.csv",
            source_sheet="csv",
            source_row=2,
        )

        selected = select_daily_quotes([inconsistent], isin=WIWYNN_ISIN)

        self.assertEqual(selected, [])


class SharedSelectionIntegrationTests(unittest.TestCase):
    def test_preprocessor_and_store_use_identical_selection(self):
        quote_csv = (
            "ISIN,Reference Security,Date,Time,Dealer,Bid Price,Ask Price,Stock Price\n"
            f"{TIANQI_ISIN},Tianqi CB,2026-04-30,,Dealer A,124,125,66.45\n"
            f"{TIANQI_ISIN},Tianqi CB,2026-04-30,,Dealer B,122,123,66.45\n"
            f"{TIANQI_ISIN},Tianqi CB,2026-04-30,,Dealer C,122.5,123.5,66.45\n"
            f"{TIANQI_ISIN},Tianqi CB,2026-04-30,,Bad Print,116.58,117.58,66.45\n"
        )
        market_csv = "date,instrument_id,value\n2026-04-30,2899 HK Equity,66.45\n"

        with tempfile.TemporaryDirectory() as tmp:
            quote_path = Path(tmp) / "synthetic-quotes.csv"
            market_path = Path(tmp) / "synthetic-equity.csv"
            quote_path.write_text(quote_csv, encoding="utf-8")
            market_path.write_text(market_csv, encoding="utf-8")

            direct = select_daily_quotes(
                load_price_history_file(quote_path),
                isin=TIANQI_ISIN,
                stock_closes={date(2026, 4, 30): 66.45},
            )
            store = PriceHistoryStore(Path(tmp) / "quotes.sqlite")
            store.import_file(quote_path)
            store.import_market_data_file(market_path)
            generated = store.build_valuation_market_rows(
                cb_instrument_id=TIANQI_ISIN,
                equity_instrument_id="2899 HK Equity",
            )

        self.assertEqual(len(generated), 1)
        self.assertAlmostEqual(generated[0]["bond_price"], direct[0].bond_price)
        self.assertEqual(generated[0]["cb_quote_dealer"], direct[0].cb_quote_dealer)
        self.assertEqual(generated[0]["cb_selection_reason"], direct[0].selection_reason)


if __name__ == "__main__":
    unittest.main()
