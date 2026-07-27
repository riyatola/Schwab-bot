"""
Time-series storage for OHLCV bars and option chain snapshots.

Design goals:
  - Shares the same SQLite file as everything else (bot.db) by default —
    no new infra to stand up. Schema is append-only and indexed by
    (symbol, timestamp) so migrating to TimescaleDB/InfluxDB later is an
    export+load, not a rewrite.
  - Every write is idempotent (INSERT OR REPLACE) — recording the same
    symbol/timestamp twice (e.g. a bot restart mid-cycle) never dupes rows.
  - This module only stores and retrieves. It doesn't know about
    strategies, scoring, or trading — kept dumb on purpose so it's easy
    to reason about and easy to migrate later.

READ THIS BEFORE BACKTESTING ON THIS DATA
------------------------------------------
Schwab's API — like most retail broker APIs — does not expose HISTORICAL
option chains, only the CURRENT chain, right now. There is no endpoint
that returns "AAPL's option chain as of six months ago." That means real
historical option bid/ask/IV/delta doesn't exist anywhere in this pipeline
unless you either:
  (a) buy it from a vendor (CBOE DataShop, ORATS, OptionMetrics), or
  (b) let this module accumulate live snapshots going forward via
      record_option_chain_snapshot(), called once per scan cycle, and
      wait — in a year you'll have a real proprietary historical dataset.

Until (a) or (b) gives you real data, backtest_engine.py falls back to
SYNTHETIC option pricing (Black-Scholes off historical underlying price +
a volatility estimate — see bs_pricing.py). That's good enough to
sanity-check strategy LOGIC (do the entry/exit rules behave sensibly, does
position sizing do what you expect, does the approval/risk pipeline gate
correctly) — it is NOT good enough to estimate real historical P&L, because
real option prices deviate from Black-Scholes theoretical value in ways
that matter (skew, term structure, liquidity, actual realized vs. assumed
vol). Treat synthetic-mode backtest results as a logic check, not alpha
confirmation.
"""

import json
import logging
import sqlite3
from datetime import datetime, date

log = logging.getLogger("timeseries")

SCHEMA = """
CREATE TABLE IF NOT EXISTS ohlcv_bars (
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,     -- '1d', '1h', '1m', etc.
    ts TEXT NOT NULL,            -- ISO date (daily) or ISO datetime (intraday)
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (symbol, timeframe, ts)
);
CREATE INDEX IF NOT EXISTS idx_bars_symbol_ts ON ohlcv_bars(symbol, ts);

CREATE TABLE IF NOT EXISTS option_chain_snapshots (
    symbol TEXT NOT NULL,
    snapshot_ts TEXT NOT NULL,   -- when this snapshot was taken
    expiration TEXT NOT NULL,
    option_type TEXT NOT NULL,   -- 'call' or 'put'
    strike REAL NOT NULL,
    bid REAL, ask REAL, delta REAL, iv REAL, occ_symbol TEXT,
    PRIMARY KEY (symbol, snapshot_ts, expiration, option_type, strike)
);
CREATE INDEX IF NOT EXISTS idx_chain_symbol_ts ON option_chain_snapshots(symbol, snapshot_ts);
"""


