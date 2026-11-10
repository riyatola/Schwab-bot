"""
Acceptance tests for the LEAPS scanner (spec section 12).

Strategy modules take a fake `market_data`, so most of this file is a
synthetic chain + a tiny fake adapter. Everything here runs offline — no
Schwab, FRED or Finnhub calls.

Run either way:
    python -m unittest tests.test_leaps -v
    python tests/test_leaps.py
"""

import copy
import json
import logging
import os
import sys
import unittest
from datetime import date, datetime, timedelta

# Make the project root importable when this file is run directly.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import yaml

import strategies.leaps as leaps
from strategies.base import occ_symbol
from risk_manager import RiskManager
from market_data import MarketDataAdapter
from sentiment import SentimentEngine
from exit_manager import ExitManager
from approval_manager import Db
from trade_tracker import ensure_trade_outcome_columns
from notifier import format_candidate_message


# --------------------------------------------------------------------------
# config + fakes
# --------------------------------------------------------------------------

def load_config() -> dict:
    """Real config.yaml when present (so these tests also assert the LEAPS
    config wiring is in place), else the committed example."""
    for name in ("config.yaml", "config.example.yaml"):
        path = os.path.join(_ROOT, name)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                return yaml.safe_load(fh) or {}
    return {}


def expiry(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def make_call(strike, bid, ask, delta, oi=500, days=None):
    return {
        "strike": strike, "bid": bid, "ask": ask, "delta": delta,
        "iv": 0.30, "symbol": "SYN", "volume": 100,
        "open_interest": oi, "dte": days,
    }


def one_exp_chain(days, calls):
    return {expiry(days): {"calls": list(calls), "puts": []}}


class FakeMarketData:
    """Stand-in for MarketDataAdapter exposing only what leaps.scan uses."""

    def __init__(self, symbols: dict):
        self.symbols = symbols

    def get_iv_rank(self, symbol):
        return self.symbols[symbol]["iv_rank"]

    def get_trend_strength(self, symbol, lookback_days):
        return self.symbols[symbol]["trend"]

    def get_last_price(self, symbol):
        return self.symbols[symbol]["last_price"]

    def get_sma(self, symbol, n):
        return self.symbols[symbol]["smas"].get(n)

    def get_earnings_date(self, symbol):
        return self.symbols[symbol].get("earnings_date")

    def get_option_chain(self, symbol, dte_min, dte_max, strike_count=100):
        return self.symbols[symbol]["chain"]


def good_symbol(spot=100.0, trend=0.45, iv_rank=30, days=500):
    """A symbol that passes every LEAPS filter with one valid 0.80-delta
    call (strike 80 -> $20 intrinsic + $1 extrinsic on a $100 spot)."""
    return {
        "iv_rank": iv_rank,
        "trend": trend,
        "last_price": spot,
        "smas": {200: 80.0, 50: 95.0},
        "earnings_date": None,
        "chain": one_exp_chain(days, [make_call(80.0, 20.8, 21.2, 0.80, days=days)]),
    }


class FakeDb:
    """Minimal risk_manager db surface."""

    def __init__(self, open_leaps=0.0, sector_risk=None, trades_today=0):
        self.open_leaps = open_leaps
        self.sector_risk = sector_risk or {}
        self.trades_today = trades_today

    def count_trades_today(self, day_iso):
        return self.trades_today

    def open_position_risk_by_sector(self, sector_map):
        return dict(self.sector_risk)

    def open_leaps_premium(self):
        return self.open_leaps


class _FakeResp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class FakeSchwabClient:
    """Records the option_chains kwargs so tests can assert the request
    window, and replays a canned payload."""

    def __init__(self, payload):
        self.payload = payload
        self.chain_calls = []

    def option_chains(self, symbol, contractType="ALL", strikeCount=100,
                      fromDate=None, toDate=None):
        self.chain_calls.append({
            "symbol": symbol, "strikeCount": strikeCount,
            "fromDate": fromDate, "toDate": toDate,
        })
        return _FakeResp(self.payload)


# --------------------------------------------------------------------------
# 1. happy path
# --------------------------------------------------------------------------

class TestHappyPath(unittest.TestCase):
    def test_one_candidate_per_symbol(self):
        cfg = load_config()
        md = FakeMarketData({"XYZ": good_symbol()})

        out = leaps.scan(None, ["XYZ"], cfg, md)

        self.assertEqual(len(out), 1)
        c = out[0]
        self.assertEqual(c["strategy"], "leaps")
        self.assertFalse(c["is_credit"])
        self.assertEqual(c["symbol"], "XYZ")
        self.assertEqual(c["est_price"], 21.0)
        self.assertEqual(c["max_loss_per_contract"], 21.0 * 100)
        # extras the alert/learning layers rely on
        for key in ("delta", "extrinsic", "annualized_extrinsic_pct",
                    "breakeven", "breakeven_move_pct", "capital_saved_pct",
                    "delta_leverage"):
            self.assertIn(key, c)
        self.assertTrue(0.0 <= c["score"] <= 1.0)
        self.assertEqual(c["legs"][0]["instruction"], "BUY_TO_OPEN")


# --------------------------------------------------------------------------
# 2. each hard reject returns nothing
# --------------------------------------------------------------------------

class TestHardRejects(unittest.TestCase):
    def _scan(self, symbol):
        return leaps.scan(None, ["XYZ"], load_config(), FakeMarketData({"XYZ": symbol}))

    def test_delta_out_of_range(self):
        s = good_symbol()
        s["chain"] = one_exp_chain(500, [make_call(80.0, 20.8, 21.2, 0.50, days=500)])
        self.assertEqual(self._scan(s), [])

    def test_dte_below_min(self):
        s = good_symbol()
        s["chain"] = one_exp_chain(300, [make_call(80.0, 20.8, 21.2, 0.80, days=300)])
        self.assertEqual(self._scan(s), [])

    def test_wide_spread(self):
        s = good_symbol()
        s["chain"] = one_exp_chain(500, [make_call(80.0, 10.0, 30.0, 0.80, days=500)])
        self.assertEqual(self._scan(s), [])

    def test_zero_bid(self):
        s = good_symbol()
        s["chain"] = one_exp_chain(500, [make_call(80.0, 0.0, 21.2, 0.80, days=500)])
        self.assertEqual(self._scan(s), [])

    def test_low_open_interest(self):
        s = good_symbol()
        s["chain"] = one_exp_chain(500, [make_call(80.0, 20.8, 21.2, 0.80, oi=10, days=500)])
        self.assertEqual(self._scan(s), [])

    def test_extrinsic_too_high(self):
        # strike 90 on a $100 spot -> $10 intrinsic, $30 extrinsic -> ~22%/yr
        s = good_symbol()
        s["chain"] = one_exp_chain(500, [make_call(90.0, 39.8, 40.2, 0.80, days=500)])
        self.assertEqual(self._scan(s), [])

    def test_iv_rank_too_high(self):
        self.assertEqual(self._scan(good_symbol(iv_rank=70)), [])

    def test_below_200dma(self):
        s = good_symbol(spot=70.0)
        self.assertEqual(self._scan(s), [])

    def test_weak_trend(self):
        self.assertEqual(self._scan(good_symbol(trend=0.1)), [])

    def test_earnings_inside_window(self):
        s = good_symbol()
        s["earnings_date"] = date.today() + timedelta(days=1)
        self.assertEqual(self._scan(s), [])


# --------------------------------------------------------------------------
# 3. best expiration per symbol
# --------------------------------------------------------------------------

class TestBestExpiration(unittest.TestCase):
    def test_only_highest_score_returned(self):
        cfg = load_config()
        chain = {}
        # identical contracts; the longest-dated one has the lowest
        # annualized extrinsic -> highest carry score -> wins.
        for days in (400, 500, 600):
            chain[expiry(days)] = {
                "calls": [make_call(80.0, 20.8, 21.2, 0.80, days=days)],
                "puts": [],
            }
        s = good_symbol()
        s["chain"] = chain

        out = leaps.scan(None, ["XYZ"], cfg, FakeMarketData({"XYZ": s}))

        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["expiration"], expiry(600))


