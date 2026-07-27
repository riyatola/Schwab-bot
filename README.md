# Schwab Options Scanner + Approval Bot

A rules-based scanner that watches NASDAQ/S&P names, screens for setups across four
options strategy types, and sends you a Telegram alert with **Approve** / **Reject**
buttons before anything touches your account. Nothing trades without your tap.

**This is infrastructure, not a signal service.** You own and tune every rule in
`strategies/`. Treat the default thresholds as starting points to backtest and adjust,
not as advice.

## New Features!

- **Paper Trading Mode**: Default! Uses real market data with simulated fills/account (never routes to Schwab until you set `mode: live` in [config.yaml](file:///c:/Users/USER/Desktop/schwab-options-bot/config.yaml)).
- **Adaptive Learning**: Rule-based and optional ML-based scoring adjustments based on past trade performance.
- **Sentiment Overlay**: Macro regime (VIX, yield curve, credit spreads) + per-symbol news sentiment (Finnhub, contrarian at extremes) to reweight scores.
- **Auto-Guardrails**: Dynamic symbol banning for consistent losers.
- **Weekly Reports**: Telegram summary of learning, active bans, and macro regime.

## Why this stack

- **Official Schwab Trader API** (developer.schwab.com) for market data + trading,
  wrapped by **schwabdev** (OAuth-based, actively maintained, MIT licensed).
  Avoided browser-automation wrappers that require your actual login password/TOTP —
  those violate Schwab's ToS and are a real credential-leak risk.
- **Telegram Bot API** for alerts + one-tap approval (free, has native inline buttons,
  no app to build). Swappable for Discord/Pushover if you prefer — see [notifier.py](file:///c:/Users/USER/Desktop/schwab-options-bot/notifier.py).
- **SQLite** for the approval queue and trade log (zero infra to run).

## Architecture

```
Universe Scan → Strategy Modules (4 types) → Learning Score Adjust → Sentiment Overlay (Macro Regime + News) → Risk Gating → Approval Queue → Telegram Alert
                                                                                                                                      │
                                                                                                                            You tap Approve/Reject
                                                                                                                                      │
                                                                                                                            Executor → Schwab/Paper Broker → Confirmation
                                                                                                                                      │
                                                                                                                            (scheduled) Trade Outcome Poll
                                                                                                                                      │
                                                                                                                            (scheduled) Learning/Guardrail/Sentiment Updates
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
   (Optional, for ML learning: `pip install scikit-learn joblib`)
4. **Configure [config.yaml](file:///c:/Users/USER/Desktop/schwab-options-bot/config.yaml)**:
   - `mode`: Defaults to `paper` (safe, simulated fills). Switch to `live` *only* after testing paper mode thoroughly.
   - `schwab`: Your app key/secret from Schwab Developer.
   - `telegram`: Bot token from [@BotFather](https://t.me/BotFather) + your chat ID (send a message to your bot first, then the bot can find you).
   - `earnings`: Your Finnhub API key (free tier, see [config.yaml](file:///c:/Users/USER/Desktop/schwab-options-bot/config.yaml)).
   - `fred`: Your FRED API key (free, for macro regime, see [config.yaml](file:///c:/Users/USER/Desktop/schwab-options-bot/config.yaml)).
   - `paper`: Optional paper trading settings (starting cash, slippage, commissions).
   - `risk`: Your risk tolerance (max position size, concurrent positions, etc.).
   - `learning`, `guardrails`, `sentiment`: Tune adaptive behavior (defaults are safe).
5. **Authenticate with Schwab**: First run does OAuth browser flow once, then caches a refresh token:
   ```bash
   python scanner.py --auth-only
   ```
6. **Run the scanner**:
   ```bash
   python scanner.py
   ```

## Strategy Modules (all enabled by default, toggle in [config.yaml](file:///c:/Users/USER/Desktop/schwab-options-bot/config.yaml))

| Module | File | What it looks for |
|---|---|---|
| Credit spreads (income) | `strategies/credit_spreads.py` | High IV rank, defined-risk verticals with favorable risk/reward and short strikes outside a probability-of-touch band |
| Directional (momentum) | `strategies/directional.py` | Trend + volume breakout confirmation before flagging long calls/puts |
| Earnings volatility | `strategies/earnings_vol.py` | Upcoming earnings, IV crush setups, straddle/strangle pricing vs. historical realized move |
| Wheel / covered calls | `strategies/wheel.py` | Cash-secured put candidates on names you'd be OK owning, and covered call candidates on existing long stock positions |

Each module returns candidates with a numeric score and the concrete contract(s),
strikes, and expirations — nothing fires without a human tap.

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
- Auto-bans expire after configurable time (default 90 days).
- Manual bans/unbans via SQLite or code.

### Sentiment Overlay (`sentiment.py`)
- **Macro Regime (Risk-On/Neutral/Risk-Off)**: Uses FRED data:
  - VIX percentile (252-day lookback)
  - 10y-2y yield curve slope
  - High-yield credit spread day-over-day change
  - Majority vote for raw regime; confirmed only after `persistence_days` (default 3) of consecutive readings to filter noise
  - Optional live Schwab quote for $VIX.X to refine current reading
- **Per-Symbol News Sentiment (Finnhub)**: Reuses `earnings.finnhub_api_key`; only acts at extremes (default |score| ≥ 0.8) with contrarian dampening (fades toward neutral, never amplifies)
- **Multipliers**: Applies per-strategy multipliers based on confirmed macro regime
- **Weekly Report**: Summarizes macro regime in weekly Telegram report

## Safety Rails Baked In

- Hard position-size and max-concurrent-position caps in [risk_manager.py](file:///c:/Users/USER/Desktop/schwab-options-bot/risk_manager.py), enforced **before** a trade is ever sent for approval (bad candidates never reach you).
- Pattern Day Trader (PDT) check against account equity if under $25k.
- Approval requests expire after a configurable window (default 15 min) since option prices move — stale approvals are auto-rejected rather than executed at a stale price.
- Every action (scan result, alert sent, approval, order, fill) is logged to SQLite and to `bot.log`.
- Defaults to paper mode — you have to *opt in* to live trading.

## Universe, Earnings, and IV Rank

- **Constituents**: [universe.py](file:///c:/Users/USER/Desktop/schwab-options-bot/universe.py) pulls NASDAQ-100 / S&P 500 membership from [yfiua/index-constituents](https://github.com/yfiua/index-constituents), cached locally for a week with a bundled fallback.
- **Earnings calendar**: [earnings.py](file:///c:/Users/USER/Desktop/schwab-options-bot/earnings.py) uses [Finnhub](https://finnhub.io)'s free tier (set `earnings.finnhub_api_key` in [config.yaml](file:///c:/Users/USER/Desktop/schwab-options-bot/config.yaml)).
- **ATM IV history**: [iv_history.py](file:///c:/Users/USER/Desktop/schwab-options-bot/iv_history.py) records daily snapshots for IV rank calculation (transparently falls back to realized volatility until enough history exists).

## What this Does NOT Do

- No backtesting engine included — validate each module's rules against historical data before trusting it with real capital.
- No latency guarantees — this polls on an interval, it isn't a market-making system.
- No guarantee Schwab's API stays stable — watch `schwabdev`'s changelog.
- No early assignment simulation in paper mode.

## Project Structure

```
schwab-options-bot/
├── config.yaml              # Your configuration
├── scanner.py               # Main entrypoint
├── auth.py                  # Schwab client + paper broker wrapper
├── paper_broker.py          # Simulated trading client
├── strategies/              # Strategy modules
│   ├── base.py              # Shared strategy helpers
│   ├── credit_spreads.py
│   ├── directional.py
│   ├── earnings_vol.py
│   └── wheel.py
├── risk_manager.py          # Risk gating
├── executor.py              # Order placement
├── approval_manager.py      # Approval queue + trade log
├── trade_tracker.py         # Outcome tracking for learning
├── learning.py              # Adaptive scoring
├── guardrails.py            # Dynamic symbol banning
├── sentiment.py             # Sentiment overlay (macro regime + news)
├── notifier.py              # Telegram alerts
├── market_data.py           # Market data + IV rank
├── earnings.py              # Earnings calendar
├── iv_history.py            # IV history storage
├── universe.py              # Symbol universe management
├── data/                    # Cache + bundled data
├── requirements.txt         # Dependencies
├── README.md                # This file
└── CLAUDE_QUICKSTART.md     # Claude-specific quickstart guide
```
