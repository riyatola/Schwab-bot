"""
Credit spread scanner: looks for defined-risk vertical spreads (put credit
spreads in uptrends / neutral names, call credit spreads in downtrends /
neutral names) on elevated implied volatility.

Rule set (tune in config.yaml under strategies.credit_spreads):
  - IV rank >= min_iv_rank: only sell premium when it's rich relative to
    the name's own 52-week IV range, not in absolute terms.
  - Short strike near target_short_delta (defaults to ~0.20, i.e. roughly
    an 80% probability of expiring OTM at entry — no guarantee, deltas are
    a rough proxy, not a true probability).
  - credit / width >= min_credit_to_width_ratio: reject spreads where
    you're risking a lot to make a little.
  - DTE window avoids very short-dated (gamma risk) and very long-dated
    (capital tied up, less theta efficiency) spreads.
"""

import logging
from strategies.base import dte, iv_rank, occ_symbol, nearest_by_delta, filter_liquid_chain

log = logging.getLogger("strategies.credit_spreads")


def scan(client, symbols: list[str], cfg: dict, market_data) -> list[dict]:
    """
    market_data: an adapter object exposing:
      - get_iv_rank(symbol) -> float
      - get_trend(symbol) -> "up" | "down" | "neutral"
      - get_option_chain(symbol, dte_min, dte_max) -> dict keyed by expiration,
        each value {"calls": [...], "puts": [...]} with delta/strike/bid/ask
    This keeps strategy modules testable without hammering the live API.
    """
    s_cfg = cfg["strategies"]["credit_spreads"]
    candidates = []

    for symbol in symbols:
        rank = market_data.get_iv_rank(symbol)
        if rank < s_cfg["min_iv_rank"]:
            continue

        trend = market_data.get_trend(symbol)
        chain = market_data.get_option_chain(symbol, s_cfg["dte_min"], s_cfg["dte_max"])

        max_spread_pct = s_cfg.get("max_leg_spread_pct", 0.20)
        for expiration, sides in chain.items():
            sides = filter_liquid_chain(sides, max_spread_pct)
            days = dte(expiration)

            if trend in ("up", "neutral"):
                candidate = _build_put_credit_spread(symbol, expiration, days, sides["puts"], s_cfg, rank)
                if candidate:
                    candidates.append(candidate)

            if trend in ("down", "neutral"):
                candidate = _build_call_credit_spread(symbol, expiration, days, sides["calls"], s_cfg, rank)
                if candidate:
                    candidates.append(candidate)

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates


def _build_put_credit_spread(symbol, expiration, days, puts, s_cfg, rank):
    short = nearest_by_delta(puts, s_cfg["target_short_delta"])
    if not short:
        return None
    long_candidates = [p for p in puts if p["strike"] < short["strike"]]
    if not long_candidates:
        return None
    long_leg = max(long_candidates, key=lambda p: p["strike"])  # closest strike below short

    width = short["strike"] - long_leg["strike"]
    credit = round((short["bid"] - long_leg["ask"]), 2)
    if width <= 0 or credit <= 0:
        return None
    ratio = credit / width
    base_ratio = s_cfg["min_credit_to_width_ratio"]
    # scale: if rank > 50, ratio requirement increases; if rank < 50, it decreases
    required_ratio = base_ratio * (rank / 50.0)
    if ratio < required_ratio:
        return None

    max_loss = round((width - credit) * 100, 2)
    score = min(1.0, (rank / 100) * 0.6 + min(ratio, 1.0) * 0.4)

    return {
        "symbol": symbol,
        "strategy": "credit_spreads",
        "description": f"Put credit spread, short delta ~{short['delta']:.2f}",
        "legs_summary": f"Sell {short['strike']}P / Buy {long_leg['strike']}P",
        "legs": [
            {"occ_symbol": occ_symbol(symbol, expiration, "P", short["strike"]),
             "instruction": "SELL_TO_OPEN", "ratio": 1},
            {"occ_symbol": occ_symbol(symbol, expiration, "P", long_leg["strike"]),
             "instruction": "BUY_TO_OPEN", "ratio": 1},
        ],
        "expiration": expiration,
        "dte": days,
        "est_price": credit,
        "is_credit": True,
        "max_loss_per_contract": max_loss,
        "score": score,
        "rationale": f"IV rank {rank:.0f}, credit/width {ratio:.2f}, short delta {short['delta']:.2f}",
    }


def _build_call_credit_spread(symbol, expiration, days, calls, s_cfg, rank):
    short = nearest_by_delta(calls, s_cfg["target_short_delta"])
    if not short:
        return None
    long_candidates = [c for c in calls if c["strike"] > short["strike"]]
    if not long_candidates:
        return None
    long_leg = min(long_candidates, key=lambda c: c["strike"])

    width = long_leg["strike"] - short["strike"]
    credit = round((short["bid"] - long_leg["ask"]), 2)
    if width <= 0 or credit <= 0:
        return None
    ratio = credit / width
    if ratio < s_cfg["min_credit_to_width_ratio"]:
        return None

    max_loss = round((width - credit) * 100, 2)
    score = min(1.0, (rank / 100) * 0.6 + min(ratio, 1.0) * 0.4)

    return {
        "symbol": symbol,
        "strategy": "credit_spreads",
        "description": f"Call credit spread, short delta ~{short['delta']:.2f}",
        "legs_summary": f"Sell {short['strike']}C / Buy {long_leg['strike']}C",
        "legs": [
            {"occ_symbol": occ_symbol(symbol, expiration, "C", short["strike"]),
             "instruction": "SELL_TO_OPEN", "ratio": 1},
            {"occ_symbol": occ_symbol(symbol, expiration, "C", long_leg["strike"]),
             "instruction": "BUY_TO_OPEN", "ratio": 1},
        ],
        "expiration": expiration,
        "dte": days,
        "est_price": credit,
        "is_credit": True,
        "max_loss_per_contract": max_loss,
        "score": score,
        "rationale": f"IV rank {rank:.0f}, credit/width {ratio:.2f}, short delta {short['delta']:.2f}",
    }
