"""
Directional (long call/put) scanner. Requires trend + a volume breakout
before flagging a long option — the point is to avoid buying premium on
noise. IV rank window avoids buying options when implied vol is itself
already expensive (you'd be fighting IV crush even if direction is right).
"""

import logging
from strategies.base import dte, nearest_by_delta, occ_symbol, filter_liquid_chain

log = logging.getLogger("strategies.directional")


def scan(client, symbols: list[str], cfg: dict, market_data) -> list[dict]:
    """
    market_data additionally exposes:
      - get_volume_ratio(symbol) -> today's volume / N-day avg volume
      - get_trend_strength(symbol, lookback_days) -> float in [-1, 1],
        negative = downtrend, positive = uptrend, magnitude = strength
    """
    s_cfg = cfg["strategies"]["directional"]
    candidates = []

    for symbol in symbols:
        rank = market_data.get_iv_rank(symbol)
        if not (s_cfg["min_iv_rank"] <= rank <= s_cfg["max_iv_rank"]):
            continue

        vol_ratio = market_data.get_volume_ratio(symbol)
        if vol_ratio < s_cfg["volume_multiple_trigger"]:
            continue

        trend_strength = market_data.get_trend_strength(symbol, s_cfg["trend_lookback_days"])
        if abs(trend_strength) < 0.2:
            continue  # not enough conviction either direction

        # Add RSI filter if enabled in config
        if s_cfg.get("use_rsi_filter", False):
            rsi = market_data.get_rsi(symbol)
            rsi_min = s_cfg.get("rsi_min", 55)
            if trend_strength > 0:  # long call - require RSI above minimum threshold
                if rsi < rsi_min:
                    continue
            else:  # long put - require RSI below inverse threshold (RSI < 45 for default rsi_min=55)
                if rsi > (100 - rsi_min):
                    continue

        chain = market_data.get_option_chain(symbol, s_cfg["dte_min"], s_cfg["dte_max"])
        max_spread_pct = s_cfg.get("max_leg_spread_pct", 0.20)
        for expiration, sides in chain.items():
            sides = filter_liquid_chain(sides, max_spread_pct)
            days = dte(expiration)
            option_type = "call" if trend_strength > 0 else "put"
            options = sides["calls"] if option_type == "call" else sides["puts"]
            # slightly OTM, delta ~0.4, for better leverage than deep ITM
            leg = nearest_by_delta(options, 0.40)
            if not leg:
                continue

            mid = round((leg["bid"] + leg["ask"]) / 2, 2)
            score = min(1.0, abs(trend_strength) * 0.6 + min(vol_ratio / 3, 1.0) * 0.4)

            candidates.append({
                "symbol": symbol,
                "strategy": "directional",
                "description": f"Long {option_type} on breakout, trend strength {trend_strength:+.2f}",
                "legs_summary": f"Buy {leg['strike']}{option_type[0].upper()}",
                "legs": [{
                    "occ_symbol": occ_symbol(symbol, expiration, option_type[0], leg["strike"]),
                    "instruction": "BUY_TO_OPEN", "ratio": 1,
                }],
                "expiration": expiration,
                "dte": days,
                "est_price": mid,
                "is_credit": False,
                "max_loss_per_contract": round(mid * 100, 2),
                "score": score,
                "rationale": (f"Volume {vol_ratio:.1f}x avg, trend {trend_strength:+.2f}, "
                              f"IV rank {rank:.0f} (in acceptable range)"),
            })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates
