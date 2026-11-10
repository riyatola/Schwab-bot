"""
LEAPS scanner — phase 1: the "stock replacement" long call.

A LEAP is nothing exotic: it's an ordinary listed option with more than
~1 year to expiration (many underlyings only list them in January). This
module looks for deep in-the-money calls (delta ~0.80, 12-24 months out)
that cost materially less than 100 shares while still capturing most of
the underlying's move — a low-extrinsic, hold-for-months cousin of
directional.py's ~0.40-delta momentum trade. It is deliberately NOT a
lottery ticket: a deep-ITM LEAP is mostly intrinsic value, so little of
the premium is exposed to theta or IV swings; the cost of carry (interest
on the strike minus dividends) plus a slice of residual volatility value
is what you pay for the leverage.

Same contract as every other strategy module:

    scan(client, symbols, cfg, market_data) -> list[dict]

Nothing here auto-executes. Candidates route through the normal
learning -> sentiment -> risk -> approval -> executor pipeline, exactly
like the other four strategies, and need a human tap.

Filter order (section 4.1 of the spec) is deliberately cheapest-first —
DB/indicator reads before any chain fetch — to protect Schwab rate limits
across a large universe. Call-side only in phase 1 (a downtrend means
skip). PMCC overlays and roll/exit logic are phase 2/3 (see the spec).
"""

import logging
from datetime import date

from strategies.base import dte, nearest_by_delta, occ_symbol, filter_liquid_chain

log = logging.getLogger("strategies.leaps")

PHASE1_VARIANT = "stock_replacement"


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def scan(client, symbols: list[str], cfg: dict, market_data) -> list[dict]:
    """market_data additionally exposes:
      - get_sma(symbol, n) -> float | None  (simple moving average of daily closes)
      - get_iv_rank / get_trend_strength / get_last_price / get_earnings_date
        (already used by the other strategies)
    """
    s_cfg = cfg["strategies"]["leaps"]
    universe_cfg = cfg["universe"]
    max_fetches = s_cfg.get("max_chain_fetches_per_cycle", 60)

    # -- cheap prefilter + rank by trend strength (no chain fetches yet) --
    ranked = []
    for symbol in symbols:
        rank = market_data.get_iv_rank(symbol)
        if not (s_cfg["min_iv_rank"] <= rank <= s_cfg["max_iv_rank"]):
            continue  # high vega: don't buy long-dated premium when IV is already rich
        trend = market_data.get_trend_strength(symbol, s_cfg["trend_lookback_days"])
        if trend < s_cfg["min_trend_strength"]:
            continue  # phase 1 is call-only; a weak/down trend means skip
        ranked.append((trend, symbol, rank))

    # Strongest trends first, so a per-cycle chain-fetch cap only ever
    # defers the weakest setups rather than cutting off arbitrarily.
    ranked.sort(key=lambda t: t[0], reverse=True)

    candidates = []
    chain_fetches = 0
    for i, (trend, symbol, rank) in enumerate(ranked):
        if chain_fetches >= max_fetches:
            log.info("LEAPS: reached max_chain_fetches_per_cycle (%d), deferring %d remaining symbol(s)",
                     max_fetches, len(ranked) - i)
            break

        last_price = market_data.get_last_price(symbol)
        if not last_price:
            continue
        if not (universe_cfg.get("min_underlying_price", 0) <= last_price <= universe_cfg.get("max_underlying_price", float("inf"))):
            continue

        # Above the long-term trend, and not chasing a blow-off extension.
        sma200 = market_data.get_sma(symbol, 200)
        if sma200 is None or last_price <= sma200:
            continue
        sma50 = market_data.get_sma(symbol, 50)
        if sma50 and last_price > (1 + s_cfg["max_extension_pct"]) * sma50:
            continue

        avoid_days = s_cfg.get("avoid_earnings_within_days", 0)
        if avoid_days:
            earnings_date = market_data.get_earnings_date(symbol)
            if earnings_date:
                days_to_earnings = (earnings_date - date.today()).days
                if 0 <= days_to_earnings <= avoid_days:
                    continue

        chain = market_data.get_option_chain(symbol, s_cfg["dte_min"], s_cfg["dte_max"])
        chain_fetches += 1
        if not chain:
            continue  # name doesn't list LEAPS in the window — nothing to do

        # Best-scoring expiration per symbol only (avoid alert spam).
        best = None
        for expiration, sides in chain.items():
            days = dte(expiration)
            if not (s_cfg["dte_min"] <= days <= s_cfg["dte_max"]):
                continue

            sides = filter_liquid_chain(sides, s_cfg["max_leg_spread_pct"])
            calls = sides["calls"]
            min_oi = s_cfg.get("min_open_interest", 0)
            if min_oi:
                calls = [c for c in calls if (c.get("open_interest") or 0) >= min_oi]
            if not calls:
                continue

            leg = nearest_by_delta(calls, s_cfg["target_delta"])
            if not leg:
                continue
            candidate = _build_candidate(symbol, expiration, days, leg, last_price, trend, rank, s_cfg)
            if candidate and (best is None or candidate["score"] > best["score"]):
                best = candidate

        if best:
            candidates.append(best)

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates


