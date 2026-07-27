"""
Black-Scholes option pricing + synthetic chain generation.

Used by backtest_engine.py ONLY as a fallback for dates that have no real
recorded option_chain_snapshot (see timeseries.py). This is a theoretical-
value approximation, not real market data — no skew, no term structure,
no liquidity/spread effects, no early-exercise premium on American options.
Good for checking strategy logic behaves sensibly; not good for estimating
real historical P&L. See timeseries.py's module docstring for the full
explanation of why real historical option chains generally aren't available
from retail broker APIs at all.
"""

import math
from datetime import date, datetime


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def bs_price_and_delta(spot: float, strike: float, dte_days: int, iv: float,
                        option_type: str, r: float = 0.045, q: float = 0.0):
    """European Black-Scholes price + delta. r: risk-free rate (annualized),
    q: dividend yield (annualized). dte_days: calendar days to expiration.
    Returns (price, delta). At dte_days<=0, returns intrinsic value and a
    delta of 1.0/-1.0/0.0 depending on moneyness."""
    t = max(dte_days, 0) / 365.0
    if t <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        if option_type == "call":
            intrinsic = max(0.0, spot - strike)
            delta = 1.0 if spot > strike else 0.0
        else:
            intrinsic = max(0.0, strike - spot)
            delta = -1.0 if spot < strike else 0.0
        return intrinsic, delta

    d1 = (math.log(spot / strike) + (r - q + 0.5 * iv * iv) * t) / (iv * math.sqrt(t))
    d2 = d1 - iv * math.sqrt(t)

    if option_type == "call":
        price = spot * math.exp(-q * t) * _norm_cdf(d1) - strike * math.exp(-r * t) * _norm_cdf(d2)
        delta = math.exp(-q * t) * _norm_cdf(d1)
    else:
        price = strike * math.exp(-r * t) * _norm_cdf(-d2) - spot * math.exp(-q * t) * _norm_cdf(-d1)
        delta = -math.exp(-q * t) * _norm_cdf(-d1)

    return max(price, 0.0), delta


def synthetic_chain(spot: float, as_of: date, expirations: list[date], iv_estimate: float,
                     strike_pct_range: float = 0.25, strike_step_pct: float = 0.025,
                     bid_ask_spread_pct: float = 0.03) -> dict:
    """Builds a fake-but-internally-consistent option chain shaped exactly
    like MarketDataAdapter.get_option_chain's output, so it's a drop-in
    substitute for strategy modules during backtesting.

    strike_pct_range/strike_step_pct: strikes generated from
    spot*(1-range) to spot*(1+range) in range*step increments.
    bid_ask_spread_pct: synthetic spread applied around the theoretical
    mid, since Black-Scholes only gives you a single fair value.
    iv_estimate: flat IV applied across the whole chain (no skew/term
    structure modeled — a real chain has both; see module docstring).
    """
    chain = {}
    strikes = []
    k = spot * (1 - strike_pct_range)
    top = spot * (1 + strike_pct_range)
    step = spot * strike_step_pct
    while k <= top:
        strikes.append(round(k, 2))
        k += step

    for exp in expirations:
        dte_days = (exp - as_of).days
        if dte_days < 0:
            continue
        calls, puts = [], []
        for strike in strikes:
            for option_type, bucket in (("call", calls), ("put", puts)):
                mid, delta = bs_price_and_delta(spot, strike, dte_days, iv_estimate, option_type)
                half_spread = max(0.01, mid * bid_ask_spread_pct / 2)
                bucket.append({
                    "strike": strike,
                    "bid": round(max(0.0, mid - half_spread), 2),
                    "ask": round(mid + half_spread, 2),
                    "delta": round(delta, 4),
                    "iv": iv_estimate,
                    "symbol": None,  # filled in by the backtest engine's occ_symbol() call if needed
                })
        chain[exp.isoformat()] = {"calls": calls, "puts": puts}
    return chain


def realized_vol_estimate(closes: list[float], window: int = 20) -> float:
    """Simple trailing annualized realized-vol estimate off daily closes,
    used as the IV proxy when no recorded IV history exists — same
    approach market_data.py already uses as its own fallback, reused here
    so the backtest's synthetic pricing behaves consistently with how the
    live bot would behave on a symbol without IV history yet."""
    if len(closes) < window + 1:
        return 0.30  # reasonable generic default if there's not enough history
    window_closes = closes[-(window + 1):]
    log_returns = [math.log(window_closes[i] / window_closes[i - 1]) for i in range(1, len(window_closes))]
    mean = sum(log_returns) / len(log_returns)
    var = sum((r - mean) ** 2 for r in log_returns) / max(1, len(log_returns) - 1)
    return math.sqrt(var) * math.sqrt(252)
