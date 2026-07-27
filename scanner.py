"""
Main entrypoint. Wires together auth, market data, the four strategy
modules, risk gating, the approval queue, the Telegram notifier, and now
the learning/guardrail loop:

  scan -> candidates -> [filter banned symbols] -> [learning score adjust]
       -> [sentiment score adjust: macro regime + news, see sentiment.py]
       -> risk.gate() (includes guardrail hard-check) -> approval -> ...
       -> execute -> trade_tracker records entry context
       -> (scheduled) trade_tracker polls outcomes
       -> (scheduled) guardrails.evaluate_all_symbols() auto-bans losers
       -> (scheduled) learning.maybe_retrain_ml_model() + weekly report
          (weekly report also includes sentiment.summarize() — macro regime)

Sentiment (sentiment.py) follows the same rule as learning.py: it only
reweights candidates the strategy modules already produced. It never
invents a trade and never overrides risk_manager's hard caps.

Usage:
    python scanner.py --auth-only     # just do the OAuth flow once, then exit
    python scanner.py                 # run the scan loop
"""

import asyncio
import argparse
import logging
import time
import yaml
from datetime import date
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from auth import build_client, get_account_hash
from market_data import MarketDataAdapter
from risk_manager import RiskManager
from approval_manager import Db
from notifier import TelegramNotifier
from executor import Executor
from strategies import credit_spreads, directional, earnings_vol, wheel
from universe import get_universe as fetch_index_universe
from earnings import EarningsCalendar
from iv_history import IVHistoryStore
from trade_tracker import TradeTracker
from guardrails import Guardrails
from learning import LearningEngine
from sentiment import SentimentEngine
from exit_manager import ExitManager
from circuit_breaker import CircuitBreaker
from timeseries import TimeSeriesStore
from datetime import date
from optionstrategist_iv import run_import as run_optionstrategist_import, ExternalIVStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()],
)
log = logging.getLogger("scanner")


