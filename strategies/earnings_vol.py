"""
Earnings volatility scanner: flags straddle/strangle candidates going into
earnings when the options-implied move looks cheap or rich relative to the
stock's own historical realized move on past earnings days.

This is a two-sided tool: "min_expected_move_edge" can flag either a long
straddle (implied move looks too cheap vs history — options underpricing
the event) or noting the same edge in the opposite direction could support
a short-vol play (selling the straddle/strangle), which carries undefined
risk and isn't included here by default — long straddles/strangles only,
since they're defined-risk.
"""

import logging
from strategies.base import dte, nearest_by_delta, occ_symbol, filter_liquid_chain

log = logging.getLogger("strategies.earnings_vol")


def scan(client, symbols: list[str], cfg: dict, market_data) -> list[dict]:
    """
    market_data additionally exposes:
      - get_earnings_date(symbol) -> date or None
      - get_historical_earnings_move(symbol) -> float (avg abs % move, e.g. 0.06 = 6%)
    """
    s_cfg = cfg["strategies"]["earnings_vol"]
    candidates = []

    for symbol in symbols:
        earnings_date = market_data.get_earnings_date(symbol)
        if not earnings_date:
            continue
        days_to_earnings = (earnings_date - __import__("datetime").date.today()).days
        if not (0 <= days_to_earnings <= s_cfg["days_before_earnings"]):
            continue

        historical_move = market_data.get_historical_earnings_move(symbol)
        if not historical_move:
            continue

        chain = market_data.get_option_chain(symbol, s_cfg["dte_min"], s_cfg["dte_max"])
        max_spread_pct = s_cfg.get("max_leg_spread_pct", 0.20)
        for expiration, sides in chain.items():
            sides = filter_liquid_chain(sides, max_spread_pct)
            days = dte(expiration)
            atm_call = nearest_by_delta(sides["calls"], 0.50)
            atm_put = nearest_by_delta(sides["puts"], 0.50)
            if not atm_call or not atm_put:
                continue

            # Check both legs' spreads individually
            call_spread_ok = (atm_call["bid"] and atm_call["ask"])
            put_spread_ok = (atm_put["bid"] and atm_put["ask"])
            if call_spread_ok and put_spread_ok:
                call_mid = (atm_call["bid"] + atm_call["ask"]) / 2
                put_mid = (atm_put["bid"] + atm_put["ask"]) / 2
                call_spread_pct = (atm_call["ask"] - atm_call["bid"]) / call_mid if call_mid else 1.0
                put_spread_pct = (atm_put["ask"] - atm_put["bid"]) / put_mid if put_mid else 1.0
                if call_spread_pct > max_spread_pct or put_spread_pct > max_spread_pct:
                    continue
            else:
                continue

            straddle_price = round((atm_call["bid"] + atm_call["ask"]) / 2
                                    + (atm_put["bid"] + atm_put["ask"]) / 2, 2)
            underlying_price = market_data.get_last_price(symbol)
            implied_move_pct = straddle_price / underlying_price if underlying_price else None
            if implied_move_pct is None:
                continue

            edge = historical_move - implied_move_pct  # positive = options look cheap vs history
            if edge < s_cfg["min_expected_move_edge"]:
                continue

            avg_surprise = market_data.get_historical_earnings_surprise(symbol)
            # Adjust score slightly if we have surprise data
            surprise_multiplier = 1.0
            if avg_surprise is not None:
                # If average surprise is 10% or more, boost score by 10%
                if avg_surprise >= 0.10:
                    surprise_multiplier = 1.10

            score = min(1.0, (edge / 0.20) * surprise_multiplier)  # normalize; tune denominator as you observe real edges

            candidates.append({
                "symbol": symbol,
                "strategy": "earnings_vol",
                "description": f"Long straddle into earnings ({earnings_date.isoformat()})",
                "legs_summary": f"Buy {atm_call['strike']}C + Buy {atm_put['strike']}P",
                "legs": [
                    {"occ_symbol": occ_symbol(symbol, expiration, "C", atm_call["strike"]),
                     "instruction": "BUY_TO_OPEN", "ratio": 1},
                    {"occ_symbol": occ_symbol(symbol, expiration, "P", atm_put["strike"]),
                     "instruction": "BUY_TO_OPEN", "ratio": 1},
                ],
                "expiration": expiration,
                "dte": days,
                "est_price": straddle_price,
                "is_credit": False,
                "max_loss_per_contract": round(straddle_price * 100, 2),
                "score": score,
                "rationale": (f"Implied move {implied_move_pct:.1%} vs historical avg "
                              f"{historical_move:.1%} (edge {edge:.1%})"),
            })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates
