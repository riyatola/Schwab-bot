"""
Wheel strategy scanner: two halves.
  1. Cash-secured puts on names in your watchlist you'd genuinely be OK
     owning at the strike (this still requires YOU to curate that list —
     see cfg["universe"]["wheel_watchlist"], separate from the broad scan
     universe, since "would I want to own this" isn't a screenable metric).
  2. Covered calls on stock you already hold (pulled from live positions).
"""

import logging
from strategies.base import nearest_by_delta, occ_symbol, dte, filter_liquid_chain

log = logging.getLogger("strategies.wheel")


def scan(client, account_positions: list[dict], cfg: dict, market_data) -> list[dict]:
    s_cfg = cfg["strategies"]["wheel"]
    watchlist = cfg["universe"].get("wheel_watchlist", [])
    candidates = []

    # -- Cash-secured puts on the curated watchlist --
    max_spread_pct = s_cfg.get("max_leg_spread_pct", 0.20)
    for symbol in watchlist:
        chain = market_data.get_option_chain(symbol, s_cfg["dte_min"], s_cfg["dte_max"])
        for expiration, sides in chain.items():
            sides = filter_liquid_chain(sides, max_spread_pct)
            days = dte(expiration)
            put = nearest_by_delta(sides["puts"], s_cfg["csp_target_delta"])
            if not put:
                continue
            credit = round((put["bid"] + put["ask"]) / 2, 2)
            candidates.append({
                "symbol": symbol,
                "strategy": "wheel",
                "description": f"Cash-secured put, delta ~{put['delta']:.2f}",
                "legs_summary": f"Sell {put['strike']}P",
                "legs": [{
                    "occ_symbol": occ_symbol(symbol, expiration, "P", put["strike"]),
                    "instruction": "SELL_TO_OPEN", "ratio": 1,
                }],
                "expiration": expiration,
                "dte": days,
                "est_price": credit,
                "is_credit": True,
                "max_loss_per_contract": round((put["strike"] * 100) - (credit * 100), 2),
                "score": 0.5 + min(credit / put["strike"], 0.1) * 5,  # more yield = higher score, capped
                "rationale": f"Willing-to-own watchlist name, delta {put['delta']:.2f}, "
                             f"annualized yield ~{(credit / put['strike']) * (365 / max(days, 1)):.1%}",
            })

    # -- Covered calls on existing long stock positions --
    for pos in account_positions:
        if pos.get("assetType") != "EQUITY" or pos.get("longQuantity", 0) < 100:
            continue
        symbol = pos["symbol"]
        chain = market_data.get_option_chain(symbol, s_cfg["dte_min"], s_cfg["dte_max"])
        cost_basis = pos.get("averagePrice", 0)
        for expiration, sides in chain.items():
            sides = filter_liquid_chain(sides, max_spread_pct)
            days = dte(expiration)
            call = nearest_by_delta(sides["calls"], s_cfg["covered_call_target_delta"])
            if not call or call["strike"] < cost_basis:
                continue  # don't sell calls below your own cost basis
            credit = round((call["bid"] + call["ask"]) / 2, 2)
            candidates.append({
                "symbol": symbol,
                "strategy": "wheel",
                "description": f"Covered call, delta ~{call['delta']:.2f}, above cost basis {cost_basis:.2f}",
                "legs_summary": f"Sell {call['strike']}C",
                "legs": [{
                    "occ_symbol": occ_symbol(symbol, expiration, "C", call["strike"]),
                    "instruction": "SELL_TO_OPEN", "ratio": 1,
                }],
                "expiration": expiration,
                "dte": days,
                "est_price": credit,
                "is_credit": True,
                "max_loss_per_contract": 0.0,  # covered by existing shares; opportunity cost, not cash loss
                "score": 0.5 + min(credit / call["strike"], 0.1) * 5,
                "rationale": f"Existing {int(pos['longQuantity'])}-share position, "
                             f"strike above cost basis, delta {call['delta']:.2f}",
            })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates
