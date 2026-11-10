# Schwab Options Scanner + Approval Bot

A rules-based scanner that watches NASDAQ/S&P names, screens for setups across **five**
options strategy types, and sends you a Telegram alert with **Approve** / **Reject**
buttons before anything touches your account. Nothing trades without your tap.

**This is infrastructure, not a signal service.** You own and tune every rule in
`strategies/`. Treat the default thresholds as starting points to backtest and adjust,
not as advice.

## Features

- **Paper Trading Mode**: Default! Uses real market data with simulated fills/account (never routes to Schwab until you set `mode: live` in [config.yaml](file:///c:/Users/USER/Desktop/schwab-options-bot/config.yaml)).
- **Five Strategy Modules**: credit spreads, directional momentum, earnings volatility, the wheel, and a **LEAPS "stock replacement"** scanner.
- **Backtesting Engine**: runs your *real* strategy modules unmodified against historical data — see [README_BACKTESTING.md](file:///c:/Users/USER/Desktop/schwab-options-bot/README_BACKTESTING.md).
- **Exit Management**: stop-loss / take-profit alerts (per-position, dollar- or percent-of-premium) plus a DTE force-exit rule.
- **Circuit Breaker / Kill Switch**: halts new approvals after a daily loss limit or a manual kill.
- **Adaptive Learning**: rule-based and optional ML-based scoring adjustments based on past trade performance.
- **Sentiment Overlay**: macro regime (VIX, yield curve, credit spreads) + per-symbol news sentiment (Finnhub, contrarian at extremes) to reweight scores.
- **Auto-Guardrails**: dynamic symbol banning for consistent losers.
- **Weekly Reports**: Telegram summary of learning, active bans, and macro regime.

## Why this stack

- **Official Schwab Trader API** (developer.schwab.com) for market data + trading,
  wrapped by **schwabdev** (OAuth-based, actively maintained, MIT licensed).
  Avoided browser-automation wrappers that require your actual login password/TOTP —
  those violate Schwab's ToS and are a real credential-leak risk.
- **Telegram Bot API** for alerts + one-tap approval (free, has native inline buttons,
  no app to build). Swappable for Discord/Pushover if you prefer — see [notifier.py](file:///c:/Users/USER/Desktop/schwab-options-bot/notifier.py).
- **SQLite** for the approval queue and trade log (zero infra to run). Time-series bars and
  option-chain snapshots share the same `bot.db` — see [timeseries.py](file:///c:/Users/USER/Desktop/schwab-options-bot/timeseries.py).

## Architecture

```
Universe Scan → Strategy Modules (5 types) → Learning Score Adjust → Sentiment Overlay (Macro Regime + News) → Risk Gating → Approval Queue → Telegram Alert
                                                                                                                                      │
                                                                                                                            You tap Approve/Reject
                                                                                                                                      │
                                                                                                                            Executor → Schwab/Paper Broker → Confirmation
                                                                                                                                      │
                                                                                                                            (scheduled) Exit Manager (SL/TP/DTE) → Approval
                                                                                                                                      │
                                                                                                                            (scheduled) Trade Outcome Poll
                                                                                                                                      │
                                                                                                                            (scheduled) Learning/Guardrail/Sentiment Updates
                                                                                                                                      │
                                                                                                                            (scheduled) Circuit Breaker (daily loss / kill switch)
```

## Setup

1. **Schwab Developer account**: Create an app at https://developer.schwab.com
   with both "Accounts and Trading Production" and "Market Data Production" scopes.
   Approval can take several days. Callback URL: `https://127.0.0.1`.
2. **Options approval level**: Confirm your actual Schwab account already has
   options approval (level 2+ for spreads) — the API can't grant this, it only
   reflects your account's real permissions.
3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```
   (Optional, for ML learning: `pip install scikit-learn joblib` — already listed in requirements.)
4. **Configure [config.yaml](file:///c:/Users/USER/Desktop/schwab-options-bot/config.yaml)**:
   - `mode`: Defaults to `paper` (safe, simulated fills). Switch to `live` *only* after testing paper mode thoroughly.
   - `schwab`: Your app key/secret from Schwab Developer.
   - `telegram`: Bot token from [@BotFather](https://t.me/BotFather) + your chat ID (send a message to your bot first, then the bot can find you).
   - `earnings`: Your Finnhub API key (free tier, see [config.yaml](file:///c:/Users/USER/Desktop/schwab-options-bot/config.yaml)).
   - `fred`: Your FRED API key (free, for macro regime, see [config.yaml](file:///c:/Users/USER/Desktop/schwab-options-bot/config.yaml)).
   - `paper`: Optional paper trading settings (starting cash, slippage, commissions).
   - `risk`: Your risk tolerance (max position size, concurrent positions, etc.). Includes LEAPS-specific caps under `risk.leaps`.
   - `learning`, `guardrails`, `sentiment`, `exits`, `circuit_breaker`: Tune adaptive behavior (defaults are safe).
5. **Authenticate with Schwab**: First run does OAuth browser flow once, then caches a refresh token:
   ```bash
   python scanner.py --auth-only
   ```
   On a headless/remote box where the browser flow can't open, use [manual_auth.py](file:///c:/Users/USER/Desktop/schwab-options-bot/manual_auth.py) instead.
6. **Run the scanner**:
   ```bash
   python scanner.py
   ```

## Strategy Modules

| Module | File | What it looks for | Backtestable |
|---|---|---|---|
| Credit spreads (income) | `strategies/credit_spreads.py` | High IV rank, defined-risk verticals with favorable risk/reward and short strikes outside a probability-of-touch band | Yes |
| Directional (momentum) | `strategies/directional.py` | Trend + volume breakout confirmation before flagging long calls/puts | Yes |
| Earnings volatility | `strategies/earnings_vol.py` | Upcoming earnings, IV crush setups, straddle/strangle pricing vs. historical realized move | No (needs historical earnings calendar) |
| Wheel / covered calls | `strategies/wheel.py` | Cash-secured put candidates on names you'd be OK owning, and covered call candidates on existing long stock positions | No (needs historical positions state) |
| LEAPS (stock replacement) | `strategies/leaps.py` | Deep-ITM long calls (delta ~0.80, 12–24 months out) as a leveraged substitute for owning 100 shares | Yes (synthetic P&L is a logic check only) |

Each module exposes the same contract — `scan(client, symbols, cfg, market_data) -> list[dict]` —
and returns candidates with a numeric score and the concrete contract(s), strikes, and
expirations. Nothing fires without a human tap. Enable/disable each module in [config.yaml](file:///c:/Users/USER/Desktop/schwab-options-bot/config.yaml).

## LEAPS Scanner (`strategies/leaps.py`)

A LEAP is just a listed option with more than ~1 year to expiration. This module is the
**"stock replacement"** variant: it looks for deep in-the-money calls (target delta ~0.80,
365–730 DTE) that cost materially less than 100 shares while still capturing most of the
underlying's move. Because a deep-ITM LEAP is mostly intrinsic value, little of the premium
is exposed to theta or IV swings — you're paying the cost of carry plus a slice of residual
volatility value for the leverage.

- **Phase 1 is call-side only** — a downtrend means skip. PMCC overlays and roll logic are phase 2/3.
- **Cheapest filter first**: cheap DB/indicator reads (IV rank band, trend strength) before any
  chain fetch, with a per-cycle chain-fetch cap (`max_chain_fetches_per_cycle`) so Schwab rate
  limits survive a large universe. Strongest-trend names are fetched first.
- **Scoring**: trend / carry (annualized extrinsic) / IV / liquidity / delta-fit, blended into a
  single score; candidates below `min_score` (default 0.55) are dropped.
- **Risk caps**: per-position premium cap (`risk.leaps.max_premium_pct_per_position`), total LEAPS
  premium cap (`risk.leaps.max_total_leaps_pct`), and a LEAPS-specific **sector cap** (`risk.leaps.max_risk_per_sector_pct`, default 15%).
- **Exits**: a dedicated **DTE force-exit** (`strategies.leaps.exits.force_exit_dte`, default 120)
  plus percent-of-premium stops (`exits.default_stop_loss_pct.leaps: 0.40` / `default_take_profit_pct.leaps: 1.00`).
- **Schedule**: runs on its own low-frequency cron (Mon–Fri, 10:45 and 15:30 US/Eastern) rather than the
  15-minute scan — LEAPS signals move on trend / 200-DMA / IV-rank timescales. A per-symbol **cooldown**
  (`strategies.leaps.cooldown_days`, default 7) prevents re-alerting the same name every cycle.
- **Universe**: uses the curated `universe.leaps_watchlist` when set, otherwise falls back to
  NASDAQ-100 + S&P 500 constituents (meme/extra symbols are deliberately excluded).
- **Sentiment**: risk-off halves LEAPS scores (long premium), risk-on boosts them modestly; the
  directional VIX-band multiplier is never applied to LEAPS.

Acceptance tests live in [tests/test_leaps.py](file:///c:/Users/USER/Desktop/schwab-options-bot/tests/test_leaps.py) (22 cases, fully offline).

## Exit Management & Circuit Breaker

### Exits (`exit_manager.py`)
- Monitors open positions for stop-loss and take-profit and **generates approval requests** —
  no auto-execution, same human-tap rule as entries.
- Levels are per-strategy in [config.yaml](file:///c:/Users/USER/Desktop/schwab-options-bot/config.yaml): absolute dollar `default_stop_loss` / `default_take_profit`
  where set, otherwise **percent-of-entry-premium** via `default_stop_loss_pct` / `default_take_profit_pct`.
- A **DTE force-exit** rule (`exits.<strategy>.force_exit_dte`) closes positions that are running out of time value.
- Marks positions to market using live data, falling back to Black-Scholes off the underlying.

### Circuit breaker (`circuit_breaker.py`)
- Tracks daily realized + unrealized P&L and trips when it exceeds `circuit_breaker.daily_loss_limit_dollars`.
- A manual kill switch (`circuit_breaker.manual_kill`) disables all new approvals on demand.

## Backtesting (`backtest_engine.py`)

Runs your **actual** strategy modules unmodified, day by day, over historical data, so a
strategy change can be validated against history before it reaches a live scan. See
[README_BACKTESTING.md](file:///c:/Users/USER/Desktop/schwab-options-bot/README_BACKTESTING.md) for full setup.

- **Supported**: `credit_spreads`, `directional`, and `leaps` (see `SUPPORTED_STRATEGIES`).
  `earnings_vol` and `wheel` are intentionally out of scope (need a historical earnings calendar
  and historical positions state respectively).
- **Data**: option pricing comes from real recorded snapshots ([timeseries.py](file:///c:/Users/USER/Desktop/schwab-options-bot/timeseries.py)) when you've been recording chains,
  otherwise from synthetic Black-Scholes pricing ([bs_pricing.py](file:///c:/Users/USER/Desktop/schwab-options-bot/bs_pricing.py)).
- **Honest limitation**: Schwab has no historical option-chain endpoint, so synthetic mode is a
  **logic check, not a P&L estimate** — no skew, no term structure, no liquidity. This is *especially*
  true for LEAPS, whose extrinsic value is dominated by carry and dividends that the synthetic model omits.
- **Bootstrap history** with [fetch_and_backtest.py](file:///c:/Users/USER/Desktop/schwab-options-bot/fetch_and_backtest.py), which pulls 5 years of daily bars
  (LEAPS needs 200+ trading days of warm-up) into the time-series store.
- Results include an equity curve plus a buy-and-hold benchmark (SPY, and the traded underlyings themselves).

## Paper Trading Mode

Default! Uses real market data with simulated fills/account (never routes to Schwab until you set `mode: live`). Key features:
- Simulated slippage and commissions (configurable in [config.yaml](file:///c:/Users/USER/Desktop/schwab-options-bot/config.yaml)).
- Expiration settlement at intrinsic value (no early assignment simulation).
- Use `PaperBrokerClient.seed_stock_position()` to test covered calls on the wheel strategy.
- Switch to live mode *only* after thorough testing!

## Learning & Guardrails

### Learning (`learning.py`)
- **Rule-based multiplier**: Adjusts scores based on past (strategy, symbol) win rate.
- **Optional ML layer**: Logistic regression that predicts win probability (enable with `learning.use_ml: true` in [config.yaml](file:///c:/Users/USER/Desktop/schwab-options-bot/config.yaml) — requires ~150 closed trades first).
- **Weekly report**: Telegram summary of performance and active bans.

### Guardrails (`guardrails.py`)
- Auto-bans symbols with:
  - Max consecutive losses (default 3).
  - Win rate below threshold (default 30%).
  - Cumulative losses above dollar limit (default $500).
- Bans are scoped per (symbol, strategy) pair; auto-bans expire after a configurable time (default 90 days).
- Manual bans/unbans via SQLite or code.

### Sentiment Overlay (`sentiment.py`)
- **Macro Regime (Risk-On/Neutral/Risk-Off)**: Uses FRED data:
  - VIX percentile (252-day lookback)
  - 10y-2y yield curve slope
  - High-yield credit spread day-over-day change
  - Majority vote for raw regime; confirmed only after `persistence_days` (default 3) of consecutive readings to filter noise
  - Optional live Schwab quote for $VIX.X to refine current reading
- **Per-Symbol News Sentiment (Finnhub)**: Reuses `earnings.finnhub_api_key`; only acts at extremes (default |score| ≥ 0.8) with contrarian dampening (fades toward neutral, never amplifies)
- **Multipliers**: Applies per-strategy multipliers based on the confirmed macro regime
- **Weekly Report**: Summarizes macro regime in the weekly Telegram report

## Safety Rails Baked In

- Hard position-size and max-concurrent-position caps in [risk_manager.py](file:///c:/Users/USER/Desktop/schwab-options-bot/risk_manager.py), enforced **before** a trade is ever sent for approval (bad candidates never reach you).
- LEAPS-specific caps: per-position premium %, total LEAPS premium %, and a 15% sector exposure cap.
- Pattern Day Trader (PDT) check against account equity if under $25k.
- Approval requests expire after a configurable window (default 15 min) since option prices move — stale approvals are auto-rejected rather than executed at a stale price.
- Circuit breaker halts new approvals after a daily loss limit or a manual kill.
- Every action (scan result, alert sent, approval, order, fill) is logged to SQLite and to `bot.log`.
- Defaults to paper mode — you have to *opt in* to live trading.

## Universe, Earnings, and IV Rank

- **Constituents**: [universe.py](file:///c:/Users/USER/Desktop/schwab-options-bot/universe.py) pulls NASDAQ-100 / S&P 500 membership from [yfiua/index-constituents](https://github.com/yfiua/index-constituents), cached locally for a week with a bundled fallback.
- **Earnings calendar**: [earnings.py](file:///c:/Users/USER/Desktop/schwab-options-bot/earnings.py) uses [Finnhub](https://finnhub.io)'s free tier (set `earnings.finnhub_api_key` in [config.yaml](file:///c:/Users/USER/Desktop/schwab-options-bot/config.yaml)).
- **ATM IV history**: [iv_history.py](file:///c:/Users/USER/Desktop/schwab-options-bot/iv_history.py) records daily snapshots for IV rank calculation (transparently falls back to realized volatility until enough history exists).
- **External IV seed**: [optionstrategist_iv.py](file:///c:/Users/USER/Desktop/schwab-options-bot/optionstrategist_iv.py) imports McMillan's free weekly volatility table so you get a usable IV percentile immediately instead of waiting ~40 trading days to build your own (kept in a separate table — see the module docstring).

## Testing

Offline acceptance tests use the standard library `unittest` (no network):

```bash
python -m unittest discover -s tests -v
# or a single module:
python -m unittest tests.test_leaps -v
```

## What this Does NOT Do

- **Backtest results do not predict live results.** The engine runs your real logic, but
  synthetic-mode pricing is a logic check, not alpha confirmation — vary parameters on an
  out-of-sample window before trusting anything.
- No latency guarantees — this polls on an interval, it isn't a market-making system.
- No guarantee Schwab's API stays stable — watch `schwabdev`'s changelog.
- No early assignment simulation in paper mode; backtest early-assignment is a threshold heuristic, not a model.
- `earnings_vol` and `wheel` are not backtestable (see above).

## Deployment

- [Dockerfile](file:///c:/Users/USER/Desktop/schwab-options-bot/Dockerfile) builds a `python:3.11-slim` image, copies `config.example.yaml` → `config.yaml`, and runs `python scanner.py` with a persistent `/data` volume.
- [render.yaml](file:///c:/Users/USER/Desktop/schwab-options-bot/render.yaml) describes a Render web service (`TZ=America/New_York`, `BOT_MODE=paper`) with secrets supplied as env vars (Schwab app key/secret/tokens, Telegram, Finnhub, FRED).
- [start_scanner.bat](file:///c:/Users/USER/Desktop/schwab-options-bot/start_scanner.bat) and [schwab-scanner.service](file:///c:/Users/USER/Desktop/schwab-options-bot/schwab-scanner.service) are convenience launchers for Windows and systemd respectively.

## Project Structure

```
schwab-options-bot/
├── config.yaml              # Your configuration (secrets — never commit)
├── config.example.yaml      # Template config
├── scanner.py               # Main entrypoint (scan loop + scheduler)
├── auth.py                  # Schwab client + paper broker wrapper
├── manual_auth.py           # Headless OAuth token exchange
├── paper_broker.py          # Simulated trading client
├── strategies/              # Strategy modules
│   ├── base.py              # Shared strategy helpers
│   ├── credit_spreads.py
│   ├── directional.py
│   ├── earnings_vol.py
│   ├── wheel.py
│   └── leaps.py             # LEAPS "stock replacement" scanner
├── risk_manager.py          # Risk gating (incl. LEAPS caps)
├── executor.py              # Order placement
├── approval_manager.py      # Approval queue + trade log
├── trade_tracker.py         # Outcome tracking for learning
├── exit_manager.py          # Stop-loss / take-profit / DTE exits
├── circuit_breaker.py       # Daily loss limit + kill switch
├── learning.py              # Adaptive scoring
├── guardrails.py            # Dynamic symbol banning
├── sentiment.py             # Sentiment overlay (macro regime + news)
├── notifier.py              # Telegram alerts
├── market_data.py           # Market data + IV rank
├── earnings.py              # Earnings calendar
├── iv_history.py            # IV history storage
├── optionstrategist_iv.py   # External weekly IV percentile importer
├── universe.py              # Symbol universe management
├── timeseries.py            # OHLCV bars + option-chain snapshot store
├── bs_pricing.py            # Black-Scholes synthetic pricing (backtest fallback)
├── backtest_engine.py       # Backtesting engine (runs real strategies)
├── fetch_and_backtest.py    # Bootstrap history + run a backtest
├── tests/                   # Offline acceptance tests (unittest)
│   └── test_leaps.py
├── data/                    # Cache + bundled index constituents
├── Dockerfile               # Container image
├── render.yaml              # Render deployment descriptor
├── requirements.txt         # Dependencies
├── README.md                # This file
├── README_BACKTESTING.md    # Backtesting setup & usage
└── CLAUDE_QUICKSTART.md     # Claude-specific quickstart guide
```