# --------------------------------------------------------------------------
# 4. score bounds + monotone in extrinsic
# --------------------------------------------------------------------------

class TestScoring(unittest.TestCase):
    def _score_for(self, mid):
        s = good_symbol()
        s["chain"] = one_exp_chain(500, [make_call(80.0, mid - 0.2, mid + 0.2, 0.80, days=500)])
        out = leaps.scan(None, ["XYZ"], load_config(), FakeMarketData({"XYZ": s}))
        self.assertEqual(len(out), 1)
        return out[0]["score"]

    def test_bounds_and_monotone(self):
        low_ext = self._score_for(21.0)   # $1 extrinsic
        high_ext = self._score_for(25.0)  # $5 extrinsic
        self.assertTrue(0.0 <= low_ext <= 1.0)
        self.assertTrue(0.0 <= high_ext <= 1.0)
        self.assertGreater(low_ext, high_ext)


# --------------------------------------------------------------------------
# 5. leg_spread_pct works for long-dated contracts (market_data fix)
# --------------------------------------------------------------------------

class TestLegSpreadLongDated(unittest.TestCase):
    def test_returns_real_value_for_500_dte(self):
        exp = expiry(500)
        strike = 80.0
        payload = {
            "callExpDateMap": {
                f"{exp}:{strike}": {
                    "80.0": [{
                        "strikePrice": strike, "bid": 20.8, "ask": 21.2,
                        "delta": 0.80, "volatility": 0.30, "symbol": "AAPL",
                        "totalVolume": 100, "openInterest": 500,
                        "daysToExpiration": 500,
                    }],
                },
            },
            "putExpDateMap": {},
        }
        client = FakeSchwabClient(payload)
        md = MarketDataAdapter(client=client)

        occ = occ_symbol("AAPL", exp, "C", strike)
        spread = md.leg_spread_pct(occ)

        self.assertIsNotNone(spread, "500-DTE leg must be found, not fail open")
        self.assertAlmostEqual(spread, (21.2 - 20.8) / 21.0, places=6)
        # window must be derived from the leg's own expiration, not 0..400
        target_dte = (datetime.strptime(exp, "%Y-%m-%d").date() - date.today()).days
        self.assertEqual(client.chain_calls[0]["fromDate"],
                         (date.today() + timedelta(days=target_dte - 7)).isoformat())
        self.assertEqual(client.chain_calls[0]["toDate"],
                         (date.today() + timedelta(days=target_dte + 7)).isoformat())


