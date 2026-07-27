"""
Records a daily ATM implied-volatility snapshot per symbol so IV rank can
be computed from Schwab's own option-chain IV data instead of the realized-
volatility proxy. Schwab's API gives you current IV per contract but no
52-week IV history endpoint, so we build that history ourselves over time.

Run once per trading day (wire into scanner.py's scheduler, see the
"record_daily_iv" job) — ideally near market close, since that's the most
representative IV snapshot of the day.

Storage: SQLite table iv_history(symbol, date, atm_iv), one row per
symbol per day. Until MIN_OBSERVATIONS_FOR_RANK is met for a symbol,
callers fall back to (in order, see market_data.get_iv_rank):
  1. optionstrategist_iv.ExternalIVStore's percentile, if wired in and
     fresh (see optionstrategist_iv.py + PATCHES.md) — real multi-year
     history from McMillan Analysis Corp's free weekly data, available
     immediately rather than after 20-60 days of your own accumulation.
  2. This module's own iv_rank(), once MIN_OBSERVATIONS_FOR_RANK is met.
  3. The realized-vol-percentile proxy (already wired in market_data.py).

CHANGE (2026-07): MIN_OBSERVATIONS_FOR_RANK lowered from 40 to 20 per
request — roughly a month of daily snapshots instead of two. This is a
real tradeoff, not a free lunch: a 20-day trailing-window rank is noisier
(one unusual week can swing "rank" a lot more than it would in a 252-day
window), and it only kicks in for a symbol you've been actively snapshotting
daily. If you're wiring in optionstrategist_iv.py (recommended — it gives
you a rank with hundreds of days of real history from day one), you likely
won't hit this path often; it mainly matters for symbols outside that
data source's coverage.
"""

import logging
import sqlite3
from datetime import date, timedelta

log = logging.getLogger("iv_history")

SCHEMA = """
CREATE TABLE IF NOT EXISTS iv_history (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    atm_iv REAL NOT NULL,
    PRIMARY KEY (symbol, date)
);
"""

MIN_OBSERVATIONS_FOR_RANK = 20  # lowered from 40 — accept more noise for a faster ramp-up (see module docstring)


class IVHistoryStore:
    def __init__(self, db_path="./bot.db"):
        # Shares the same SQLite file as approval_manager.Db by default —
        # pass the same db_path if you want everything in one file.
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def record(self, symbol: str, atm_iv: float, as_of: date = None):
        as_of = as_of or date.today()
        self.conn.execute(
            "INSERT OR REPLACE INTO iv_history (symbol, date, atm_iv) VALUES (?, ?, ?)",
            (symbol.upper(), as_of.isoformat(), atm_iv),
        )
        self.conn.commit()

    def record_many(self, snapshots: dict[str, float], as_of: date = None):
        as_of = as_of or date.today()
        rows = [(sym.upper(), as_of.isoformat(), iv) for sym, iv in snapshots.items() if iv]
        self.conn.executemany(
            "INSERT OR REPLACE INTO iv_history (symbol, date, atm_iv) VALUES (?, ?, ?)",
            rows,
        )
        self.conn.commit()

    def history(self, symbol: str, lookback_days: int = 252) -> list[float]:
        cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
        rows = self.conn.execute(
            "SELECT atm_iv FROM iv_history WHERE symbol=? AND date >= ? ORDER BY date ASC",
            (symbol.upper(), cutoff),
        ).fetchall()
        return [r[0] for r in rows]

    def iv_rank(self, symbol: str, lookback_days: int = 252) -> float | None:
        """Returns None if there isn't enough history yet — caller should fall back."""
        values = self.history(symbol, lookback_days)
        if len(values) < MIN_OBSERVATIONS_FOR_RANK:
            return None
        current = values[-1]
        lo, hi = min(values), max(values)
        if hi == lo:
            return 50.0
        return max(0.0, min(100.0, 100 * (current - lo) / (hi - lo)))

    def observation_count(self, symbol: str, lookback_days: int = 252) -> int:
        return len(self.history(symbol, lookback_days))


def compute_atm_iv(option_chain_for_expiry: dict, underlying_price: float) -> float | None:
    """
    Given one expiration's {"calls": [...], "puts": [...]} (as returned by
    MarketDataAdapter.get_option_chain), find the strike closest to the
    underlying price and average call/put IV there as the ATM IV snapshot.
    """
    calls = option_chain_for_expiry.get("calls", [])
    puts = option_chain_for_expiry.get("puts", [])
    if not calls or not puts or not underlying_price:
        return None

    atm_call = min(calls, key=lambda c: abs(c["strike"] - underlying_price))
    atm_put = min(puts, key=lambda p: abs(p["strike"] - underlying_price))

    ivs = [x["iv"] for x in (atm_call, atm_put) if x.get("iv")]
    if not ivs:
        return None
    return sum(ivs) / len(ivs)


def record_daily_iv_for_universe(symbols: list[str], market_data, store: IVHistoryStore, dte_target=(25, 35)):
    """
    Job body for the daily scheduled task. Picks a near-the-money expiration
    in the dte_target window (a ~30 DTE snapshot is a reasonably stable,
    commonly-used point on the vol surface) and records ATM IV for each symbol.
    """
    snapshots = {}
    for symbol in symbols:
        try:
            chain = market_data.get_option_chain(symbol, dte_target[0], dte_target[1])
            if not chain:
                continue
            # pick the expiration closest to the middle of the target window
            expiration = next(iter(chain))
            underlying_price = market_data.get_last_price(symbol)
            atm_iv = compute_atm_iv(chain[expiration], underlying_price)
            if atm_iv:
                snapshots[symbol] = atm_iv
        except Exception as e:
            log.warning("Failed to record IV for %s: %s", symbol, e)

    store.record_many(snapshots)
    log.info("Recorded ATM IV for %d/%d symbols", len(snapshots), len(symbols))
    return snapshots
