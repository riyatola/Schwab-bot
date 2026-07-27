# Claude Quickstart Guide for Schwab Options Bot

Hey there! You've just opened the Schwab Options Bot project. Here's everything you need to know to work with it in Claude.

## Project Overview

This is a Python-based options trading bot that:
1. Scans NASDAQ/S&P universes for setups using 4 strategy modules
2. Sends Telegram alerts with Approve/Reject buttons
3. Trades only with manual approval
4. Now includes paper trading, adaptive learning, sentiment overlay (macro regime + news), and auto-guardrails!

## Key Files to Know

| File | Purpose |
|------|---------|
| [config.yaml](file:///c:/Users/USER/Desktop/schwab-options-bot/config.yaml) | Configuration (API keys, risk limits, mode, etc.) |
| [scanner.py](file:///c:/Users/USER/Desktop/schwab-options-bot/scanner.py) | Main entrypoint, orchestrates everything |
| [auth.py](file:///c:/Users/USER/Desktop/schwab-options-bot/auth.py) | Schwab client + paper broker wrapper |
| [paper_broker.py](file:///c:/Users/USER/Desktop/schwab-options-bot/paper_broker.py) | Simulated trading (default mode) |
| [strategies/](file:///c:/Users/USER/Desktop/schwab-options-bot/strategies) | Strategy modules |
| [learning.py](file:///c:/Users/USER/Desktop/schwab-options-bot/learning.py) | Adaptive scoring |
| [guardrails.py](file:///c:/Users/USER/Desktop/schwab-options-bot/guardrails.py) | Dynamic symbol banning |
| [sentiment.py](file:///c:/Users/USER/Desktop/schwab-options-bot/sentiment.py) | Sentiment overlay (macro regime + news) |

## First Things to Do

1. **Check [config.yaml](file:///c:/Users/USER/Desktop/schwab-options-bot/config.yaml)**:
   - Confirm `mode: paper` (safe default!)
   - Verify Telegram credentials are present (bot token and chat ID)
   - Fill in Schwab app key/secret, Finnhub API key, and FRED API key when ready

2. **Install Dependencies**:
   ```bash
   .\venv\Scripts\activate  # If using venv
   pip install -r requirements.txt
   ```

3. **Authenticate with Schwab**:
   ```bash
   python scanner.py --auth-only
   ```

4. **Run the Bot**:
   ```bash
   python scanner.py
   ```

## How to Make Changes

- **Adjust strategies**: Edit files in [strategies/](file:///c:/Users/USER/Desktop/schwab-options-bot/strategies)
- **Tune risk/learning/guardrails/sentiment**: Edit [config.yaml](file:///c:/Users/USER/Desktop/schwab-options-bot/config.yaml)
- **Change notifier (Telegram → Discord/etc.)**: Edit [notifier.py](file:///c:/Users/USER/Desktop/schwab-options-bot/notifier.py)

## Reminders

- Default mode is paper trading (safe!). Switch to `mode: live` *only* after thorough testing.
- All credentials are in [config.yaml](file:///c:/Users/USER/Desktop/schwab-options-bot/config.yaml) — never commit this file to Git!
- Check [README.md](file:///c:/Users/USER/Desktop/schwab-options-bot/README.md) for full docs!
