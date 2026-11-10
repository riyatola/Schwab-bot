
# fetch_and_backtest.py
import logging
from datetime import datetime, date
from auth import build_client
from config_env import load_config
from timeseries import TimeSeriesStore
from backtest_engine import BacktestEngine, run_walk_forward
from scanner import get_universe

# Setup logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

def fetch_and_store_history(client, symbols: list[str], ts_store: TimeSeriesStore):
    """Fetch daily OHLCV from Schwab and store in TimeSeriesStore.

    Pulls 5 years, not 2: the LEAPS scanner needs >= 200 trading days of
    warm-up for its 200-DMA filter before it can produce a signal, and a
    12-24 month hold barely fits inside a 2-year window otherwise.
    """
    # Get symbols we already have history for
    existing_symbols = {s["symbol"] for s in ts_store.coverage_summary()}
    
    for i, symbol in enumerate(symbols):
        if symbol in existing_symbols:
            log.info("   [%d/%d] Skipping %s (already have history)", i+1, len(symbols), symbol)
            continue
            
        log.info("   [%d/%d] Fetching history for %s...", i+1, len(symbols), symbol)
        try:
            resp = client.price_history(
                symbol, 
                periodType="year",
                period=5,  # Last 5 years: LEAPS needs 200+ trading days of warm-up
                frequencyType="daily"
            )
            resp.raise_for_status()
            candles = resp.json().get("candles", [])
            
            if not candles:
                log.warning("   No candles found for %s", symbol)
                continue
            
            bars = []
            for candle in candles:
                d = datetime.fromtimestamp(candle["datetime"] / 1000).date().isoformat()
                bars.append({
                    "ts": d,
                    "open": candle.get("open"),
                    "high": candle.get("high"),
                    "low": candle.get("low"),
                    "close": candle.get("close"),
                    "volume": candle.get("volume"),
                })
            
            count = ts_store.record_bars(symbol, "1d", bars)
            log.info("   Stored %d bars for %s", count, symbol)
        except Exception as e:
            log.exception("   Failed to fetch/store %s: %s", symbol, e)

def main():
    # Load config (env vars override secrets — see config_env.py)
    cfg = load_config()

    # Get authenticated Schwab client
    log.info("Authenticating with Schwab...")
    client = build_client(cfg)

    # Initialize time series store
    ts_store = TimeSeriesStore("./bot.db")

    # Get full universe from config
    log.info("Getting universe from config...")
    all_symbols = get_universe(client, cfg)
    log.info("Total universe size: %d symbols", len(all_symbols))
    
    # Optional: Use a subset of symbols for faster backtesting
    # symbols = all_symbols[:50]  # Uncomment to limit to first 50 symbols
    # log.info("Using subset of %d symbols for backtesting", len(symbols))
    symbols = all_symbols
    
    # Step 1: Fetch and store historical price data
    log.info("\nStep 1: Fetching historical price data...")
    fetch_and_store_history(client, symbols, ts_store)

    # Step 2: Verify what we have
    log.info("\nStep 2: Coverage summary:")
    coverage = ts_store.coverage_summary()
    log.info("   Have data for %d symbols total", len(coverage))
    # Filter symbols to only those in our subset that we actually have data for
    symbols_with_data = [s["symbol"] for s in coverage if s["symbol"] in symbols]
    log.info("   Will backtest %d symbols from subset", len(symbols_with_data))

    # Step 3: Run a single in-sample backtest (Priorities 5 & 7: adaptive
    # modules + commissions/spread gate wired in)
    log.info("\nStep 3: Running backtest (learning + sentiment overlay enabled)...")
    engine = BacktestEngine(
        cfg,
        ts_store,
        strategies=("credit_spreads", "directional"),
        chain_source="synthetic",  # Use "recorded" once you have real option chain snapshots
        approval_score_threshold=0.55,
        stop_loss_pct=2.0,       # Stop at 2x loss of entry credit
        take_profit_pct=0.5,     # Take profit at 50% of max profit
        starting_cash=cfg.get("paper", {}).get("starting_cash", 10000.0),
        use_learning=True,       # Priority 5: rule-based multiplier from realized win rate/expectancy
        use_sentiment=True,      # Priority 5: lookahead-safe macro regime overlay (see sentiment.HistoricalRegimeEngine)
        # commission_per_contract and max_spread_pct_skip (Priority 7) are
        # read from config.yaml's paper.commission_per_contract and
        # backtest.max_spread_pct_skip — set those there rather than here.
    )

    result = engine.run(
        symbols=symbols_with_data,
        start_date="2024-01-01",
        end_date="2025-01-01"
    )

    print("\n" + "="*60)
    print("BACKTEST RESULTS (in-sample, 2024)")
    print("="*60)
    print(result.summary())

    # Save trades to CSV
    result.trades_to_csv("backtest_trades.csv")
    log.info("\nTrades saved to backtest_trades.csv")

    # Step 4: Walk-forward validation (Priority 6) — checks whether the
    # config's parameter choices are overfit to the 2024 bull run by
    # rolling forward multiple train/test windows and only ever scoring
    # a window on data the tuning step never saw.
    log.info("\nStep 4: Running walk-forward validation...")
    wf_engine = BacktestEngine(
        cfg,
        ts_store,
        strategies=("credit_spreads", "directional"),
        chain_source="synthetic",
        approval_score_threshold=0.55,
        stop_loss_pct=2.0,
        take_profit_pct=0.5,
        starting_cash=cfg.get("paper", {}).get("starting_cash", 10000.0),
        use_learning=True,
        use_sentiment=True,
    )
    param_grid = {
        "strategies.credit_spreads.min_iv_rank": [40, 50, 60],
        "strategies.directional.volume_multiple_trigger": [1.5, 2.0, 2.5],
    }
    wf_result = run_walk_forward(
        wf_engine,
        symbols=symbols_with_data,
        start_date="2023-06-01",
        end_date="2026-07-01",
        train_window_days=180,    # 6 months in-sample
        test_window_days=90,      # 3 months out-of-sample
        step_days=30,             # roll forward monthly
        param_grid=param_grid,
        tune_on_train=True,
    )

    print("\n" + "="*60)
    print("WALK-FORWARD VALIDATION RESULTS")
    print("="*60)
    print(wf_result.summary())
    print(
        "\nCompare this to the single in-sample 2024 result above. If the "
        "walk-forward (out-of-sample) numbers are meaningfully worse — "
        "lower win rate, negative P&L, bigger drawdown — the in-sample "
        "result was likely overfit to the 2024 bull run rather than real edge."
    )


if __name__ == "__main__":
    main()