# --------------------------------------------------------------------------
# 6. LEAPS-aware risk sizing + exposure gate
# --------------------------------------------------------------------------

class TestRisk(unittest.TestCase):
    SNAPSHOT_5K = {"net_liq": 5000, "cash": 5000, "day_trades_left": 0, "positions": []}
    SNAPSHOT_50K = {"net_liq": 50000, "cash": 50000, "day_trades_left": 0, "positions": []}

    def test_small_account_rejects_expensive_leap(self):
        rm = RiskManager(load_config(), FakeDb())
        cand = {"symbol": "XYZ", "strategy": "leaps", "max_loss_per_contract": 2500.0}

        rm.size_candidate(cand, 5000)

        self.assertEqual(cand["suggested_qty"], 0)
        self.assertTrue(cand.get("rejected_reason"))
        self.assertFalse(rm.gate(cand, self.SNAPSHOT_5K))

    def test_large_account_sizes_to_one_and_passes_gate(self):
        rm = RiskManager(load_config(), FakeDb())
        # $3,000 premium -> 10% of $50k = $5,000 cap -> qty 1
        cand = {"symbol": "XYZ", "strategy": "leaps", "max_loss_per_contract": 3000.0}

        rm.size_candidate(cand, 50000)

        self.assertEqual(cand["suggested_qty"], 1)
        # passes even though $3,000 > the generic 4% sector cap ($2,000) —
        # proves the LEAPS-specific 15% sector cap is in effect.
        self.assertTrue(rm.gate(cand, self.SNAPSHOT_50K))

    def test_total_leaps_exposure_cap_blocks(self):
        rm = RiskManager(load_config(), FakeDb(open_leaps=14000.0))
        cand = {"symbol": "XYZ", "strategy": "leaps", "max_loss_per_contract": 3000.0}

        rm.size_candidate(cand, 50000)

        # 14,000 open + 3,000 candidate = 17,000 > 30% of 50,000 (15,000)
        self.assertFalse(rm.gate(cand, self.SNAPSHOT_50K))


# --------------------------------------------------------------------------
# 7. cooldown dedupe
# --------------------------------------------------------------------------

class TestCooldown(unittest.TestCase):
    def test_recent_approval_blocks_second_alert(self):
        db = Db(":memory:")
        cand = {"symbol": "AAPL", "strategy": "leaps"}
        db.create_approval(cand, 30)

        self.assertTrue(db.recent_approval_exists("AAPL", "leaps", 7))
        self.assertFalse(db.recent_approval_exists("MSFT", "leaps", 7))
        self.assertFalse(db.recent_approval_exists("AAPL", "leaps", 0))

        # mirrors scanner.run_leaps_cycle's guard: no new approval created
        before = db.conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]
        if not db.recent_approval_exists("AAPL", "leaps", 7):
            db.create_approval(cand, 30)
        after = db.conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]
        self.assertEqual(before, after)


# --------------------------------------------------------------------------
# 8. sentiment: risk-off halves LEAPS, VIX band not applied
# --------------------------------------------------------------------------

class TestSentiment(unittest.TestCase):
    def test_risk_off_halves_and_no_vix_band(self):
        cfg = load_config()
        cfg["earnings"]["finnhub_api_key"] = ""   # keep the test offline
        cfg["fred"]["api_key"] = ""
        db = Db(":memory:")
        eng = SentimentEngine(cfg, db)

        today = date.today().isoformat()
        db.conn.execute(
            "INSERT OR REPLACE INTO macro_regime_history (date, raw_regime, confirmed_regime) "
            "VALUES (?, ?, ?)", (today, "RISK_OFF", "RISK_OFF"),
        )
        db.conn.commit()

        cand = {"symbol": "AAPL", "strategy": "leaps", "score": 0.80}
        eng.apply(cand)

        self.assertEqual(cand["macro_regime"], "RISK_OFF")
        self.assertEqual(cand["regime_multiplier"], 0.5)
        self.assertAlmostEqual(cand["score"], 0.40, places=3)
        self.assertNotIn("vix_band_multiplier", cand)


