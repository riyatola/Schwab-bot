# Backtesting — setup & usage

## Files
- `timeseries.py` — OHLCV + option chain snapshot storage (SQLite, shares your `bot.db`)
- `bs_pricing.py` — Black-Scholes synthetic option pricing (fallback when no real historical chain data exists)
- `backtest_engine.py` — runs your real `strategies/credit_spreads.py` and `strategies/directional.py` unmodified against historical data

Drop all three into your project root (same level as `scanner.py`).

## The honest limitation, up front

**Schwab's API has no historical option chain endpoint.** Neither does almost
any retail broker. You can't pull "what AAPL's option chain looked like six
months ago" from anywhere in this codebase, because that data doesn't exist
in Schwab's API surface. Two ways forward:

1. **Start recording now, backtest later.** Add one line to your scan cycle
   (`scanner.py`, inside `run_scan_cycle`) to capture real chains going
   forward:
   ```python
   from timeseries import TimeSeriesStore
   ts_store = TimeSeriesStore()  # once, alongside your other setup
   # inside the scan loop, per symbol you're already pulling a chain for:
   ts_store.record_option_chain_snapshot(symbol, chain)
   ```
   In 6–12 months you'll have real proprietary historical option data no
   synthetic model can match.

2. **Backtest now with synthetic (Black-Scholes) pricing.** This is what
   `chain_source="synthetic"` (the default) does — it derives a theoretical
   chain from historical stock price + a realized-vol estimate. It has no
   skew, no term structure, no bid/ask liquidity effects. **Use it to check
   whether your strategy logic behaves the way you think it does — entry
   timing, exit timing, position sizing, does `directional.py` actually
   only fire on real breakouts — not to estimate real historical profit.**
   A strategy that "backtests well" in synthetic mode has passed a logic
   check, not a profitability check.

## Getting historical price data in

You need daily OHLCV bars for whatever symbols you want to test. This
sandbox has no network access, so I can't fetch real data for you here —
you'll need to supply it yourself, from any of:
- Schwab's own `price_history` endpoint (same one `market_data.py` already
  calls — could write a small script that loops over a date range... though
  Schwab's endpoint returns *current* trailing history, not arbitrary past
  windows beyond what it retains)
- A free vendor CSV export (Stooq, Yahoo Finance's download button, etc.)
- Any data you already have

Load it with:
```python
from timeseries import TimeSeriesStore
store = TimeSeriesStore("./bot.db")
store.import_bars_csv("AAPL", "1d", "aapl_daily.csv")   # adjust column names via kwargs if needed
print(store.coverage_summary("AAPL"))                    # confirm what you actually loaded
```

## Running a backtest

```python
import yaml
from timeseries import TimeSeriesStore
from backtest_engine import BacktestEngine

cfg = yaml.safe_load(open("config.yaml"))
store = TimeSeriesStore("./bot.db")

engine = BacktestEngine(
    cfg, store,
    strategies=("credit_spreads", "directional"),   # earnings_vol/wheel not supported yet — see backtest_engine.py docstring
    chain_source="synthetic",                        # or "recorded" once you've built real history
    approval_score_threshold=0.55,                    # simulates "would I have tapped Approve"
    stop_loss_pct=1.5,                                # optional — see next section
    take_profit_pct=0.5,
)

result = engine.run(symbols=["AAPL", "MSFT", "NVDA"], start_date="2024-01-01", end_date="2024-12-31")
print(result.summary())
result.trades_to_csv("backtest_trades.csv")
```

## Why `earnings_vol.py` and `wheel.py` aren't included

- `earnings_vol.py` needs a real historical earnings-date calendar.
  Finnhub's free tier doesn't give you that depth of history, and there's
  no offline substitute — so it's excluded rather than silently producing
  garbage.
- `wheel.py`'s covered-call half needs a historical account position state
  (you'd need to have already owned the stock). A backtest of a fresh
  account doesn't have that. Its cash-secured-put half *could* be adapted,
  but isn't wired in yet.

## What to actually do with backtest results

Split your data into an in-sample window (tune thresholds here) and a
genuinely separate out-of-sample window (only look at this once, to check
whether what you tuned still works). If performance falls apart
out-of-sample, that's the backtest doing its job — it means the in-sample
result was overfitting, not edge. This matters more than almost anything
else on this list.
