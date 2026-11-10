"""
Backtesting engine.

Runs your ACTUAL strategy modules (strategies/credit_spreads.py,
strategies/directional.py, strategies/leaps.py — see SUPPORTED_STRATEGIES
below) unmodified,
day by day, over historical data, so a strategy code change gets validated
against history before it ever reaches a live scan. Nothing in
strategies/*.py needs to change to be backtested — that was a hard
requirement, since testing a copy of the logic instead of the real thing
defeats the point.

WHAT THIS DOES NOT DO
---------------------
- It is NOT a claim that backtested performance predicts live performance.
  Overfitting to history is easy and this engine does nothing to stop you
  from doing it to yourself — vary your parameters on an out-of-sample
  window before you trust anything here.
- earnings_vol.py and wheel.py are NOT included in SUPPORTED_STRATEGIES by
  default. earnings_vol needs a real historical earnings-date calendar,
  which isn't available (see BacktestMarketData.get_earnings_date). wheel
  needs a historical account-positions state for its covered-call half,
  which a backtest of a fresh account doesn't have. Both would silently
  produce zero candidates if you tried anyway — better to be explicit
  they're out of scope for now.
- LEAPS (strategies/leaps.py) IS supported, but in synthetic mode its P&L
  is a LOGIC CHECK ONLY, not a profit estimate. synthetic_chain prices with
  q=0 (no dividends) and a single flat IV with no skew or term structure,
  while a deep-ITM long-dated call's extrinsic value is dominated by carry
  and dividends — exactly the two inputs the synthetic model omits. Do not
  tune LEAPS parameters on synthetic numbers. It also needs ~200 trading
  days of warm-up before its 200-DMA filter has anything to read.
- Option pricing is either (a) real recorded snapshots from
  timeseries.TimeSeriesStore if you've been running record_option_chain_snapshot
  on your live scan cycle, or (b) synthetic Black-Scholes pricing
  (bs_pricing.py) as a fallback. Synthetic mode has no skew, no term
  structure, and no liquidity effects — see bs_pricing.py's docstring.
  Treat synthetic-mode results as a strategy-logic check, not a P&L
  estimate.
- There is no human approval step — every candidate above
  approval_score_threshold is auto-executed. That's deliberate (a backtest
  can't simulate your judgment) but it does mean live results will differ
  from backtest results by however often you'd have rejected a real alert.
- Early assignment on short legs is a threshold heuristic, not a real
  model — see BacktestEngine._check_early_assignment's docstring. It
  force-exits a short leg that's gone deep ITM before expiration (mirrors
  paper_broker.py's check_early_assignment_risk), which removes the
  "riding a deep-ITM short to expiration for free" bias, but it doesn't
  model dividend-driven early-exercise timing on short calls specifically,
  and it never creates the resulting stock position a real assignment
  would (that's a covered-call/wheel-shaped problem, and wheel.py isn't
  backtested here anyway — see above).

USAGE
-----
    from timeseries import TimeSeriesStore
    from backtest_engine import BacktestEngine
    import yaml

    cfg = yaml.safe_load(open("config.yaml"))
    ts_store = TimeSeriesStore("./bot.db")
    # Load history first — see timeseries.TimeSeriesStore.import_bars_csv()
    # or record_bars() if you already have daily OHLCV from somewhere.

    engine = BacktestEngine(cfg, ts_store)
    param_grid = {
    "strategies.credit_spreads.min_iv_rank": [40, 50, 60],
    "strategies.directional.volume_multiple_trigger": [1.5, 2.0, 2.5],
}
wf_result = run_walk_forward(
    engine,
    symbols=symbols,
    start_date="2024-01-01",
    end_date="2026-07-01",
    train_window_days=180,
    test_window_days=90,
    step_days=30,
    param_grid=param_grid,
    tune_on_train=True,  # will search per window; set False to just use static config
)
print(wf_result.summary())
    result = engine.run(symbols=["AAPL", "MSFT", "NVDA"],
                         start_date="2024-01-01", end_date="2024-12-31")
    print(result.summary())
    result.trades_to_csv("./backtest_trades.csv")
"""
# After engine = BacktestEngine(...)


import logging
import math
import random
import statistics
from datetime import date, datetime, timedelta

from bs_pricing import synthetic_chain, bs_price_and_delta, realized_vol_estimate
from strategies import credit_spreads, directional, leaps
from strategies.base import parse_occ_symbol, set_reference_date
from sentiment import HistoricalRegimeEngine

log = logging.getLogger("backtest")

SUPPORTED_STRATEGIES = {
    "credit_spreads": credit_spreads.scan,
    "directional": directional.scan,
    "leaps": leaps.scan,
}