def load_config(path="config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


INDEX_NAME_MAP = {
    "NASDAQ100": "NASDAQ100",
    "SP500": "SP500",
}


def get_universe(client, cfg: dict) -> list[str]:
    """
    Pulls NASDAQ-100 / S&P 500 constituents from universe.py (backed by
    yfiua/index-constituents, cached locally with a bundled fallback), plus
    any extra_symbols/exclude_symbols from config.yaml. Also folds in
    today's NASDAQ volume movers as a bonus catch-all for names outside
    those two indices that are suddenly active.
    """
    index_labels = [INDEX_NAME_MAP[i] for i in cfg["universe"].get("indices", []) if i in INDEX_NAME_MAP]
    symbols = set(fetch_index_universe(
        index_labels,
        cfg["universe"].get("extra_symbols", []),
        cfg["universe"].get("exclude_symbols", []),
    ))

    try:
        resp = client.movers("$NASDAQ", sort="VOLUME", frequency=0)
        resp.raise_for_status()
        for m in resp.json().get("screeners", []):
            symbols.add(m["symbol"])
    except Exception:
        log.warning("Movers lookup failed; continuing with index constituents only.")

    excluded = set(cfg["universe"].get("exclude_symbols", []))
    return sorted(symbols - excluded)


def record_daily_snapshots(client, cfg, market_data, iv_store, ts_store):
    """Daily job (cron, see 15:45 scheduling below) — replaces the old
    per-15-min-cycle chain recording (AUDIT_FINDINGS.md #6). Fetches each
    symbol's option chain ONCE per day and uses that single fetch for two
    things:
      1. A full option_chain_snapshot row set (ts_store) — builds the
         real historical chain data backtest_engine.py needs for
         chain_source="recorded".
      2. The ATM IV snapshot (iv_store) that used to be pulled by
         iv_history.record_daily_iv_for_universe() with its own separate
         chain fetch.
    One API pass instead of two, and it only happens once a day near
    market close instead of every 15 minutes — a much smaller, much less
    duplicated footprint on both Schwab's rate limits and bot.db.
    """
    from strategies.base import dte as _dte
    from iv_history import compute_atm_iv

    symbols = get_universe(client, cfg)
    dte_min, dte_max = 21, 60          # wide enough to cover every strategy's DTE window
    iv_target_min, iv_target_max = 25, 35  # same target Schwab-facing window iv_history.py used
    iv_target_mid = (iv_target_min + iv_target_max) / 2

    iv_snapshots = {}
    chains_recorded = 0
    for symbol in symbols:
        try:
            chain = market_data.get_option_chain(symbol, dte_min, dte_max)
            if not chain:
                continue

            ts_store.record_option_chain_snapshot(symbol, chain)
            chains_recorded += 1

            # Pick whichever expiration in the chain sits closest to the
            # ~30 DTE IV target, then compute ATM IV off that expiration.
            best_exp = min(chain.keys(), key=lambda e: abs(_dte(e) - iv_target_mid))
            underlying_price = market_data.get_last_price(symbol)
            atm_iv = compute_atm_iv(chain[best_exp], underlying_price)
            if atm_iv:
                iv_snapshots[symbol] = atm_iv
        except Exception as e:
            log.warning("Daily snapshot failed for %s: %s", symbol, e)

    iv_store.record_many(iv_snapshots)
    log.info("Daily snapshot: recorded option chains for %d/%d symbols, ATM IV for %d symbols",
              chains_recorded, len(symbols), len(iv_snapshots))
    return {"chains_recorded": chains_recorded, "iv_recorded": len(iv_snapshots)}


async def run_circuit_breaker_check(circuit_breaker, notifier):
    """Check circuit breaker and send alert if it trips."""
    if not circuit_breaker.enabled:
        return
    tripped = circuit_breaker.check_and_trip()
    if tripped:
        status = circuit_breaker.get_status()
        await notifier.send_note(
            f"⚠️ CIRCUIT BREAKER TRIPPED! ⚠️\n"
            f"Reason: {status.get('reason', 'Unknown')}\n"
            f"Total daily P&L: ${status.get('total_daily_pnl', 0):.2f}\n"
            f"New approvals disabled until reset or tomorrow."
        )
    # Also check manual kill switch
    if circuit_breaker.manual_kill and not circuit_breaker.is_tripped():
        # Update the DB's manual_kill flag
        today = date.today().isoformat()
        circuit_breaker.conn.execute("""
            UPDATE circuit_breaker SET manual_kill = 1 WHERE date = ?
        """, (today,))
        circuit_breaker.conn.commit()
        await notifier.send_note("⚠️ MANUAL KILL SWITCH ACTIVE! ⚠️ New approvals disabled.")


async def run_scan_cycle(client, account_hash, cfg, market_data, risk, db, notifier, guardrails, learning, sentiment, circuit_breaker, ts_store):
    db.expire_stale()

    # Check circuit breaker first
    if circuit_breaker.is_tripped():
        log.warning("Circuit breaker tripped, skipping scan for new approvals")
        return

    # Cheap (cached for hours) — refreshed once per cycle so the regime used
    # below reflects today's persistence-confirmed reading, not a stale one
    # from process start.
    regime = sentiment.refresh_macro_regime()
    log.info("Macro regime: %s", regime)

    snapshot = risk.account_snapshot(client, account_hash)
    symbols = get_universe(client, cfg)

    # Filter out auto-banned symbols before spending API calls scanning them.
    active_bans = {b["symbol"] for b in guardrails.list_active_bans()}
    if active_bans:
        symbols = [s for s in symbols if s not in active_bans]
    log.info("Scanning %d symbols (%d banned symbols excluded)", len(symbols), len(active_bans))

    min_avg_vol = cfg["universe"].get("min_avg_option_volume", 0)
    if min_avg_vol:
        liquid_symbols = []
        for s in symbols:
            try:
                if market_data.get_avg_volume(s) >= min_avg_vol:
                    liquid_symbols.append(s)
            except Exception:
                log.debug("Volume lookup failed for %s — excluding from this cycle", s)
        log.info("Liquidity prefilter: %d/%d symbols meet min_avg_option_volume=%s",
                  len(liquid_symbols), len(symbols), min_avg_vol)
        symbols = liquid_symbols

    # NOTE: option chain snapshots are NOT recorded here anymore (see
    # AUDIT_FINDINGS.md #6). Doing a full-universe chain pull every
    # poll_interval_seconds (default 15 min) hammered Schwab's rate limits
    # and wrote near-duplicate rows to option_chain_snapshots all day long,
    # since chains barely move intraday. Recording now happens once/day in
    # record_daily_snapshots(), wired into the 15:45 cron job below,
    # reusing the same chain fetch for both the IV history snapshot and
    # the full chain snapshot (one API pass instead of two).

    all_candidates = []
    s_cfg = cfg["strategies"]

    if s_cfg["credit_spreads"]["enabled"]:
        all_candidates += credit_spreads.scan(client, symbols, cfg, market_data)
    if s_cfg["directional"]["enabled"]:
        all_candidates += directional.scan(client, symbols, cfg, market_data)
    if s_cfg["earnings_vol"]["enabled"]:
        all_candidates += earnings_vol.scan(client, symbols, cfg, market_data)
    if s_cfg["wheel"]["enabled"]:
        all_candidates += wheel.scan(client, snapshot["positions"], cfg, market_data)

    log.info("Found %d raw candidates before risk gating", len(all_candidates))

    # Apply learned score adjustments, then the sentiment overlay (macro
    # regime + per-symbol news, see sentiment.py) — both are reweighters,
    # neither invents candidates or touches risk caps. Re-sort afterward so
    # the highest final score within each cycle gets sent first if
    # max_new_trades_per_day caps out.
    all_candidates = [learning.apply(c) for c in all_candidates]
    all_candidates = [sentiment.apply(c) for c in all_candidates]
    all_candidates.sort(key=lambda c: c["score"], reverse=True)

    for candidate in all_candidates:
        # Check circuit breaker again before each approval in case it tripped mid-scan
        if circuit_breaker.is_tripped():
            break
        candidate = risk.size_candidate(candidate, snapshot["net_liq"])
        if not risk.gate(candidate, snapshot):
            continue
        
        approval_id = db.create_approval(candidate, cfg["approval"]["timeout_minutes"])
        await notifier.send_approval_request(approval_id, candidate)
        log.info("Sent approval request %s for %s (%s), score=%.2f",
                  approval_id, candidate["symbol"], candidate["strategy"], candidate["score"])


async def run_exit_check(exit_manager, db, notifier, circuit_breaker):
    if circuit_breaker.is_tripped():
        log.warning("Circuit breaker tripped, skipping exit checks")
        return

    # 1. Check exit triggers
    triggers = exit_manager.check_triggers()
    for trigger in triggers:
        if circuit_breaker.is_tripped():
            break
        candidate = exit_manager.build_exit_candidate(trigger)
        timeout = exit_manager.exit_cfg.get("approval_timeout_minutes", 10)
        approval_id = db.create_approval(candidate, timeout)
        exit_manager.conn.execute(
            "UPDATE trades SET exit_approval_id=? WHERE id=?",
            (approval_id, trigger["trade_id"]),
        )
        exit_manager.conn.commit()
        await notifier.send_approval_request(approval_id, candidate)
        log.info(f"Sent exit approval request for trade {trigger['trade_id']}")

    # 2. Check prolonged risk-off and send alert (once per day)
    if exit_manager.check_prolonged_risk_off():
        today = date.today().isoformat()
        if exit_manager.last_risk_off_alert_date != today:
            exit_manager.last_risk_off_alert_date = today
            await notifier.send_note(
                "⚠️ **Risk-Off Regime Persisted for 3+ Days** ⚠️\n"
                "Consider reviewing your open positions, especially correlated directional/earnings_vol trades. "
                "Stop-loss levels have been tightened automatically."
            )
async def run_learning_cycle(db, guardrails, learning, sentiment, notifier):
    """Weekly-ish job: re-evaluate bans, retrain the optional ML model, and
    push a summary to Telegram so you can see what the bot has "learned" —
    now including the macro regime sentiment.py has been running with."""
    newly_banned = guardrails.evaluate_all_symbols()
    learning.maybe_retrain_ml_model()
    summary = learning.summarize() + "\n\n" + sentiment.summarize()
    active_bans = guardrails.list_active_bans()
    await notifier.send_learning_report(summary, newly_banned, active_bans)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--auth-only", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    client = build_client(cfg)

    if args.auth_only:
        get_account_hash(client, cfg["schwab"].get("account_index", 0))
        log.info("Auth complete, tokens cached.")
        return

    account_hash = get_account_hash(client, cfg["schwab"].get("account_index", 0))
    db = Db()
    iv_store = IVHistoryStore()
    ts_store = TimeSeriesStore()
    earnings_calendar = EarningsCalendar(cfg)
    external_iv_store = ExternalIVStore()
    market_data = MarketDataAdapter(client, cfg=cfg, iv_store=iv_store, earnings_calendar=earnings_calendar,
                                     external_iv_store=external_iv_store)

    guardrails = Guardrails(cfg, db)
    learning = LearningEngine(cfg, db)
    sentiment = SentimentEngine(cfg, db, market_data=market_data)
    risk = RiskManager(cfg, db, guardrails=guardrails, market_data=market_data)
    circuit_breaker = CircuitBreaker(cfg, db, client, account_hash,paper_mode=cfg.get("mode", "paper") == "paper")
    exit_manager = ExitManager(cfg, db, client, account_hash, market_data=market_data,
    sentiment=sentiment)
    trade_tracker = TradeTracker(db, client, account_hash)
    executor = Executor(cfg, client, account_hash, db, trade_tracker=trade_tracker)
    notifier = TelegramNotifier(cfg, db, executor)

    await notifier.run_polling_forever()
    await notifier.send_note("Schwab options scanner started. Alerts will appear here.")

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_scan_cycle,
        "interval",
        seconds=cfg["scan"]["poll_interval_seconds"],
        args=[client, account_hash, cfg, market_data, risk, db, notifier, guardrails, learning, sentiment, circuit_breaker, ts_store],
        next_run_time=None,
    )
    # Check for exit triggers on same interval as scan
    scheduler.add_job(
        lambda: asyncio.create_task(run_exit_check(exit_manager, db, notifier, circuit_breaker)),
        "interval",
        seconds=cfg["scan"]["poll_interval_seconds"],
    )
    # Check circuit breaker
    scheduler.add_job(
        lambda: asyncio.create_task(run_circuit_breaker_check(circuit_breaker, notifier)),
        "interval",
        seconds=cfg.get("circuit_breaker", {}).get("check_interval_seconds", 900),
    )
    scheduler.add_job(
        lambda: record_daily_snapshots(client, cfg, market_data, iv_store, ts_store),
        "cron",
        hour=15, minute=45,
    )
    # Poll trade outcomes a few times a day during market hours — closing
    # fills need to actually settle/show up before this can find them.
    scheduler.add_job(
        trade_tracker.poll_and_update_outcomes,
        "interval",
        minutes=cfg.get("learning", {}).get("outcome_poll_minutes", 45),
    )
    # Re-evaluate guardrail bans and retrain/report weekly by default —
    # this is a judgment over accumulated history, not a per-cycle check.
    scheduler.add_job(
        lambda: asyncio.create_task(run_learning_cycle(db, guardrails, learning, sentiment, notifier)),
        "cron",
        day_of_week=cfg.get("learning", {}).get("report_day_of_week", "sun"),
        hour=cfg.get("learning", {}).get("report_hour", 18),
    )
    # Paper mode only: settle any option positions past expiration at
    # intrinsic value so trade_tracker/learning/guardrails have realized
    # outcomes to learn from. No-op (attribute won't exist) in live mode.
    if hasattr(client, "settle_expired_positions"):
        scheduler.add_job(client.settle_expired_positions, "cron", hour=16, minute=15)
    # Check for early assignment risk every hour
    if hasattr(client, "check_early_assignment_risk"): scheduler.add_job(client.check_early_assignment_risk, "cron", hour=16, minute=10)
    # Weekly IV import from OptionStrategist
    scheduler.add_job(
        lambda: run_optionstrategist_import(db_path="./bot.db"),
        "cron",
        day_of_week="sun",
        hour=6, minute=0,
    )
    scheduler.start()

    await run_scan_cycle(client, account_hash, cfg, market_data, risk, db, notifier, guardrails, learning, sentiment, circuit_breaker, ts_store)

    # keep the event loop alive for the scheduler + telegram polling
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
