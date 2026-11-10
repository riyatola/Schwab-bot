"""
Config loading with environment-variable overrides.

`config.yaml` holds every tunable (and, locally, your secrets). On a hosted
platform (Render, Docker, systemd) the secrets are usually supplied as
environment variables rather than a committed file, while the non-secret
config comes from a mounted `config.yaml` (e.g. a Render "Secret File").

Precedence (highest first):
    1. environment variables (secrets + mode)
    2. the YAML file at CONFIG_PATH / config.yaml
    3. per-caller defaults for anything still missing

Kept dependency-light (os + yaml only) on purpose: CLI helpers like
manual_auth.py can import it without pulling in the full scanner, which
imports apscheduler / telegram / etc.
"""

import logging
import os

import yaml

log = logging.getLogger("config_env")

# env var -> (dotted path into the config dict, type)
_ENV_OVERRIDES = [
    ("BOT_MODE", "mode", str),
    ("SCHWAB_APP_KEY", "schwab.app_key", str),
    ("SCHWAB_APP_SECRET", "schwab.app_secret", str),
    ("SCHWAB_CALLBACK_URL", "schwab.callback_url", str),
    ("SCHWAB_ACCOUNT_INDEX", "schwab.account_index", int),
    ("SCHWAB_TOKEN_PATH", "schwab.token_path", str),
    ("TELEGRAM_BOT_TOKEN", "telegram.bot_token", str),
    ("TELEGRAM_CHAT_ID", "telegram.chat_id", str),
    ("FINNHUB_API_KEY", "earnings.finnhub_api_key", str),
    ("FRED_API_KEY", "fred.api_key", str),
]


def _set(cfg: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    for part in parts[:-1]:
        cfg = cfg.setdefault(part, {})
    cfg[parts[-1]] = value


def apply_env_overrides(cfg: dict, environ=None) -> dict:
    """Overlay environment variables onto an already-loaded config dict."""
    environ = os.environ if environ is None else environ
    for var, path, cast in _ENV_OVERRIDES:
        raw = environ.get(var)
        if raw is None or raw == "":
            continue
        try:
            value = cast(raw)
        except (TypeError, ValueError):
            log.warning("Ignoring %s=%r — not a valid %s", var, raw, cast.__name__)
            continue
        _set(cfg, path, value)
        log.info("config override from env: %s -> %s", var, path)
    return cfg


def load_config(path: str | None = None) -> dict:
    """Load config.yaml (path from `path` or the CONFIG_PATH env var) and
    apply environment-variable overrides. A missing file is not fatal — the
    app degrades to env-only config."""
    path = path or os.environ.get("CONFIG_PATH") or "config.yaml"
    cfg = {}
    if os.path.exists(path):
        with open(path) as f:
            cfg = yaml.safe_load(f) or {}
    else:
        log.warning("Config file %r not found — relying on environment variables only.", path)
    return apply_env_overrides(cfg)