# --------------------------------------------------------------------------
# 9. exit manager: time-decay exit for LEAPS only
# --------------------------------------------------------------------------

class ExitMarketData:
    """Returns a chain containing the test leg so MTM resolves."""

    def __init__(self, days, strike, bid, ask):
        self.exp = expiry(days)
        self.strike = strike
        self.bid = bid
        self.ask = ask

    def get_option_chain(self, symbol, dte_min, dte_max, strike_count=100):
        return {self.exp: {"calls": [{
            "strike": self.strike, "bid": self.bid, "ask": self.ask,
            "delta": 0.80, "iv": 0.30,
        }], "puts": []}}

    def get_last_price(self, symbol):
        return 100.0


class TestExitManager(unittest.TestCase):
    def _insert_trade(self, conn, trade_id, strategy, order_json, entry_price, is_credit):
        ensure_trade_outcome_columns(conn)
        conn.execute(
            "INSERT INTO trades (id, symbol, strategy, order_json, status, outcome, "
            "entry_price, stop_loss, take_profit, is_credit, created_at) "
            "VALUES (?, ?, ?, ?, 'EXECUTED', 'OPEN', ?, NULL, NULL, ?, ?)",
            (trade_id, "AAPL", strategy, json.dumps(order_json), entry_price,
             is_credit, datetime.utcnow().isoformat()),
        )
        conn.commit()

    def test_leaps_100_dte_triggers_time_decay_exit(self):
        cfg = load_config()
        db = Db(":memory:")
        occ = occ_symbol("AAPL", expiry(100), "C", 80.0)
        self._insert_trade(db.conn, "t-leaps", "leaps", {
            "orderLegCollection": [{
                "instruction": "BUY_TO_OPEN", "quantity": 1,
                "instrument": {"symbol": occ, "assetType": "OPTION"},
            }],
        }, entry_price=21.0, is_credit=0)

        em = ExitManager(cfg, db, client=None, account_hash="h",
                         market_data=ExitMarketData(100, 80.0, 20.8, 21.2))
        triggers = em.check_triggers()

        self.assertEqual(len(triggers), 1)
        self.assertEqual(triggers[0]["trigger_type"], "time_decay_exit")
        self.assertEqual(triggers[0]["strategy"], "leaps")

    def test_credit_spread_100_dte_does_not_trigger(self):
        cfg = load_config()
        db = Db(":memory:")
        exp = expiry(100)
        self._insert_trade(db.conn, "t-cs", "credit_spreads", {
            "orderLegCollection": [
                {"instruction": "SELL_TO_OPEN", "quantity": 1,
                 "instrument": {"symbol": occ_symbol("AAPL", exp, "C", 105.0),
                                "assetType": "OPTION"}},
                {"instruction": "BUY_TO_OPEN", "quantity": 1,
                 "instrument": {"symbol": occ_symbol("AAPL", exp, "C", 110.0),
                                "assetType": "OPTION"}},
            ],
        }, entry_price=1.35, is_credit=1)

        em = ExitManager(cfg, db, client=None, account_hash="h",
                         market_data=ExitMarketData(100, 105.0, 1.30, 1.40))
        triggers = em.check_triggers()

        self.assertEqual(triggers, [])


# --------------------------------------------------------------------------
# 10. no secrets leak into logs or alert messages
# --------------------------------------------------------------------------

class TestNoSecretLeak(unittest.TestCase):
    def test_secrets_absent_from_logs_and_message(self):
        cfg = load_config()
        secrets = [
            cfg.get("schwab", {}).get("app_key"),
            cfg.get("schwab", {}).get("app_secret"),
            cfg.get("telegram", {}).get("bot_token"),
            cfg.get("earnings", {}).get("finnhub_api_key"),
            cfg.get("fred", {}).get("api_key"),
        ]
        secrets = [s for s in secrets if s]

        records = []

        class _Capture(logging.Handler):
            def emit(self, record):
                try:
                    records.append(record.getMessage())
                except Exception:
                    records.append(str(record.msg))

        logger = logging.getLogger("strategies.leaps")
        handler = _Capture()
        logger.addHandler(handler)
        level = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            md = FakeMarketData({"XYZ": good_symbol()})
            out = leaps.scan(None, ["XYZ"], cfg, md)
            message = format_candidate_message(out[0]) if out else ""
        finally:
            logger.removeHandler(handler)
            logger.setLevel(level)

        blob = "\n".join(records) + "\n" + message
        for s in secrets:
            self.assertNotIn(s, blob)


if __name__ == "__main__":
    unittest.main(verbosity=2)