def _build_candidate(symbol, expiration, days, leg, spot, trend, iv_rank_val, s_cfg):
    """Apply the section 4.2 hard rejects, compute the section 1 metrics,
    score the setup (section 4.4), and return the candidate dict (4.3) —
    or None if it fails any hard filter."""
    bid, ask = leg.get("bid"), leg.get("ask")
    if not bid or not ask or bid <= 0 or ask <= 0:
        return None

    delta = leg.get("delta", 0.0) or 0.0
    if not (s_cfg["min_delta"] <= abs(delta) <= s_cfg["max_delta"]):
        return None

    strike = leg["strike"]
    mid = (bid + ask) / 2
    intrinsic = max(spot - strike, 0.0)
    extrinsic = mid - intrinsic

    # Sanity: a crossed/stale/low-liquidity quote shows up as a mid below
    # intrinsic or materially negative extrinsic. Reject either.
    if mid < intrinsic * 0.98:
        return None
    if extrinsic < -0.02 * spot:
        return None

    ann_ext = (max(extrinsic, 0.0) / spot) * (365 / max(days, 1)) if spot else None
    if ann_ext is None or ann_ext > s_cfg["max_annualized_extrinsic_pct"]:
        return None

    premium_per_contract = mid * 100
    max_prem = s_cfg.get("max_premium_per_contract_dollars")
    if max_prem and premium_per_contract > max_prem:
        return None

    spread_pct = (ask - bid) / mid if mid else 1.0
    breakeven = strike + mid
    be_move = (breakeven - spot) / spot if spot else 0.0
    capital_saved = 1 - (mid / spot) if spot else 0.0
    delta_leverage = (spot * abs(delta)) / mid if mid else 0.0

    # -- section 4.4 scoring (0..1; learning/sentiment multipliers apply later) --
    trend_score = min(1.0, trend / 0.6)
    carry_score = 1 - min(1.0, ann_ext / s_cfg["max_annualized_extrinsic_pct"])
    iv_score = 1 - _clamp(iv_rank_val / s_cfg["max_iv_rank"], 0, 1) if s_cfg["max_iv_rank"] else 1.0
    liq_score = 1 - min(1.0, spread_pct / s_cfg["max_leg_spread_pct"]) if s_cfg["max_leg_spread_pct"] else 1.0
    delta_fit = 1 - min(1.0, abs(abs(delta) - s_cfg["target_delta"]) / 0.15)
    score = (0.30 * trend_score + 0.25 * carry_score + 0.20 * iv_score
             + 0.15 * liq_score + 0.10 * delta_fit)
    score = round(_clamp(score, 0.0, 1.0), 3)
    if score < s_cfg.get("min_score", 0.55):
        return None

    rationale = (
        f"Uptrend {trend:+.2f}, above 200DMA, IV rank {iv_rank_val:.0f}, delta {delta:.2f}, "
        f"extrinsic {ann_ext:.1%}/yr of spot, breakeven {be_move:+.1%}, saves {capital_saved:.0%} vs shares"
    )

    return {
        "symbol": symbol,
        "strategy": "leaps",
        "variant": s_cfg.get("variant", PHASE1_VARIANT),
        "description": f"Deep-ITM LEAP call, delta {delta:.2f}, {days} DTE",
        "legs_summary": f"Buy {strike}C {expiration}",
        "legs": [{
            "occ_symbol": occ_symbol(symbol, expiration, "C", strike),
            "instruction": "BUY_TO_OPEN", "ratio": 1,
        }],
        "expiration": expiration,
        "dte": days,
        "est_price": round(mid, 2),
        "is_credit": False,
        "max_loss_per_contract": round(mid * 100, 2),
        "score": score,
        "rationale": rationale,
        # extras (additive; used by the alert and by learning features)
        "delta": round(delta, 3),
        "extrinsic": round(max(extrinsic, 0.0), 3),
        "annualized_extrinsic_pct": round(ann_ext, 4),
        "breakeven": round(breakeven, 2),
        "breakeven_move_pct": round(be_move, 4),
        "capital_saved_pct": round(capital_saved, 4),
        "delta_leverage": round(delta_leverage, 2),
    }
