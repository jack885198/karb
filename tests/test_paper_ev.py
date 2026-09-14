"""Regression tests: all fixtures are synthetic, never evidence of market profitability."""
import copy
import unittest
from decimal import Decimal as D
from karb.paper_ev import fee_rate, market_tokens, validate_books, walk, quote, delayed_paper_fill
from karb.api.models import ArbitrageOpportunity


def market(rate="0"):
    return {"active": True, "closed": False, "acceptingOrders": True,
            "enableOrderBook": True, "negRisk": False, "conditionId": "condition-1",
            "outcomes": '["Yes","No"]', "clobTokenIds": '["yes","no"]',
            "orderMinSize": 5, "feesEnabled": True,
            "feeSchedule": {"rate": rate, "exponent": 1, "takerOnly": True}}


def book(token, ask, size="100", bid="0.40"):
    return {"asset_id": token, "market": "condition-1", "timestamp": "10000",
            "asks": [{"price": ask, "size": size}],
            "bids": [{"price": bid, "size": size}]}


def books():
    return {"Yes": book("yes", "0.46"), "No": book("no", "0.51")}


class PaperTests(unittest.TestCase):
    def test_gross_profit_units(self):
        x = ArbitrageOpportunity(None, D(".46"), D(".51"), D(".97"),
                                 D(".03")/D(".97"), D(100), D(100), D(100))
        self.assertEqual(x.expected_profit_usd, D(3))

    def test_zero_fee_set(self):
        r = quote(market(), books(), D(100), 10000, D(1000), D(0))
        self.assertEqual(r["net_if_merged"], D(3))

    def test_crypto_fees_erase_edge(self):
        r = quote(market(".07"), books(), D(100), 10000, D(1000), D(0))
        self.assertEqual(r["fees"], D("3.48810"))
        self.assertEqual(r["net_if_merged"], D("-.48810"))

    def test_multi_level_cost(self):
        b = books()
        b["No"]["asks"] = [{"price": ".56", "size": "90"},
                           {"price": ".51", "size": "10"}]
        r = quote(market(), b, D(100), 10000, D(1000), D(0))
        self.assertEqual(r["net_if_merged"], D("-1.50"))

    def test_fee_per_level_not_vwap(self):
        b = book("yes", ".1", "50")
        b["asks"].append({"price": ".9", "size": "50"})
        amount, fee, _ = walk(b, "asks", D(100), D(".07"))
        self.assertEqual(amount, D(50))
        self.assertEqual(fee, D(".63000"))

    def test_insufficient_depth(self):
        b = books()
        b["No"]["asks"][0]["size"] = "1"
        with self.assertRaisesRegex(ValueError, "insufficient_depth"):
            quote(market(), b, D(100), 10000, D(1000))

    def test_unknown_fee_rejected(self):
        m = market()
        del m["feeSchedule"]
        with self.assertRaisesRegex(ValueError, "unknown_fee"):
            fee_rate(m)

    def test_zero_fees_must_be_explicit(self):
        m = market()
        m["feesEnabled"] = False
        del m["feeSchedule"]
        self.assertEqual(fee_rate(m), 0)

    def test_stale_book(self):
        with self.assertRaisesRegex(ValueError, "stale_book"):
            validate_books(market(), books(), 13000)

    def test_future_book(self):
        with self.assertRaisesRegex(ValueError, "stale_book"):
            validate_books(market(), books(), 9000)

    def test_skew(self):
        b = books()
        b["No"]["timestamp"] = "8500"
        with self.assertRaisesRegex(ValueError, "unsynchronized"):
            validate_books(market(), b, 10000)

    def test_wrong_condition(self):
        b = books()
        b["No"]["market"] = "different"
        with self.assertRaisesRegex(ValueError, "identity"):
            validate_books(market(), b, 10000)

    def test_wrong_token(self):
        b = books()
        b["No"]["asset_id"] = "different"
        with self.assertRaisesRegex(ValueError, "identity"):
            validate_books(market(), b, 10000)

    def test_negative_risk_excluded(self):
        m = market()
        m["negRisk"] = True
        with self.assertRaisesRegex(ValueError, "nonstandard"):
            market_tokens(m)

    def test_cash_limit(self):
        with self.assertRaisesRegex(ValueError, "insufficient_cash"):
            quote(market(), books(), D(100), 10000, D(50))

    def test_both_legs_remain_reserved(self):
        s = quote(market(), books(), D(100), 10000, D(1000), D(0))
        r = delayed_paper_fill(market(), books(), s, 10000)
        self.assertEqual(r["state"], "paired_paper")
        self.assertEqual(r["debit"], D(97))
        self.assertEqual(r["credit"], 0)
        self.assertEqual(r["pending_model_credit"], 100)

    def test_single_leg_unwind_loss(self):
        s = quote(market(), books(), D(100), 10000, D(1000), D(0))
        later = books()
        later["No"]["asks"][0]["price"] = ".55"
        r = delayed_paper_fill(market(), later, s, 10000)
        self.assertEqual(r["state"], "single_leg_unwind_paper")
        self.assertEqual(r["paper_pnl"], D(-6))

    def test_unresolved_leg_not_profit(self):
        s = quote(market(), books(), D(100), 10000, D(1000), D(0))
        later = books()
        later["No"]["asks"] = []
        later["Yes"]["bids"] = []
        r = delayed_paper_fill(market(), later, s, 10000)
        self.assertEqual(r["state"], "unresolved_single_leg")
        self.assertIsNone(r["paper_pnl"])
        self.assertEqual(r["credit"], 0)
        self.assertEqual(r["debit"], D(46))

    def test_no_fills_no_debit(self):
        s = quote(market(), books(), D(100), 10000, D(1000), D(0))
        later = books()
        later["Yes"]["asks"] = []
        later["No"]["asks"] = []
        r = delayed_paper_fill(market(), later, s, 10000)
        self.assertEqual(r["state"], "missed")
        self.assertEqual(r["debit"], 0)


if __name__ == "__main__":
    unittest.main()
