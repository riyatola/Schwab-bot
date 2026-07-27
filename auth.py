"""
Thin wrapper around schwabdev for OAuth + client construction.

First run will open a browser-based OAuth flow (via schwabdev's built-in
callback capture on 127.0.0.1). After that, refresh tokens are cached in
the path set by schwab.token_path in config.yaml and refreshed automatically.

`mode: paper` (default) in config.yaml wraps the real client in
PaperBrokerClient (see paper_broker.py) — real market data, simulated
fills and account state, nothing ever routed to Schwab's order endpoint.
`mode: live` returns the real client untouched. That's the entire switch;
everything downstream (market_data.py, risk_manager.py, executor.py,
trade_tracker.py) is unaware of which one it's holding, since
PaperBrokerClient duck-types the same method surface.

Defaults to paper mode deliberately — you have to opt into live trading
by explicitly setting `mode: live`, not the other way around.
"""

import schwabdev
import logging

from paper_broker import PaperBrokerClient

log = logging.getLogger("auth")


def build_client(cfg: dict):
    schwab_cfg = cfg["schwab"]
    token_path = schwab_cfg.get("token_path", "./tokens.json")
    try:
        # Try with tokens_db (schwabdev 3.0.5+)
        real_client = schwabdev.Client(
            app_key=schwab_cfg["app_key"],
            app_secret=schwab_cfg["app_secret"],
            callback_url=schwab_cfg.get("callback_url", "https://127.0.0.1"),
            tokens_db=token_path,
        )
    except TypeError:
        # Try with tokens_file (older versions)
        try:
            real_client = schwabdev.Client(
                app_key=schwab_cfg["app_key"],
                app_secret=schwab_cfg["app_secret"],
                callback_url=schwab_cfg.get("callback_url", "https://127.0.0.1"),
                tokens_file=token_path,
            )
        except TypeError:
            # Try with token_path (newer versions before 3.0.5)
            try:
                real_client = schwabdev.Client(
                    app_key=schwab_cfg["app_key"],
                    app_secret=schwab_cfg["app_secret"],
                    callback_url=schwab_cfg.get("callback_url", "https://127.0.0.1"),
                    token_path=token_path,
                )
            except TypeError:
                # Fallback: positional args
                real_client = schwabdev.Client(
                    schwab_cfg["app_key"],
                    schwab_cfg["app_secret"],
                    schwab_cfg.get("callback_url", "https://127.0.0.1")
                )

    mode = cfg.get("mode", "paper")
    if mode == "live":
        log.warning("mode=live — orders WILL be routed to your real Schwab account.")
        return real_client

    if mode != "paper":
        raise ValueError(f"Unknown mode '{mode}' in config.yaml — expected 'paper' or 'live'.")

    db_path = cfg.get("schwab", {}).get("paper_db_path", "./bot.db")
    log.info("mode=paper — using PaperBrokerClient (real market data, simulated fills).")
    return PaperBrokerClient(real_client, cfg, db_path=db_path)


def get_account_hash(client, account_index: int = 0) -> str:
    """Schwab's API addresses accounts by an opaque hash, not the raw account number.
    Works unchanged for PaperBrokerClient, which returns a single fake account."""
    resp = client.linked_accounts()
    resp.raise_for_status()
    accounts = resp.json()
    if not accounts:
        raise RuntimeError("No linked Schwab accounts found for this app.")
    return accounts[account_index]["hashValue"]
