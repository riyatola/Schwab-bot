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
import os
import threading
import time
from datetime import date
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config_env
from auth import build_client, get_account_hash
from market_data import MarketDataAdapter
from risk_manager import RiskManager
from approval_manager import Db
from notifier import TelegramNotifier
from executor import Executor
from strategies import credit_spreads, directional, earnings_vol, wheel, leaps
from universe import get_universe as fetch_index_universe
from earnings import EarningsCalendar
from iv_history import IVHistoryStore, record_daily_iv_for_universe
from trade_tracker import TradeTracker
from guardrails import Guardrails
from learning import LearningEngine
from sentiment import SentimentEngine
from exit_manager import ExitManager
from circuit_breaker import CircuitBreaker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()],
)
log = logging.getLogger("scanner")


def load_config(path: str | None = None) -> dict:
    """Load config.yaml and apply environment-variable overrides (secrets +
    mode). See config_env.py for the env var list and precedence."""
    return config_env.load_config(path)


def start_health_server(port: int) -> None:
    """Minimal /healthz endpoint so a host (Render, Docker HEALTHCHECK) can
    confirm the process is up. Runs in a daemon thread; the trading loop is
    unaffected. Binds 0.0.0.0 so it's reachable from the platform's proxy."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/healthz", "/health"):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):  # silence per-request logging
            pass

    server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="healthz").start()
    log.info("Health endpoint listening on 0.0.0.0:%d (/healthz)", port)


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


def get_leaps_universe(client, cfg: dict) -> list[str]:
    """Universe for the LEAPS scanner. Deliberately narrower than
    get_universe(): uses the curated `universe.leaps_watchlist` when set,
    otherwise index constituents only. extra_symbols (meme / speculative /
    leveraged names) and the movers catch-all are excluded — LEAPS on those
    are not stock replacement (spec section 4.5)."""
    excluded = set(cfg["universe"].get("exclude_symbols", []))
    watchlist = cfg["universe"].get("leaps_watchlist") or []
    if watchlist:
        return sorted({s for s in watchlist if s not in excluded})
    index_labels = [INDEX_NAME_MAP[i] for i in cfg["universe"].get("indices", []) if i in INDEX_NAME_MAP]
    symbols = set(fetch_index_universe(index_labels, [], []))
    return sorted(symbols - excluded)


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


async def run_scan_cycle(client, account_hash, cfg, market_data, risk, db, notifier, guardrails, learning, sentiment, circuit_breaker):
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
        # Size first, then gate: gate() rejects a candidate sized to 0
        # contracts (fair value above max_risk_per_trade_pct), so sizing must
        # happen before the gate to catch it.
        candidate = risk.size_candidate(candidate, snapshot["net_liq"])
        if not risk.gate(candidate, snapshot):
            continue
        approval_id = db.create_approval(candidate, cfg["approval"]["timeout_minutes"])
        await notifier.send_approval_request(approval_id, candidate)
        log.info("Sent approval request %s for %s (%s), score=%.2f",
                  approval_id, candidate["symbol"], candidate["strategy"], candidate["score"])


async def run_leaps_cycle(client, account_hash, cfg, market_data, risk, db, notifier, guardrails, learning, sentiment, circuit_breaker):
    """LEAPS scan, run on its own (low-frequency) daily schedule — see the
    run_leaps_cycle job in main(). Kept separate from the 15-minute
    run_scan_cycle on purpose: LEAPS signals move on trend / 200-DMA / IV-rank
    timescales, so a fast cadence would only burn chain fetches and re-alert
    the same setup. A per-symbol cooldown (strategies.leaps.cooldown_days)
    dedupes repeat alerts across cycles."""
    db.expire_stale()

    if circuit_breaker.is_tripped():
        log.warning("Circuit breaker tripped, skipping LEAPS scan")
        return

    regime = sentiment.refresh_macro_regime()
    log.info("LEAPS macro regime: %s", regime)

    snapshot = risk.account_snapshot(client, account_hash)
    symbols = get_leaps_universe(client, cfg)

    active_bans = {b["symbol"] for b in guardrails.list_active_bans()}
    if active_bans:
        symbols = [s for s in symbols if s not in active_bans]
    log.info("LEAPS scanning %d symbol(s) (%d banned excluded)", len(symbols), len(active_bans))

    candidates = leaps.scan(client, symbols, cfg, market_data)
    log.info("LEAPS found %d raw candidate(s) before risk gating", len(candidates))

    candidates = [learning.apply(c) for c in candidates]
    candidates = [sentiment.apply(c) for c in candidates]
    candidates.sort(key=lambda c: c["score"], reverse=True)

    cooldown_days = cfg["strategies"]["leaps"].get("cooldown_days", 7)
    for candidate in candidates:
        if circuit_breaker.is_tripped():
            break
        if db.recent_approval_exists(candidate["symbol"], "leaps", cooldown_days):
            log.info("LEAPS: skipping %s — alerted within the %d-day cooldown",
                     candidate["symbol"], cooldown_days)
            continue
        candidate = risk.size_candidate(candidate, snapshot["net_liq"])
        if not risk.gate(candidate, snapshot):
            continue
        approval_id = db.create_approval(candidate, cfg["approval"]["timeout_minutes"])
        await notifier.send_approval_request(approval_id, candidate)
        log.info("Sent LEAPS approval request %s for %s, score=%.2f",
                  approval_id, candidate["symbol"], candidate["score"])


async def run_exit_check(exit_manager, db, notifier, circuit_breaker):
    """Check open positions for exit triggers (stop-loss/take-profit),
    send approval requests if triggers hit."""
    if circuit_breaker.is_tripped():
        log.warning("Circuit breaker tripped, skipping exit checks")
        return

    triggers = exit_manager.check_triggers()
    if not triggers:
        return

    for trigger in triggers:
        if circuit_breaker.is_tripped():
            break
        candidate = exit_manager.build_exit_candidate(trigger)
        # Create approval request
        timeout = exit_manager.exit_cfg.get("approval_timeout_minutes", 10)
        approval_id = db.create_approval(candidate, timeout)
        # Update original trade's exit_approval_id
        exit_manager.conn.execute(
            "UPDATE trades SET exit_approval_id=? WHERE id=?",
            (approval_id, trigger["trade_id"]),
        )
        exit_manager.conn.commit()
        await notifier.send_approval_request(approval_id, candidate)
        log.info(f"Sent exit approval request for trade {trigger['trade_id']}")


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
    start_health_server(int(os.environ.get("PORT", "8080")))
    client = build_client(cfg)

    if args.auth_only:
        get_account_hash(client, cfg["schwab"].get("account_index", 0))
        log.info("Auth complete, tokens cached.")
        return

    account_hash = get_account_hash(client, cfg["schwab"].get("account_index", 0))
    db = Db()
    iv_store = IVHistoryStore()
    earnings_calendar = EarningsCalendar(cfg)
    market_data = MarketDataAdapter(client, cfg=cfg, iv_store=iv_store, earnings_calendar=earnings_calendar)

    guardrails = Guardrails(cfg, db)
    learning = LearningEngine(cfg, db)
    sentiment = SentimentEngine(cfg, db, market_data=market_data)
    exit_manager = ExitManager(cfg, db, client, account_hash, market_data=market_data)
    circuit_breaker = CircuitBreaker(
        cfg, db, client, account_hash, paper_mode=cfg.get("mode", "paper") == "paper"
    )
    risk = RiskManager(cfg, db, guardrails=guardrails, market_data=market_data)

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
        args=[client, account_hash, cfg, market_data, risk, db, notifier, guardrails, learning, sentiment, circuit_breaker],
    )
    # LEAPS runs on its own low-frequency schedule (two checks/day, US/Eastern)
    # rather than the 15-minute scan — its signals move on trend / 200-DMA /
    # IV-rank timescales. The scheduler's default timezone is server-local, so
    # pin it to America/New_York explicitly.
    if cfg.get("strategies", {}).get("leaps", {}).get("enabled", False):
        leaps_args = [client, account_hash, cfg, market_data, risk, db, notifier,
                      guardrails, learning, sentiment, circuit_breaker]
        for _hour, _minute in cfg["strategies"]["leaps"].get("scan_times", [[10, 45], [15, 30]]):
            scheduler.add_job(
                run_leaps_cycle,
                "cron",
                day_of_week="mon-fri",
                hour=_hour,
                minute=_minute,
                timezone="America/New_York",
                args=leaps_args,
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
        lambda: record_daily_iv_for_universe(get_universe(client, cfg), market_data, iv_store),
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
    scheduler.start()

    await run_scan_cycle(client, account_hash, cfg, market_data, risk, db, notifier, guardrails, learning, sentiment, circuit_breaker)

    # keep the event loop alive for the scheduler + telegram polling
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
