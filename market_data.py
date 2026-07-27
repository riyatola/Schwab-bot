"""
Adapter between raw Schwab API responses and the clean interface the
strategy modules expect. Keeping this separate means: (a) strategy modules
are unit-testable with fake data, (b) if Schwab changes response shapes,
you fix it in one place.

Endpoints used (see https://developer.schwab.com/products/trader-api--individual):
  - /marketdata/v1/chains          -> option chains (has delta, IV, greeks)
  - /marketdata/v1/pricehistory    -> daily bars for trend/volume calcs
  - /marketdata/v1/movers          -> optional, for a "what's active today" universe boost
  - /marketdata/v1/instruments     -> fundamentals lookup (limited; earnings dates
                                       are NOT reliably available from Schwab's API —
                                       see note in get_earnings_date below)
"""
import logging
from datetime import date, datetime
import numpy as np

from iv_history import IVHistoryStore
from earnings import EarningsCalendar

log = logging.getLogger("market_data")


class MarketDataAdapter:
    def __init__(self, client, cfg: dict = None, iv_store: IVHistoryStore = None,
                 earnings_calendar: EarningsCalendar = None, external_iv_store=None):
        self.client = client
        self._chain_cache = {}
        self._history_cache = {}
        self._ohlc_cache = {}
        self._avg_volume_cache = {}
        self.cfg = cfg or {}
        # Both are optional so this class still works standalone / in tests;
        # scanner.py wires real instances of both in.
        self.iv_store = iv_store
        self.earnings_calendar = earnings_calendar
        # optionstrategist_iv.ExternalIVStore — real-history IV percentile,
        # checked before iv_store/realized-vol proxy. Optional; None is fine.
        self.external_iv_store = external_iv_store

    # ---------- option chains ----------

    def get_option_chain(self, symbol: str, dte_min: int, dte_max: int, strike_count: int = 100) -> dict:
        cache_key = (symbol, dte_min, dte_max, strike_count)
        if cache_key in self._chain_cache:
            return self._chain_cache[cache_key]

        resp = self.client.option_chains(
            symbol=symbol,
            contractType="ALL",
            strikeCount=strike_count,
            fromDate=None,
            toDate=None,
        )
        resp.raise_for_status()
        data = resp.json()

        result = {}
        for exp_map_key, calls_by_strike in data.get("callExpDateMap", {}).items():
            expiration = exp_map_key.split(":")[0]
            days = (datetime.strptime(expiration, "%Y-%m-%d").date() - date.today()).days
            if not (dte_min <= days <= dte_max):
                continue
            result.setdefault(expiration, {"calls": [], "puts": []})
            for strike_str, contracts in calls_by_strike.items():
                for c in contracts:
                    result[expiration]["calls"].append(self._parse_contract(c))

        for exp_map_key, puts_by_strike in data.get("putExpDateMap", {}).items():
            expiration = exp_map_key.split(":")[0]
            if expiration not in result:
                continue
            for strike_str, contracts in puts_by_strike.items():
                for p in contracts:
                    result[expiration]["puts"].append(self._parse_contract(p))

        self._chain_cache[cache_key] = result
        return result

    @staticmethod
    def _parse_contract(c: dict) -> dict:
        return {
            "strike": c.get("strikePrice"),
            "bid": c.get("bid", 0.0),
            "ask": c.get("ask", 0.0),
            "delta": c.get("delta", 0.0),
            "iv": c.get("volatility", 0.0),
            "symbol": c.get("symbol"),
            "volume": c.get("totalVolume", 0),
            "open_interest": c.get("openInterest", 0),
        }

    # ---------- IV rank ----------

    def get_iv_rank(self, symbol: str) -> float:
        """
        Fallback chain, best data first:
          1. optionstrategist_iv.ExternalIVStore — McMillan's own IV
             percentile, backed by hundreds of days of real history,
             refreshed weekly. Used if wired in and the latest row is
             fresh (see ExternalIVStore.percentile's max_age_days).
          2. iv_history.py's own iv_rank() — your own recorded ATM IV
             snapshots, once MIN_OBSERVATIONS_FOR_RANK is met.
          3. Realized-volatility-percentile proxy (always available).
        """
        if self.external_iv_store:
            ext_rank = self.external_iv_store.percentile(symbol)
            if ext_rank is not None:
                return ext_rank

        if self.iv_store:
            real_rank = self.iv_store.iv_rank(symbol)
            if real_rank is not None:
                return real_rank

        closes = self._get_daily_closes(symbol, days=252)
        if len(closes) < 30:
            return 50.0
        returns = np.diff(np.log(closes))
        rolling_vol = np.array([
            np.std(returns[max(0, i - 20):i]) * np.sqrt(252)
            for i in range(20, len(returns))
        ])
        current = rolling_vol[-1]
        rank = 100 * (np.sum(rolling_vol <= current) / len(rolling_vol))
        return float(rank)

    # ---------- technical indicators ----------

    def get_rsi(self, symbol: str, period: int = 14) -> float:
        closes = self._get_daily_closes(symbol, days=period + 5)
        if len(closes) < period + 1:
            return 50.0
        # compute RSI
        deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        gains = [d if d > 0 else 0 for d in deltas[-period:]]
        losses = [-d if d < 0 else 0 for d in deltas[-period:]]
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return rsi

    def get_adx(self, symbol: str, period: int = 14) -> float:
        """ADX calculation using existing trend strength proxy, as full OHLC
        integration would require larger refactoring. Returns scaled trend strength
        in 0-100 ADX-like range to maintain interface consistency."""
        # Use existing trend strength as momentum proxy since full ADX requires OHLC
        raw_strength = self.get_trend_strength(symbol, lookback_days=period)
        # Scale [-1, 1] range to 0-100 to match ADX's typical 0-100 range
        return float(np.clip(abs(raw_strength) * 100, 0, 100))

    # ---------- trend / volume ----------

    def get_trend(self, symbol: str) -> str:
        strength = self.get_trend_strength(symbol, lookback_days=20)
        if strength > 0.15:
            return "up"
        if strength < -0.15:
            return "down"
        return "neutral"

    def get_trend_strength(self, symbol: str, lookback_days: int) -> float:
        closes = self._get_daily_closes(symbol, days=lookback_days + 5)
        if len(closes) < lookback_days:
            return 0.0
        window = closes[-lookback_days:]
        pct_change = (window[-1] - window[0]) / window[0]
        return float(np.clip(pct_change * 5, -1, 1))  # scale into [-1, 1]

    def get_volume_ratio(self, symbol: str) -> float:
        resp = self.client.price_history(symbol, periodType="month", period=1, frequencyType="daily")
        resp.raise_for_status()
        candles = resp.json().get("candles", [])
        if len(candles) < 21:
            return 1.0
        volumes = [c["volume"] for c in candles]
        today_vol = volumes[-1]
        avg_vol = np.mean(volumes[-21:-1])
        return float(today_vol / avg_vol) if avg_vol else 1.0

    def get_avg_volume(self, symbol: str, lookback: int = 20) -> float:
        """Average daily UNDERLYING share volume over `lookback` days — used
        as a liquidity prefilter (config: universe.min_avg_option_volume).
        NOTE on naming: this is underlying stock volume, not option-contract
        volume. Schwab's chain response does carry a per-contract totalVolume
        field, but checking that would mean pulling a full chain for every
        candidate symbol just to decide whether to scan it — chicken-and-egg
        for a universe-level prefilter. Underlying volume is a reasonable,
        cheap proxy: illiquid stocks reliably have illiquid options chains,
        the converse isn't guaranteed but is rare enough to not be worth the
        extra API calls here. Same convention backtest_engine.py's
        illiquid_volume_threshold already uses."""
        cache_key = (symbol, lookback)
        if cache_key in self._avg_volume_cache:
            return self._avg_volume_cache[cache_key]
        resp = self.client.price_history(symbol, periodType="month", period=1, frequencyType="daily")
        resp.raise_for_status()
        candles = resp.json().get("candles", [])
        vols = [c["volume"] for c in candles[-lookback:] if c.get("volume") is not None]
        avg = float(sum(vols) / len(vols)) if vols else 0.0
        self._avg_volume_cache[cache_key] = avg
        return avg

    def leg_spread_pct(self, occ_symbol: str) -> float | None:
        """Bid/ask spread as a fraction of mid for one OCC option symbol, or
        None if it can't be found in a freshly pulled chain. Mirrors
        BacktestMarketData.leg_spread_pct so live and backtest use the same
        liquidity gate. Used by risk_manager's hard spread gate (see
        risk_manager.py patch below)."""
        from strategies.base import parse_occ_symbol
        parsed = parse_occ_symbol(occ_symbol)
        underlying = parsed["underlying"]
        chain = self.get_option_chain(underlying, dte_min=0, dte_max=400)
        target_exp = parsed["expiration"]
        option_type = parsed["option_type"]
        target_strike = parsed["strike"]
        sides = chain.get(target_exp)
        if not sides:
            return None
        contracts = sides.get("calls", []) if option_type == "call" else sides.get("puts", [])
        for c in contracts:
            if abs(c["strike"] - target_strike) < 0.001:
                bid, ask = c.get("bid"), c.get("ask")
                if not bid or not ask:
                    return None
                mid = (bid + ask) / 2
                return (ask - bid) / mid if mid else None
        return None

    def get_put_call_ratio(self, symbol: str = "SPX", dte_min: int = 10, dte_max: int = 60) -> float | None:
        """Compute put/call ratio for a symbol (default SPX) across a DTE range.
        Uses open interest or volume? Let's use volume (more reactive)."""
        chain = self.get_option_chain(symbol, dte_min, dte_max, strike_count=200)
        total_put_volume = 0
        total_call_volume = 0
        for sides in chain.values():
            for put in sides.get("puts", []):
                # Wait, does Schwab's API provide volume in the option chain response?
                # Let's check _parse_contract — right now we don't parse it. Let's update _parse_contract first!
                total_put_volume += put.get("volume", 0)
            for call in sides.get("calls", []):
                total_call_volume += call.get("volume", 0)
        if total_call_volume > 0:
            return total_put_volume / total_call_volume
        return None

    def get_last_price(self, symbol: str) -> float:
        resp = self.client.quotes([symbol])
        resp.raise_for_status()
        return resp.json()[symbol]["quote"]["lastPrice"]

    def _get_daily_closes(self, symbol: str, days: int) -> list[float]:
        cache_key = (symbol, days)
        if cache_key in self._history_cache:
            return self._history_cache[cache_key]
        resp = self.client.price_history(
            symbol, periodType="year", period=1, frequencyType="daily"
        )
        resp.raise_for_status()
        candles = resp.json().get("candles", [])
        closes = [c["close"] for c in candles][-days:]
        self._history_cache[cache_key] = closes
        return closes

    # ---------- earnings ----------

    def get_earnings_date(self, symbol: str):
        """
        Backed by Finnhub's earnings calendar (see earnings.py). Returns
        None (disabling earnings_vol for that symbol) if no calendar was
        wired in or Finnhub has nothing scheduled within the lookahead window.
        """
        if not self.earnings_calendar:
            return None
        lookahead = self.cfg.get("strategies", {}).get("earnings_vol", {}).get("days_before_earnings", 30)
        return self.earnings_calendar.get_earnings_date(symbol, lookahead_days=lookahead)

    def get_historical_earnings_move(self, symbol: str):
        """
        Computes the average absolute 1-day close-to-close % move following
        each of the symbol's past earnings dates (from Finnhub), using price
        history we already pull from Schwab. Needs a few quarters of history
        to be meaningful — returns None if there isn't enough.
        """
        if not self.earnings_calendar:
            return None

        past_dates = self.earnings_calendar.get_historical_earnings_dates(symbol)
        if len(past_dates) < 4:
            return None

        resp = self.client.price_history(symbol, periodType="year", period=3, frequencyType="daily")
        resp.raise_for_status()
        candles = resp.json().get("candles", [])
        if not candles:
            return None

        by_date = {}
        for c in candles:
            d = datetime.fromtimestamp(c["datetime"] / 1000).date()
            by_date[d] = c["close"]
        sorted_dates = sorted(by_date)

        moves = []
        for earnings_day in past_dates:
            after = next((d for d in sorted_dates if d >= earnings_day), None)
            if not after:
                continue
            idx = sorted_dates.index(after)
            if idx == 0:
                continue
            before_close = by_date[sorted_dates[idx - 1]]
            after_close = by_date[after]
            if before_close:
                moves.append(abs(after_close - before_close) / before_close)

        if len(moves) < 4:
            return None
        return float(np.mean(moves))

    def get_historical_earnings_surprise(self, symbol: str):
        """
        Returns average absolute % surprise (|(actual-estimate)/estimate|)
        from past earnings, or None if not enough data.
        """
        if not self.earnings_calendar:
            return None

        earnings_data = self.earnings_calendar.get_historical_earnings_data(symbol)
        if len(earnings_data) < 4:
            return None

        surprises = []
        for _, actual, estimate in earnings_data:
            if actual is None or estimate is None or estimate == 0:
                continue
            # Calculate % surprise
            surprise_pct = abs((actual - estimate) / estimate)
            surprises.append(surprise_pct)

        if len(surprises) < 4:
            return None
        return float(np.mean(surprises))