class BacktestMarketData:
    """Duck-types MarketDataAdapter's interface, backed by historical bars
    + (real snapshot or synthetic) option pricing instead of live API
    calls. `set_as_of()` must be called before each day's scan — every
    method here only ever looks at data up to and including that date, so
    strategies can't accidentally see the future."""

    def __init__(self, ts_store, cfg, chain_source="synthetic", slippage_pct: float = 0.005, illiquid_volume_threshold: float = 500000):
        self.ts_store = ts_store
        self.cfg = cfg
        self.chain_source = chain_source  # "synthetic" or "recorded"
        self._as_of: date = None
        self._bars_cache = {}  # symbol -> full bar list (fetched once, sliced per day)
        self._chain_cache = {}  # (symbol, dte_min, dte_max, as_of) -> chain
        self.slippage_pct = slippage_pct  # 0.5% slippage by default
        self.illiquid_volume_threshold = illiquid_volume_threshold

    def get_avg_volume(self, symbol: str, lookback: int = 20) -> float:
        bars = self._bars(symbol)
        vols = [b["volume"] for b in bars[-lookback:] if b["volume"] is not None]
        return sum(vols) / len(vols) if vols else 0.0

    def set_as_of(self, as_of: date):
        self._as_of = as_of
        self._chain_cache = {}  # Clear chain cache when date changes
        # strategies.base.dte() reads the wall clock by default; point it at
        # the simulated day so a replayed chain's DTE is measured against
        # `as_of`, not the real today (see that module's comment).
        set_reference_date(as_of)

    def _bars(self, symbol: str) -> list[dict]:
        if symbol not in self._bars_cache:
            self._bars_cache[symbol] = self.ts_store.get_bars(symbol, "1d")
        all_bars = self._bars_cache[symbol]
        cutoff = self._as_of.isoformat()
        return [b for b in all_bars if b["ts"] <= cutoff]

    def _closes(self, symbol: str) -> list[float]:
        return [b["close"] for b in self._bars(symbol) if b["close"] is not None]

    def get_last_price(self, symbol: str) -> float:
        closes = self._closes(symbol)
        return closes[-1] if closes else None

    def get_trend_strength(self, symbol: str, lookback_days: int) -> float:
        closes = self._closes(symbol)
        if len(closes) < lookback_days:
            return 0.0
        window = closes[-lookback_days:]
        if not window[0]:
            return 0.0
        pct_change = (window[-1] - window[0]) / window[0]
        return max(-1.0, min(1.0, pct_change * 5))

    def get_trend(self, symbol: str) -> str:
        strength = self.get_trend_strength(symbol, lookback_days=20)
        if strength > 0.15:
            return "up"
        if strength < -0.15:
            return "down"
        return "neutral"

    def get_volume_ratio(self, symbol: str) -> float:
        bars = self._bars(symbol)
        if len(bars) < 21:
            return 1.0
        volumes = [b["volume"] for b in bars[-21:] if b["volume"] is not None]
        if len(volumes) < 21:
            return 1.0
        today_vol, avg_vol = volumes[-1], sum(volumes[:-1]) / (len(volumes) - 1)
        return today_vol / avg_vol if avg_vol else 1.0

    def get_iv_rank(self, symbol: str) -> float:
        """Same realized-vol-percentile fallback market_data.py uses when
        no recorded IV history exists — kept consistent so backtest
        behavior matches what the live bot does for a symbol without IV
        history yet."""
        closes = self._closes(symbol)
        if len(closes) < 50:
            return 50.0
        rolling = []
        for i in range(20, len(closes)):
            window = closes[max(0, i - 20):i]
            if len(window) < 2:
                continue
            rolling.append(realized_vol_estimate(window, window=len(window) - 1))
        if not rolling:
            return 50.0
        current = rolling[-1]
        return 100 * (sum(1 for v in rolling if v <= current) / len(rolling))

    def get_earnings_date(self, symbol: str):
        # No historical earnings calendar available offline — see module
        # docstring. earnings_vol.py is intentionally excluded from
        # SUPPORTED_STRATEGIES because of this.
        return None

    def get_historical_earnings_move(self, symbol: str):
        return None

    def get_rsi(self, symbol: str, period: int = 14) -> float:
        closes = self._closes(symbol)
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
        return float(abs(raw_strength) * 100)

    def get_sma(self, symbol: str, n: int) -> float | None:
        """Simple moving average of the last `n` daily closes up to (and
        including) `as_of`, or None if there isn't enough history yet —
        mirrors MarketDataAdapter.get_sma so LEAPS' 200-DMA / 50-DMA
        extension filters behave identically live and in backtest. Only ever
        reads bars with ts <= as_of, so there's no lookahead."""
        if not n or n <= 0:
            return None
        closes = self._closes(symbol)
        if len(closes) < n:
            return None
        return sum(closes[-n:]) / n

    # ---------- option chain ----------

    def get_option_chain(self, symbol: str, dte_min: int, dte_max: int) -> dict:
        cache_key = (symbol, dte_min, dte_max, self._as_of.isoformat())
        if cache_key in self._chain_cache:
            return self._chain_cache[cache_key]
        
        if self.chain_source == "recorded":
            snap = self.ts_store.get_option_chain_snapshot(symbol, as_of_ts=self._as_of.isoformat())
            if snap:
                # filter to requested DTE window
                filtered = {}
                for exp, sides in snap.items():
                    dte_days = (datetime.strptime(exp, "%Y-%m-%d").date() - self._as_of).days
                    if dte_min <= dte_days <= dte_max:
                        filtered[exp] = sides
                if filtered:
                    self._chain_cache[cache_key] = filtered
                    return filtered
            # fall through to synthetic if no recorded snapshot covers this date/window

        spot = self.get_last_price(symbol)
        if not spot:
            self._chain_cache[cache_key] = {}
            return {}
        closes = self._closes(symbol)
        iv = realized_vol_estimate(closes) if len(closes) > 21 else 0.30

        expirations = []
        if dte_min > 90:
            # Long-dated (LEAPS) windows: monthly-spaced expirations aligned
            # to 30-day boundaries from as_of. Anchoring the grid (rather
            # than starting at dte_min like the weekly case) is what lets
            # leg_spread_pct/_get_leg_price's leg_dte ± 7 window round-trip
            # back to exactly this expiration, so a LEAPS leg is actually
            # found instead of falling through to the Black-Scholes
            # fallback. Weekly spacing across 365-730 DTE would also build
            # ~50 expirations x ~40 strikes x 2 sides for no benefit.
            step = 30
            d = ((dte_min + step - 1) // step) * step
            while d <= dte_max:
                expirations.append(self._as_of + timedelta(days=d))
                d += step
        else:
            d = dte_min
            while d <= dte_max:
                expirations.append(self._as_of + timedelta(days=d))
                d += 7  # weekly-spaced synthetic expirations across the window
        if not expirations:
            expirations = [self._as_of + timedelta(days=(dte_min + dte_max) // 2)]

        chain = synthetic_chain(spot, self._as_of, expirations, iv)
        self._chain_cache[cache_key] = chain
        return chain    


    def _leg_dte_window(self, parsed: dict) -> tuple[int, int]:
        """DTE window (±7 days) around a leg's own expiration, measured from
        `as_of`. Mirrors MarketDataAdapter.leg_spread_pct's window. The old
        hardcoded 21-60 window meant a LEAPS leg was never found in the
        chain and fell through to the Black-Scholes fallback — and, in the
        spread gate, silently 'passed' instead of being checked."""
        exp = datetime.strptime(parsed["expiration"], "%Y-%m-%d").date()
        leg_dte = (exp - self._as_of).days
        return max(0, leg_dte - 7), leg_dte + 7

    def leg_spread_pct(self, leg: dict, as_of: date):
        """Bid/ask spread as a fraction of mid for one leg's contract, or
        None if the contract can't be found in the chain that day.
        Used as a pre-trade liquidity gate (Priority 7): a wide quoted
        spread means the fill you'd actually get diverges a lot from mid,
        so candidates with any leg wider than a configurable threshold get
        skipped rather than traded through unrealistic assumed slippage."""
        parsed = parse_occ_symbol(leg["occ_symbol"]) if leg.get("occ_symbol") else leg
        symbol = parsed.get("underlying") or parsed.get("symbol")
        dte_min, dte_max = self._leg_dte_window(parsed)
        chain = self.get_option_chain(symbol, dte_min, dte_max)
        target_exp = parsed["expiration"]
        option_type = parsed["option_type"]
        target_strike = parsed["strike"]
        for exp, sides in chain.items():
            if exp != target_exp:
                continue
            contracts = sides.get("calls", []) if option_type == "call" else sides.get("puts", [])
            for c in contracts:
                if abs(c["strike"] - target_strike) < 0.001:
                    bid, ask = c.get("bid"), c.get("ask")
                    if not bid or not ask:
                        return None
                    mid = (bid + ask) / 2
                    return (ask - bid) / mid if mid else None
        return None

    def _get_leg_price(self, parsed, as_of: date, side: str, aggressive: bool = False) -> float:
        """Get the actual fill price for a leg: bid for sells, ask for buys with tiered slippage based on volume.
        aggressive=True widens the slippage further, simulating a market/
        aggressive-limit order chasing a fast-moving name instead of a
        passive resting limit — see calculate_fill_price's docstring and
        AUDIT_FINDINGS.md #9."""
        symbol = parsed["underlying"] if "underlying" in parsed else parsed.get("symbol")
        dte_min, dte_max = self._leg_dte_window(parsed)  # window around THIS leg's expiry (LEAPS-safe)
        chain = self.get_option_chain(symbol, dte_min, dte_max)
        
        # Calculate tiered slippage
        avg_vol = self.get_avg_volume(symbol)
        liquidity_mult = 5.0 if avg_vol < self.illiquid_volume_threshold else 1.0
        effective_slippage = self.slippage_pct * liquidity_mult
        if aggressive:
            # Chasing the move costs more than sitting at a passive limit —
            # roughly doubling slippage is a simple, defensible proxy
            # without needing tick-level order book data.
            effective_slippage *= 2.0
        
        # Find the correct expiration and strike
        target_exp = parsed["expiration"]
        option_type = parsed["option_type"]
        target_strike = parsed["strike"]
        
        for exp, sides in chain.items():
            if exp != target_exp:
                continue
            contracts = sides.get("calls", []) if option_type == "call" else sides.get("puts", [])
            for c in contracts:
                if abs(c["strike"] - target_strike) < 0.001:
                    # Get the right price: bid for sell, ask for buy
                    base_price = c["bid"] if side == "sell" else c["ask"]
                    # Apply effective slippage
                    slippage = base_price * effective_slippage
                    if side == "sell":
                        return base_price - slippage  # Sell at slightly worse price
                    else:
                        return base_price + slippage  # Buy at slightly worse price
        
        # Fallback if we can't find the contract in the chain
        spot = self.get_last_price(symbol)
        exp = datetime.strptime(target_exp, "%Y-%m-%d").date()
        dte_days = (exp - as_of).days
        closes = self._closes(symbol)
        iv = realized_vol_estimate(closes) if len(closes) > 21 else 0.30
        price, _ = bs_price_and_delta(spot, target_strike, dte_days, iv, option_type)
        
        # Apply effective slippage to fallback price too
        slippage = price * effective_slippage
        if side == "sell":
            return price - slippage
        else:
            return price + slippage

    def calculate_fill_price(self, legs: list[dict], as_of: date, is_credit: bool = True, aggressive: bool = False) -> float:
        """Calculate net fill price for a multi-leg position (entry or exit).
        For credit trades: returns positive net credit
        For debit trades: returns positive net debit
        aggressive: pass True for momentum/chase-style entries (e.g.
        directional.py) to widen slippage, modeling a market/aggressive-
        limit order instead of a passive resting limit at/near mid."""
        total = 0.0
        for leg in legs:
            parsed = parse_occ_symbol(leg["occ_symbol"]) if leg.get("occ_symbol") else leg
            side = "sell" if leg["instruction"].startswith("SELL") else "buy"
            leg_price = self._get_leg_price(parsed, as_of, side, aggressive=aggressive)
            # For credit spread: sell leg +, buy leg -
            if side == "sell":
                total += leg_price * leg.get("ratio", 1)
            else:
                total -= leg_price * leg.get("ratio", 1)
        return round(total, 4) if is_credit else round(-total, 4)

    def price_position(self, legs: list[dict], as_of: date) -> float:
        """Mark an open multi-leg position to market for exit-condition
        checks (stop-loss/take-profit), using mid price."""
        total = 0.0
        for leg in legs:
            parsed = parse_occ_symbol(leg["occ_symbol"]) if leg.get("occ_symbol") else leg
            symbol = parsed["underlying"] if "underlying" in parsed else leg.get("symbol")
            spot = self.get_last_price(symbol)
            if spot is None:
                return None
            exp = datetime.strptime(parsed["expiration"], "%Y-%m-%d").date()
            dte_days = (exp - as_of).days
            closes = self._closes(symbol)
            iv = realized_vol_estimate(closes) if len(closes) > 21 else 0.30
            price, _ = bs_price_and_delta(spot, parsed["strike"], dte_days, iv, parsed["option_type"])
            sign = 1 if leg["instruction"].startswith("SELL") else -1
            # value to the position holder: short leg owes back the price, long leg owns the price
            total += (-sign) * price * leg.get("ratio", 1)
        return round(total, 4)


def calculate_commissions(legs: list[dict], qty: int, commission_per_contract: float = 0.65) -> float:
    """Calculate total commissions for entry or exit: $0.65 per contract per leg"""
    num_legs = len(legs)
    total_contracts = qty * num_legs
    return round(total_contracts * commission_per_contract, 2)


def _pnl(is_credit: bool, entry_price: float, exit_price: float, qty: int, entry_comm: float, exit_comm: float) -> float:
    """Realized P&L per contract-multiplier-adjusted position, including commissions.
    Credit trade (sold premium): profit = (credit received - cost to close) * 100 * qty - entry_comm - exit_comm.
    Debit trade (bought premium): profit = (exit value - entry cost) * 100 * qty - entry_comm - exit_comm.
    """
    if is_credit:
        gross_pnl = (entry_price - abs(exit_price)) * qty * 100
    else:
        gross_pnl = (exit_price - entry_price) * qty * 100
    return round(gross_pnl - entry_comm - exit_comm, 2)


class Trade:
    __slots__ = ("symbol", "strategy", "entry_date", "expiration", "legs", "is_credit",
                 "entry_price", "entry_comm", "qty", "max_loss_per_contract", "exit_date", "exit_price",
                 "exit_comm", "exit_reason", "realized_pnl")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


class BacktestResult:
    def __init__(self, trades: list[Trade], equity_curve: list[tuple], underlying_bars: dict = None):
        self.trades = trades
        self.equity_curve = equity_curve  # [(date_iso, equity), ...]
        # {symbol: [{"ts","close",...}, ...]} for the underlyings actually
        # traded, so summary() can benchmark against buy-and-hold shares of
        # the same names (the comparison that matters for LEAPS).
        self.underlying_bars = underlying_bars or {}

    def summary(self, spy_bars: list[dict] = None) -> str:
        closed = [t for t in self.trades if t.realized_pnl is not None]
        if not closed:
            return "No closed trades in this window."
        wins = [t for t in closed if t.realized_pnl > 0]
        losses = [t for t in closed if t.realized_pnl < 0]
        total_pnl = sum(t.realized_pnl for t in closed)
        win_rate = len(wins) / len(closed) if closed else 0.0
        avg_win = statistics.mean([t.realized_pnl for t in wins]) if wins else 0.0
        avg_loss = statistics.mean([t.realized_pnl for t in losses]) if losses else 0.0
        expectancy = total_pnl / len(closed) if closed else 0.0

        equity_vals = [e for _, e in self.equity_curve]
        peak = equity_vals[0] if equity_vals else 0
        max_dd = 0.0
        for v in equity_vals:
            peak = max(peak, v)
            if peak:
                max_dd = max(max_dd, (peak - v) / peak)

        by_strategy = {}
        for t in closed:
            by_strategy.setdefault(t.strategy, []).append(t.realized_pnl)

        lines = [
            f"Trades: {len(closed)}  |  Win rate: {win_rate:.0%}  |  Total P&L: ${total_pnl:,.2f}",
            f"Avg win: ${avg_win:,.2f}  |  Avg loss: ${avg_loss:,.2f}  |  Expectancy/trade: ${expectancy:,.2f}",
            f"Max drawdown: {max_dd:.1%}",
            "",
            "By strategy:",
        ]
        for strat, pnls in by_strategy.items():
            wr = sum(1 for p in pnls if p > 0) / len(pnls) if pnls else 0.0
            lines.append(f"  {strat}: {len(pnls)} trades, {wr:.0%} win rate, ${sum(pnls):,.2f} total")

        # -- rolling Sharpe: are wins clustered, or consistent? (AUDIT_FINDINGS.md #7) --
        rolling = self.rolling_sharpe(window=20)
        if rolling:
            lines.append("")
            lines.append(
                f"Rolling 20-trade Sharpe (per-trade, not annualized): "
                f"latest {rolling[-1]:.2f}  (range {min(rolling):.2f} to {max(rolling):.2f} over the run)"
            )
        else:
            lines.append("")
            lines.append(f"Rolling 20-trade Sharpe: not enough closed trades yet (need 20, have {len(closed)}).")

        # -- monthly P&L attribution: seasonal/regime bias check --
        monthly = self.monthly_pnl()
        if monthly:
            lines.append("")
            lines.append("Monthly P&L attribution:")
            for month, pnl in monthly.items():
                sign = "+" if pnl >= 0 else ""
                lines.append(f"  {month}: {sign}${pnl:,.2f}")

        # -- SPY buy-and-hold benchmark, same window --
        if spy_bars:
            bench = self.benchmark_comparison(spy_bars)
            if bench and bench["strategy_return_pct"] is not None and bench["spy_return_pct"] is not None:
                lines.append("")
                verdict = "BEAT" if bench["beat_spy"] else "did NOT beat"
                lines.append(
                    f"vs SPY buy-and-hold: strategy {bench['strategy_return_pct']:+.1f}%  |  "
                    f"SPY {bench['spy_return_pct']:+.1f}%  |  SPY max drawdown {bench['spy_max_drawdown_pct']:.1f}%  |  "
                    f"strategy {verdict} SPY"
                )
            else:
                lines.append("")
                lines.append("vs SPY buy-and-hold: SPY bars didn't cover this window — skipped.")

        # -- buy-and-hold the SAME underlyings, equal weight (the benchmark
        # that actually matters for LEAPS: a stock-replacement LEAP only
        # wins if it delivers comparable upside for materially less capital
        # without a worse drawdown once extrinsic cost is paid) --
        if self.underlying_bars:
            shares = self.buy_shares_comparison(self.underlying_bars)
            if shares and shares["strategy_return_pct"] is not None:
                lines.append("")
                verdict = "BEAT" if shares["beat_shares"] else "did NOT beat"
                lines.append(
                    f"vs buying shares of the traded underlyings (equal-weight, "
                    f"{shares['n_symbols']} name(s)): strategy {shares['strategy_return_pct']:+.1f}%  |  "
                    f"shares {shares['shares_return_pct']:+.1f}%  |  shares max drawdown "
                    f"{shares['shares_max_drawdown_pct']:.1f}%  |  strategy {verdict} shares"
                )

        return "\n".join(lines)

    def rolling_sharpe(self, window: int = 20) -> list[float]:
        """Rolling Sharpe ratio over the trailing `window` CLOSED TRADES
        (a trade-sequence Sharpe, not annualized to a time basis — it
        answers "are wins consistent trade-to-trade, or lumpy/clustered",
        which a single aggregate Sharpe or max-drawdown number can hide).
        Returns one value per trade once at least `window` trades have
        closed, in trade-close order; empty list if there aren't enough
        closed trades yet."""
        closed = [t for t in self.trades if t.realized_pnl is not None]
        pnls = [t.realized_pnl for t in closed]
        if len(pnls) < window:
            return []
        out = []
        for i in range(window, len(pnls) + 1):
            chunk = pnls[i - window:i]
            mean = statistics.mean(chunk)
            stdev = statistics.pstdev(chunk)
            out.append(round(mean / stdev, 3) if stdev else 0.0)
        return out

    def monthly_pnl(self) -> dict:
        """Realized P&L grouped by exit month ('YYYY-MM'), sorted
        chronologically. Useful for spotting whether all the profit came
        from one or two calendar months (a seasonal fluke or a specific
        regime) rather than being broadly distributed across the backtest
        window (AUDIT_FINDINGS.md #7)."""
        closed = [t for t in self.trades if t.realized_pnl is not None and t.exit_date]
        by_month = {}
        for t in closed:
            month = t.exit_date[:7]
            by_month[month] = by_month.get(month, 0.0) + t.realized_pnl
        return dict(sorted(by_month.items()))

    def benchmark_comparison(self, spy_bars: list[dict]) -> dict | None:
        """Compares this run's equity curve against a SPY buy-and-hold over
        the same window, so "my bot made money" gets checked against "did
        it make more money, for similar or less drawdown, than just
        holding the index" (AUDIT_FINDINGS.md #7). If your bot doesn't
        clear this bar fairly consistently, the operational complexity
        and tail risk of running it may not be worth it over indexing.

        spy_bars: list of {"ts": iso_date, "close": float} — e.g. from
        TimeSeriesStore.get_bars("SPY", "1d", start_date, end_date).
        Returns None if spy_bars don't cover the equity curve's window at
        all (rather than silently comparing against a partial window)."""
        if not self.equity_curve or not spy_bars:
            return None
        spy_by_date = {b["ts"]: b["close"] for b in spy_bars if b.get("close")}
        if not spy_by_date:
            return None

        start_date, end_date = self.equity_curve[0][0], self.equity_curve[-1][0]

        if start_date not in spy_by_date:
            candidates = sorted(d for d in spy_by_date if d >= start_date)
            if not candidates:
                return None
            start_date = candidates[0]
        if end_date not in spy_by_date:
            candidates = sorted((d for d in spy_by_date if d <= end_date), reverse=True)
            if not candidates:
                return None
            end_date = candidates[0]

        spy_start, spy_end = spy_by_date[start_date], spy_by_date[end_date]
        spy_return = (spy_end - spy_start) / spy_start if spy_start else None

        equity_start, equity_end = self.equity_curve[0][1], self.equity_curve[-1][1]
        strategy_return = (equity_end - equity_start) / equity_start if equity_start else None

        spy_vals = [spy_by_date[d] for d in sorted(spy_by_date) if start_date <= d <= end_date]
        spy_peak = spy_vals[0] if spy_vals else 0
        spy_max_dd = 0.0
        for v in spy_vals:
            spy_peak = max(spy_peak, v)
            if spy_peak:
                spy_max_dd = max(spy_max_dd, (spy_peak - v) / spy_peak)

        return {
            "strategy_return_pct": round(strategy_return * 100, 2) if strategy_return is not None else None,
            "spy_return_pct": round(spy_return * 100, 2) if spy_return is not None else None,
            "spy_max_drawdown_pct": round(spy_max_dd * 100, 2),
            "beat_spy": bool(
                strategy_return is not None and spy_return is not None and strategy_return > spy_return
            ),
        }

    def buy_shares_comparison(self, underlying_bars: dict) -> dict | None:
        """Equal-weight buy-and-hold of the underlyings this run actually
        traded, over the same window as the equity curve. This is the
        benchmark section 8.6 of the LEAPS spec asks for: a stock-replacement
        LEAP is only worth the operational complexity (and the extrinsic
        cost of carry) if it roughly matches holding the shares for less
        capital, without a worse drawdown.

        underlying_bars: {symbol: [{"ts": iso_date, "close": float}, ...]}.
        Returns None if no symbol has usable closes over the window rather
        than comparing against a partial/empty set."""
        if not self.equity_curve or not underlying_bars:
            return None
        start_date, end_date = self.equity_curve[0][0], self.equity_curve[-1][0]

        returns, drawdowns = [], []
        for bars in underlying_bars.values():
            by_date = {b["ts"]: b["close"] for b in bars if b.get("close")}
            if not by_date:
                continue
            s = start_date if start_date in by_date else next(
                (d for d in sorted(by_date) if d >= start_date), None)
            e = end_date if end_date in by_date else next(
                (d for d in sorted(by_date, reverse=True) if d <= end_date), None)
            if not s or not e:
                continue
            vals = [by_date[d] for d in sorted(by_date) if s <= d <= e]
            if len(vals) < 2 or not vals[0]:
                continue
            returns.append((vals[-1] - vals[0]) / vals[0])
            peak, dd = vals[0], 0.0
            for v in vals:
                peak = max(peak, v)
                if peak:
                    dd = max(dd, (peak - v) / peak)
            drawdowns.append(dd)

        if not returns:
            return None

        shares_return = sum(returns) / len(returns)
        equity_start, equity_end = self.equity_curve[0][1], self.equity_curve[-1][1]
        strategy_return = (equity_end - equity_start) / equity_start if equity_start else None

        return {
            "strategy_return_pct": round(strategy_return * 100, 2) if strategy_return is not None else None,
            "shares_return_pct": round(shares_return * 100, 2),
            "shares_max_drawdown_pct": round((sum(drawdowns) / len(drawdowns)) * 100, 2),
            "beat_shares": bool(strategy_return is not None and strategy_return > shares_return),
            "n_symbols": len(returns),
        }

    def trades_to_csv(self, path: str):
        import csv
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["symbol", "strategy", "entry_date", "expiration", "is_credit",
                        "entry_price", "entry_comm", "qty", "max_loss_per_contract", "exit_date",
                        "exit_price", "exit_comm", "exit_reason", "realized_pnl"])
            for t in self.trades:
                w.writerow([t.symbol, t.strategy, t.entry_date, t.expiration, t.is_credit,
                            t.entry_price, t.entry_comm, t.qty, t.max_loss_per_contract, t.exit_date,
                            t.exit_price, t.exit_comm, t.exit_reason, t.realized_pnl])


class BacktestEngine:
    def __init__(self, cfg: dict, ts_store, strategies: tuple = ("credit_spreads", "directional"),
                 chain_source: str = "synthetic", approval_score_threshold: float = 0.55,
                 stop_loss_pct: float = None, take_profit_pct: float = None,
                 starting_cash: float = 100_000.0, slippage_pct: float = 0.005,
                 use_learning: bool = False, simulate_fill_probability: bool = True,
                 fill_probability_seed: int = 42, early_assignment_itm_threshold_pct: float = None,
                 use_sentiment: bool = False, max_spread_pct_skip: float = None,
                 fred_api_key: str = None):
        unknown = set(strategies) - set(SUPPORTED_STRATEGIES)
        if unknown:
            raise ValueError(
                f"Unsupported strategies for backtesting: {unknown}. "
                f"Supported: {list(SUPPORTED_STRATEGIES)} (see backtest_engine.py docstring for why "
                f"earnings_vol/wheel aren't included yet)."
            )
        self.cfg = cfg
        self.ts_store = ts_store
        self.strategy_names = strategies
        self.market_data = BacktestMarketData(ts_store, cfg, chain_source=chain_source, slippage_pct=slippage_pct)
        self.approval_score_threshold = approval_score_threshold
        self.stop_loss_pct = stop_loss_pct       # e.g. 2.0 = close at 2x the entry credit lost
        self.take_profit_pct = take_profit_pct   # e.g. 0.5 = close at 50% of max profit captured
        self.starting_cash = starting_cash
        self.risk_cfg = cfg.get("risk", {})
        self.commission_per_contract = cfg.get("paper", {}).get("commission_per_contract", 0.65)

        # -- early assignment risk (mirrors paper_broker.check_early_assignment_risk) --
        # Defaults from cfg.paper.early_assignment_itm_threshold_pct so a
        # backtest run and a paper-trading run use the same threshold
        # unless you deliberately override one. Only credit_spreads has a
        # short leg among the strategies this engine supports today
        # (directional is long-only, earnings_vol/wheel aren't backtested
        # — see module docstring), so in practice this only affects
        # credit_spreads.py trades. See _check_early_assignment's docstring.
        self.early_assignment_itm_threshold_pct = (
            early_assignment_itm_threshold_pct
            if early_assignment_itm_threshold_pct is not None
            else cfg.get("paper", {}).get("early_assignment_itm_threshold_pct", 0.05)
        )


        # -- item 9: order fill probability ("ghost fill" problem) --
        # See _estimate_fill_probability's docstring. A resting limit order
        # isn't guaranteed to fill just because a candidate scored above
        # threshold; this simulates that some candidates simply never get
        # hit, especially momentum names that are already running away
        # from you. Deterministic per run via fill_probability_seed so
        # backtests are reproducible.
        self.simulate_fill_probability = simulate_fill_probability
        self._fill_rng = random.Random(fill_probability_seed)

        # -- item 8: leak-free adaptive learning during backtest --
        # Off by default (matches learning.cfg being an opt-in live
        # feature). When on, candidates get learning.apply()'d each day
        # just like scanner.run_scan_cycle does, and the model backing
        # that scoring is retrained monthly using ONLY trades closed
        # strictly before the day being scanned — see
        # LearningEngine.maybe_retrain_historical's docstring and
        # AUDIT_FINDINGS.md #8 for why that matters.
        self.use_learning = use_learning
        self.learning = None
        self._learning_db = None
        self._learning_last_retrain_month = None
        if use_learning:
            from approval_manager import Db
            from learning import LearningEngine
            from trade_tracker import ensure_trade_outcome_columns
            self._learning_db = Db(":memory:")
            ensure_trade_outcome_columns(self._learning_db.conn)
            self.learning = LearningEngine(cfg, self._learning_db)

        # -- Priority 5: sentiment overlay in backtest --
        # See sentiment.HistoricalRegimeEngine's docstring for why this is
        # a separate, date-parameterized class rather than reusing the
        # live SentimentEngine directly (lookahead leak otherwise). News
        # sentiment is NOT reproduced here — no historical Finnhub feed
        # exists — so this only applies the macro regime multiplier half
        # of what sentiment.SentimentEngine.apply() does live.
        self.use_sentiment = use_sentiment
        self._regime_engine = None
        if use_sentiment:
            import os as _os
            key = fred_api_key or cfg.get("fred", {}).get("api_key") or _os.environ.get("FRED_API_KEY")
            self._regime_engine = HistoricalRegimeEngine(cfg, key)
            # preload() is called from run() once start/end dates are known.

        # -- Priority 7: pre-trade spread gate --
        # Skip a candidate entirely (don't even attempt a fill) if any leg's
        # quoted bid/ask spread exceeds this fraction of mid. Configurable
        # via config.yaml backtest.max_spread_pct_skip; defaults to 0.10
        # (10%) per the audit recommendation.
        self.max_spread_pct_skip = (
            max_spread_pct_skip
            if max_spread_pct_skip is not None
            else cfg.get("backtest", {}).get("max_spread_pct_skip", 0.10)
        )

    def _estimate_fill_probability(self, candidate: dict, day: date) -> float:
        """Approximates the odds a resting limit order at our assumed fill
        price actually gets hit that day, instead of assuming every
        candidate above threshold fills for free (the "ghost fill"
        problem — AUDIT_FINDINGS.md #9). Directional (momentum breakout)
        trades are the case that matters most: the strategy only fires on
        a trend+volume breakout in the first place, which means the stock
        is already moving away from a passive limit sitting near the
        theoretical price. Defined-risk/event strategies (credit_spreads,
        wheel, earnings_vol) aren't chasing a breakout, so they get a high
        baseline fill probability that only drops for illiquid names."""
        strategy = candidate.get("strategy")
        symbol = candidate["symbol"]

        if strategy == "directional":
            trend_strength = abs(self.market_data.get_trend_strength(symbol, 20))
            vol_ratio = self.market_data.get_volume_ratio(symbol)
            # Both terms roughly in [0,1]; more conviction + more volume
            # confirmation means the stock is running away from you faster.
            chase_intensity = min(1.0, trend_strength) * 0.6 + min(vol_ratio / 4.0, 1.0) * 0.4
            # Floors at 0.30 per the audit's own worked example (a strong,
            # volume-confirmed breakout dropping fill odds to ~30%).
            return max(0.30, 1.0 - chase_intensity * 0.7)

        avg_vol = self.market_data.get_avg_volume(symbol)
        if avg_vol < self.market_data.illiquid_volume_threshold:
            return 0.70
        return 0.95

    def _record_closed_trade_for_learning(self, t: "Trade"):
        """Feeds a closed backtest trade into the in-memory learning DB so
        LearningEngine.maybe_retrain_historical() has something to train
        on later in the run. No-op unless use_learning=True."""
        if not self.learning:
            return
        import json
        import uuid
        outcome = "WIN" if t.realized_pnl > 0 else ("LOSS" if t.realized_pnl < 0 else "BREAKEVEN")
        features = {
            "strategy": t.strategy,
            "score": None,  # not tracked on backtest Trade objects today
            "dte": None,
            "est_price": t.entry_price,
            "max_loss_per_contract": t.max_loss_per_contract,
        }
        self._learning_db.conn.execute(
            "INSERT INTO trades (id, approval_id, symbol, strategy, order_json, schwab_order_id, status, "
            "created_at, realized_pnl, outcome, closed_at, features_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), None, t.symbol, t.strategy, "{}", None, "EXECUTED",
             t.entry_date, t.realized_pnl, outcome, t.exit_date, json.dumps(features)),
        )
        self._learning_db.conn.commit()

    def run(self, symbols: list[str], start_date: str, end_date: str) -> BacktestResult:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = datetime.strptime(end_date, "%Y-%m-%d").date()

        trading_days = self._trading_days(symbols, start, end)
        if not trading_days:
            raise RuntimeError(
                f"No stored bars found for {symbols} in [{start_date}, {end_date}]. "
                f"Load history first via TimeSeriesStore.import_bars_csv() or record_bars()."
            )

        if self.use_sentiment and self._regime_engine is not None:
            self._regime_engine.preload(start, end)

        open_trades: list[Trade] = []
        closed_trades: list[Trade] = []
        cash = self.starting_cash
        equity_curve = []

        for day in trading_days:
            self.market_data.set_as_of(day)

            # (item 8) Retrain the learning model once per calendar month,
            # on the first trading day we reach in that month, using ONLY
            # trades closed strictly before `day` — see
            # LearningEngine.maybe_retrain_historical's docstring. This
            # runs BEFORE today's scan so today's candidates are scored by
            # a model that could only have known about the past.
            if self.learning:
                month_key = day.strftime("%Y-%m")
                if month_key != self._learning_last_retrain_month:
                    self.learning.maybe_retrain_historical(cutoff_date=day.isoformat())
                    self._learning_last_retrain_month = month_key

            # 1) check open positions for exit conditions
            still_open = []
            for t in open_trades:
                exit_reason, exit_price = self._check_exit(t, day)
                if exit_reason:
                    t.exit_date = day.isoformat()
                    t.exit_price = exit_price
                    t.exit_reason = exit_reason
                    t.exit_comm = calculate_commissions(t.legs, t.qty, self.commission_per_contract)
                    t.realized_pnl = _pnl(t.is_credit, t.entry_price, exit_price, t.qty, t.entry_comm, t.exit_comm)
                    cash += t.realized_pnl
                    closed_trades.append(t)
                    self._record_closed_trade_for_learning(t)
                else:
                    still_open.append(t)
            open_trades = still_open

            # 2) scan for new candidates using the REAL strategy modules
            day_candidates = []
            for name in self.strategy_names:
                scan_fn = SUPPORTED_STRATEGIES[name]
                try:
                    day_candidates += scan_fn(None, symbols, self.cfg, self.market_data)
                except Exception:
                    log.exception("Strategy %s raised during backtest on %s — skipping this day for it", name, day)

            # (item 8) Same reweighting step scanner.run_scan_cycle applies
            # live — pure score adjustment, never invents/removes candidates.
            if self.learning:
                day_candidates = [self.learning.apply(c) for c in day_candidates]

            # (item 5) Same reweighting step scanner.run_scan_cycle applies
            # live via sentiment.apply() — macro-regime multiplier only
            # (no news dampening; see HistoricalRegimeEngine's docstring).
            # Regime is computed strictly from data available on/before
            # `day`, so this can't leak future macro conditions into past
            # trades the way naively calling the live SentimentEngine would.
            if self.use_sentiment and self._regime_engine is not None:
                for c in day_candidates:
                    regime, mult = self._regime_engine.multiplier_for(c.get("strategy", ""), day)
                    base_score = c.get("score", 0.5)
                    c["score_before_sentiment"] = round(base_score, 3)
                    c["macro_regime"] = regime
                    c["regime_multiplier"] = round(mult, 3)
                    c["score"] = round(max(0.0, min(1.0, base_score * mult)), 3)

            day_candidates.sort(key=lambda c: c["score"], reverse=True)

            # 3) simplified risk gating (position caps + daily cap; no PDT/margin sim)
            new_today = 0
            symbols_open = {t.symbol for t in open_trades}
            for c in day_candidates:
                if c["score"] < self.approval_score_threshold:
                    continue
                if len(open_trades) >= self.risk_cfg.get("max_concurrent_positions", 8):
                    break
                if new_today >= self.risk_cfg.get("max_new_trades_per_day", 5):
                    break
                if c["symbol"] in symbols_open:
                    continue  # respects max_positions_per_symbol=1 default behavior

                # (item 9) "Ghost fill" check: a candidate clearing the score
                # threshold doesn't mean a resting limit order actually gets
                # hit that day, especially for momentum names already
                # running away from a passive limit. Skip the fill this
                # candidate/day if the simulated draw says it wouldn't fill
                # — the candidate isn't otherwise penalized (no cooldown),
                # since a real trader would just try again next cycle.
                if self.simulate_fill_probability:
                    fill_prob = self._estimate_fill_probability(c, day)
                    if self._fill_rng.random() > fill_prob:
                        continue

                # Same fix as risk_manager.RiskManager.size_candidate(): if
                # even 1 contract's max_loss_per_contract exceeds max_risk,
                # skip the trade rather than force-size to 1 and silently
                # blow past max_risk_per_trade_pct. The old max(1, ...)
                # here matched the same bug that existed in the live path —
                # fixing only one would make backtest sizing diverge from
                # what the live bot actually does.
                max_risk = self.starting_cash * self.risk_cfg.get("max_risk_per_trade_pct", 0.02)
                per_contract_risk = c.get("max_loss_per_contract") or 0
                if per_contract_risk <= 0:
                    qty = 1
                else:
                    qty = int(max_risk // per_contract_risk)
                    if qty < 1:
                        continue

                # Cap position size on illiquid names to 1 contract
                avg_vol = self.market_data.get_avg_volume(c["symbol"])
                if avg_vol < self.market_data.illiquid_volume_threshold:
                    qty = min(qty, 1)

                # (item 7) Pre-trade spread gate: skip the candidate outright
                # if any leg's quoted bid/ask spread is wider than
                # max_spread_pct_skip. A trade you'd realistically decline
                # to work at that quote shouldn't get an assumed fill just
                # because the score cleared threshold — same principle as
                # the fill-probability check above, but a market-quality
                # gate rather than a "would it get hit" gate.
                if self.max_spread_pct_skip:
                    too_wide = False
                    for leg in c["legs"]:
                        spread_pct = self.market_data.leg_spread_pct(leg, day)
                        if spread_pct is not None and spread_pct > self.max_spread_pct_skip:
                            too_wide = True
                            break
                    if too_wide:
                        continue

                # (item 9) Directional/momentum entries are modeled as
                # chasing the move (aggressive limit / market-order style,
                # wider slippage) rather than getting the nice passive-limit
                # price every other strategy assumes — see
                # BacktestMarketData._get_leg_price's aggressive= handling.
                aggressive = c["strategy"] == "directional"

                # Calculate realistic entry price and commissions
                entry_price = self.market_data.calculate_fill_price(
                    c["legs"], day, is_credit=c["is_credit"], aggressive=aggressive
                )
                entry_comm = calculate_commissions(c["legs"], qty, self.commission_per_contract)

                # Subtract commissions from cash immediately
                cash -= entry_comm

                trade = Trade(
                    symbol=c["symbol"], strategy=c["strategy"], entry_date=day.isoformat(),
                    expiration=c["expiration"], legs=c["legs"], is_credit=c["is_credit"],
                    entry_price=entry_price, entry_comm=entry_comm, qty=qty,
                    max_loss_per_contract=c.get("max_loss_per_contract"),
                )
                open_trades.append(trade)
                symbols_open.add(c["symbol"])
                new_today += 1

            unrealized = 0.0
            for t in open_trades:
                mtm = self.market_data.price_position(t.legs, day)
                if mtm is not None:
                    unrealized += _pnl(t.is_credit, t.entry_price, mtm, t.qty, 0, 0)  # No commissions for unrealized
            equity_curve.append((day.isoformat(), round(cash + unrealized, 2)))

        # settle anything still open at the end of the window at last-known intrinsic value
        for t in open_trades:
            exp_date = datetime.strptime(t.expiration, "%Y-%m-%d").date()
            settle_day = min(exp_date, trading_days[-1])
            self.market_data.set_as_of(settle_day)
            exit_price = self.market_data.calculate_fill_price(t.legs, settle_day, is_credit=t.is_credit) or t.entry_price
            t.exit_date = settle_day.isoformat()
            t.exit_price = exit_price
            t.exit_reason = "window_end"
            t.exit_comm = calculate_commissions(t.legs, t.qty, self.commission_per_contract)
            t.realized_pnl = _pnl(t.is_credit, t.entry_price, exit_price, t.qty, t.entry_comm, t.exit_comm)
            cash += t.realized_pnl
            closed_trades.append(t)
            self._record_closed_trade_for_learning(t)

        # Bars for the underlyings actually traded, so summary() can compare
        # against buy-and-hold shares of the same names (section 8.6).
        underlying_bars = {
            s: self.ts_store.get_bars(s, "1d", start.isoformat(), end.isoformat())
            for s in sorted({t.symbol for t in closed_trades})
        }
        # Hand the wall clock back to strategies.base.dte() — set_as_of()
        # repointed it at simulated days for the duration of this run.
        set_reference_date(None)
        return BacktestResult(closed_trades, equity_curve, underlying_bars)

    def _force_exit_dte(self, strategy: str):
        """strategies.<name>.exits.force_exit_dte, or None if the strategy
        doesn't configure one. Only LEAPS sets it today (120, from
        config.yaml) — LEAPS theta decays sharply inside ~6 months, so a
        stock-replacement LEAP is closed well before expiration rather than
        ridden to expiry like the other strategies' short-dated spreads."""
        return (self.cfg.get("strategies", {}).get(strategy, {})
                .get("exits", {}).get("force_exit_dte"))

    def _check_exit(self, t: Trade, day: date):
        exp_date = datetime.strptime(t.expiration, "%Y-%m-%d").date()
        if day >= exp_date:
            price = self.market_data.calculate_fill_price(t.legs, day, is_credit=t.is_credit)
            return "expiration", (price if price is not None else 0.0)

        # Time-decay exit (LEAPS): close once DTE <= the strategy's
        # force_exit_dte, checked before stop/take-profit the same way
        # exit_manager.check_triggers ranks its time_decay_exit first.
        force_dte = self._force_exit_dte(t.strategy)
        if force_dte is not None and (exp_date - day).days <= force_dte:
            price = self.market_data.calculate_fill_price(t.legs, day, is_credit=t.is_credit)
            return "force_exit_dte", (price if price is not None else 0.0)

        # Early-assignment risk: mirrors paper_broker.check_early_assignment_risk.
        # Checked before stop-loss/take-profit since a real assignment isn't
        # something you'd get to choose to ride past — it forces the exit
        # regardless of where your P&L stop levels sit.
        if self._check_early_assignment(t, day):
            price = self.market_data.calculate_fill_price(t.legs, day, is_credit=t.is_credit)
            return "early_assignment", (price if price is not None else 0.0)

        if self.stop_loss_pct or self.take_profit_pct:
            mtm = self.market_data.price_position(t.legs, day)
            if mtm is not None:
                if t.is_credit:
                    move = (t.entry_price - abs(mtm)) / t.entry_price if t.entry_price else 0
                    if self.take_profit_pct and move >= self.take_profit_pct:
                        exit_price = self.market_data.calculate_fill_price(t.legs, day, is_credit=t.is_credit)
                        return "take_profit", exit_price
                    if self.stop_loss_pct and move <= -self.stop_loss_pct:
                        exit_price = self.market_data.calculate_fill_price(t.legs, day, is_credit=t.is_credit)
                        return "stop_loss", exit_price
                else:
                    move = (mtm - t.entry_price) / t.entry_price if t.entry_price else 0
                    if self.take_profit_pct and move >= self.take_profit_pct:
                        exit_price = self.market_data.calculate_fill_price(t.legs, day, is_credit=t.is_credit)
                        return "take_profit", exit_price
                    if self.stop_loss_pct and move <= -self.stop_loss_pct:
                        exit_price = self.market_data.calculate_fill_price(t.legs, day, is_credit=t.is_credit)
                        return "stop_loss", exit_price
        return None, None

    def _check_early_assignment(self, t: Trade, day: date) -> bool:
        """Backtest counterpart to paper_broker.PaperBrokerClient.
        check_early_assignment_risk() — same rule, same threshold source
        (paper.early_assignment_itm_threshold_pct), applied per-leg so it
        works on multi-leg positions like credit_spreads.py's verticals:
        any SHORT leg that's gone ITM by >= threshold (as a % of strike)
        forces the exit, same as it would in paper mode. Long legs
        (BUY_TO_OPEN) are never assignable and are skipped. Returns False,
        not an exception, if the threshold is 0/None (feature off) or the
        underlying's price isn't available that day — this is a bias-
        reduction heuristic, not a hard requirement, so it fails open
        rather than blocking a backtest run over a data gap.
        """
        threshold = self.early_assignment_itm_threshold_pct
        if not threshold:
            return False
        for leg in t.legs:
            if not leg.get("instruction", "").startswith("SELL"):
                continue
            parsed = parse_occ_symbol(leg["occ_symbol"]) if leg.get("occ_symbol") else leg
            symbol = parsed.get("underlying") or parsed.get("symbol")
            strike = parsed.get("strike")
            option_type = parsed.get("option_type")
            if not symbol or not strike:
                continue
            spot = self.market_data.get_last_price(symbol)
            if not spot:
                continue
            if option_type == "put" and spot <= strike * (1 - threshold):
                return True
            if option_type == "call" and spot >= strike * (1 + threshold):
                return True
        return False

    def _trading_days(self, symbols, start, end) -> list[date]:
        all_dates = set()
        for symbol in symbols:
            bars = self.ts_store.get_bars(symbol, "1d", start.isoformat(), end.isoformat())
            all_dates.update(b["ts"] for b in bars)
        return sorted(datetime.strptime(d, "%Y-%m-%d").date() for d in all_dates)

# backtest_engine.py (additions)

class WalkForwardResult:
    """Holds results from walk-forward backtest."""

    def __init__(self, window_results: list, best_params: dict):
        self.window_results = window_results  # list of dicts per window
        self.best_params = best_params        # params chosen from in-sample tuning

    def summary(self) -> str:
        """Aggregate summary across all out-of-sample windows."""
        if not self.window_results:
            return "No windows processed."
        # Collect OOS metrics
        total_pnl = sum(w["total_pnl"] for w in self.window_results)
        win_rates = [w["win_rate"] for w in self.window_results if w.get("win_rate") is not None]
        avg_win_rate = sum(win_rates) / len(win_rates) if win_rates else 0
        max_dd = max(w.get("max_drawdown", 0) for w in self.window_results)
        n_trades = sum(w.get("n_trades", 0) for w in self.window_results)

        lines = [
            "*Walk-Forward Summary (Out-of-Sample)*",
            f"  Windows: {len(self.window_results)}",
            f"  Total P&L (OOS): ${total_pnl:,.2f}",
            f"  Avg Win Rate (OOS): {avg_win_rate:.1%}",
            f"  Max Drawdown (OOS): {max_dd:.1%}",
            f"  Total Trades (OOS): {n_trades}",
            "",
            "*Per-Window Details:*",
        ]
        for i, w in enumerate(self.window_results):
            lines.append(
                f"  Window {i+1}: {w['test_start']} → {w['test_end']} | "
                f"PnL ${w['total_pnl']:,.2f} | WR {w.get('win_rate', 0):.0%} | "
                f"Trades {w.get('n_trades', 0)}"
            )
        return "\n".join(lines)


def run_walk_forward(
    engine: "BacktestEngine",
    symbols: list[str],
    start_date: str,
    end_date: str,
    train_window_days: int = 180,   # 6 months
    test_window_days: int = 90,     # 3 months
    step_days: int = 30,            # roll forward monthly
    param_grid: dict = None,        # optional: e.g. {"min_iv_rank": [40,50,60], "volume_multiple_trigger": [1.5, 2.0]}
    tune_on_train: bool = False,    # if True, run a grid search on each train window; if False, just use current config
) -> WalkForwardResult:
    """
    Sliding-window walk-forward backtest.

    - train_window_days: length of in-sample period (e.g. 180 days)
    - test_window_days: length of out-of-sample period (e.g. 90 days)
    - step_days: roll window forward by this many days
    - param_grid: dict of parameter names → list of values to try during tuning.
                  Only used if tune_on_train is True.
    - tune_on_train: if True, runs grid search on train window and uses best params on test window.
                     If False, just uses the engine's current config (no tuning) and runs test window.
    """
    from datetime import datetime, timedelta

    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()

    window_results = []
    best_params_overall = {}

    current_start = start
    while current_start + timedelta(days=test_window_days) <= end:
        train_start = current_start
        train_end = train_start + timedelta(days=train_window_days)
        test_start = train_end
        test_end = min(test_start + timedelta(days=test_window_days), end)

        # Ensure we have enough data
        trading_days = engine._trading_days(symbols, train_start, test_end)  # helper to get all days
        if not trading_days:
            break

        if tune_on_train and param_grid:
            # --- TUNE on train window ---
            best_pnl = -float("inf")
            best_params = {}
            # Simple grid search: loop over param_grid combinations
            from itertools import product
            keys = list(param_grid.keys())
            vals = list(param_grid.values())
            for combination in product(*vals):
                params = dict(zip(keys, combination))
                # Apply params to engine.cfg (temporarily)
                old_cfg = engine.cfg
                # Deep copy or mutate carefully — simpler: we'll modify nested dicts
                for k, v in params.items():
                    # navigate nested keys (e.g. "strategies.credit_spreads.min_iv_rank")
                    parts = k.split(".")
                    target = engine.cfg
                    for part in parts[:-1]:
                        target = target.setdefault(part, {})
                    target[parts[-1]] = v

                # Run train-only backtest (no exits? we just want P&L signal)
                # Actually we just want to see which params do best on the train window
                train_result = engine.run(
                    symbols=symbols,
                    start_date=train_start.isoformat(),
                    end_date=train_end.isoformat(),
                )
                # Use total P&L as objective (could add Sharpe/win rate later)
                total_pnl = sum(t.realized_pnl for t in train_result.trades if t.realized_pnl is not None)
                if total_pnl > best_pnl:
                    best_pnl = total_pnl
                    best_params = params

            # Restore or apply best params to engine.cfg for the test run
            for k, v in best_params.items():
                parts = k.split(".")
                target = engine.cfg
                for part in parts[:-1]:
                    target = target.setdefault(part, {})
                target[parts[-1]] = v
            best_params_overall = best_params
        else:
            # No tuning — just use current config
            pass

        # --- RUN TEST on out-of-sample window ---
        test_result = engine.run(
            symbols=symbols,
            start_date=test_start.isoformat(),
            end_date=test_end.isoformat(),
        )
        closed_trades = [t for t in test_result.trades if t.realized_pnl is not None]
        total_pnl = sum(t.realized_pnl for t in closed_trades)
        wins = [t for t in closed_trades if t.realized_pnl > 0]
        win_rate = len(wins) / len(closed_trades) if closed_trades else 0

        # Record window result
        window_results.append({
            "train_start": train_start.isoformat(),
            "train_end": train_end.isoformat(),
            "test_start": test_start.isoformat(),
            "test_end": test_end.isoformat(),
            "total_pnl": total_pnl,
            "win_rate": win_rate,
            "n_trades": len(closed_trades),
            "max_drawdown": 0.0,  # Could compute from equity curve if you add it to test_result
            # Optional: store best_params if tuning was done
            "best_params": best_params_overall if tune_on_train else None,
        })

        # Advance window
        current_start += timedelta(days=step_days)

    return WalkForwardResult(window_results, best_params_overall)