class TimeSeriesStore:
    def __init__(self, db_path="./bot.db"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---------- OHLCV bars ----------

    def record_bars(self, symbol: str, timeframe: str, bars: list[dict]):
        """bars: list of {ts, open, high, low, close, volume}. ts must be
        an ISO date/datetime string. Safe to call repeatedly with
        overlapping data — later calls overwrite same (symbol,timeframe,ts)."""
        rows = [
            (symbol.upper(), timeframe, b["ts"], b.get("open"), b.get("high"),
             b.get("low"), b.get("close"), b.get("volume"))
            for b in bars
        ]
        self.conn.executemany(
            "INSERT OR REPLACE INTO ohlcv_bars "
            "(symbol, timeframe, ts, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def get_bars(self, symbol: str, timeframe: str = "1d",
                 start: str = None, end: str = None) -> list[dict]:
        """Returns bars sorted ascending by ts. start/end are inclusive ISO
        date/datetime strings; omit either for an open-ended range."""
        query = "SELECT ts, open, high, low, close, volume FROM ohlcv_bars WHERE symbol=? AND timeframe=?"
        params = [symbol.upper(), timeframe]
        if start:
            query += " AND ts >= ?"
            params.append(start)
        if end:
            query += " AND ts <= ?"
            params.append(end)
        query += " ORDER BY ts ASC"
        rows = self.conn.execute(query, params).fetchall()
        return [
            {"ts": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]}
            for r in rows
        ]

    def import_bars_csv(self, symbol: str, timeframe: str, csv_path: str,
                         date_col="Date", o="Open", h="High", l="Low", c="Close", v="Volume"):
        """Bulk-load historical bars from a CSV (e.g. exported from
        Schwab's price history, Stooq, or any vendor export). Column names
        are configurable since every export tool names them differently."""
        import csv as _csv
        bars = []
        with open(csv_path, newline="") as f:
            reader = _csv.DictReader(f)
            for row in reader:
                try:
                    ts = row[date_col]
                    # normalize to ISO if it parses as a common date format
                    try:
                        ts = datetime.strptime(ts, "%Y-%m-%d").date().isoformat()
                    except ValueError:
                        pass  # leave as-is if already ISO or an unrecognized format
                    bars.append({
                        "ts": ts,
                        "open": float(row[o]) if row.get(o) not in (None, "") else None,
                        "high": float(row[h]) if row.get(h) not in (None, "") else None,
                        "low": float(row[l]) if row.get(l) not in (None, "") else None,
                        "close": float(row[c]) if row.get(c) not in (None, "") else None,
                        "volume": float(row[v]) if row.get(v) not in (None, "") else None,
                    })
                except KeyError as e:
                    raise ValueError(
                        f"CSV missing expected column {e} — pass date_col/o/h/l/c/v kwargs "
                        f"matching your file's actual headers: {reader.fieldnames}"
                    )
        return self.record_bars(symbol, timeframe, bars)

    def coverage_summary(self, symbol: str = None) -> list[dict]:
        """Quick sanity check on what history you actually have before
        trusting a backtest window."""
        query = "SELECT symbol, timeframe, COUNT(*), MIN(ts), MAX(ts) FROM ohlcv_bars"
        params = []
        if symbol:
            query += " WHERE symbol=?"
            params.append(symbol.upper())
        query += " GROUP BY symbol, timeframe ORDER BY symbol, timeframe"
        rows = self.conn.execute(query, params).fetchall()
        return [
            {"symbol": r[0], "timeframe": r[1], "bar_count": r[2], "first": r[3], "last": r[4]}
            for r in rows
        ]

    # ---------- option chain snapshots ----------

    def record_option_chain_snapshot(self, symbol: str, chain: dict, snapshot_ts: str = None):
        """chain: the dict shape returned by MarketDataAdapter.get_option_chain,
        i.e. {expiration: {"calls": [...], "puts": [...]}}. Call this once
        per scan cycle (wire into scanner.py) to build real historical
        coverage over time — see module docstring."""
        snapshot_ts = snapshot_ts or datetime.utcnow().isoformat()
        rows = []
        for expiration, sides in chain.items():
            for option_type, contracts in (("call", sides.get("calls", [])), ("put", sides.get("puts", []))):
                for c in contracts:
                    rows.append((
                        symbol.upper(), snapshot_ts, expiration, option_type, c.get("strike"),
                        c.get("bid"), c.get("ask"), c.get("delta"), c.get("iv"), c.get("symbol"),
                    ))
        if not rows:
            return 0
        self.conn.executemany(
            "INSERT OR REPLACE INTO option_chain_snapshots "
            "(symbol, snapshot_ts, expiration, option_type, strike, bid, ask, delta, iv, occ_symbol) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def get_option_chain_snapshot(self, symbol: str, as_of_ts: str = None) -> dict:
        """Returns the most recent snapshot at or before as_of_ts (or the
        latest snapshot if as_of_ts is None), reshaped back into the same
        {expiration: {"calls":[...], "puts":[...]}} structure
        MarketDataAdapter.get_option_chain returns, so it's a drop-in
        substitute for strategy modules. Returns {} if no snapshot exists
        yet for this symbol at/before that time."""
        query = "SELECT MAX(snapshot_ts) FROM option_chain_snapshots WHERE symbol=?"
        params = [symbol.upper()]
        if as_of_ts:
            query += " AND snapshot_ts <= ?"
            params.append(as_of_ts)
        row = self.conn.execute(query, params).fetchone()
        if not row or not row[0]:
            return {}
        chosen_ts = row[0]

        rows = self.conn.execute(
            "SELECT expiration, option_type, strike, bid, ask, delta, iv, occ_symbol "
            "FROM option_chain_snapshots WHERE symbol=? AND snapshot_ts=?",
            (symbol.upper(), chosen_ts),
        ).fetchall()

        chain = {}
        for expiration, option_type, strike, bid, ask, delta, iv, occ_symbol in rows:
            chain.setdefault(expiration, {"calls": [], "puts": []})
            bucket = "calls" if option_type == "call" else "puts"
            chain[expiration][bucket].append({
                "strike": strike, "bid": bid, "ask": ask, "delta": delta, "iv": iv, "symbol": occ_symbol,
            })
        return chain
