"""
Shared helpers for strategy modules. Each strategy module implements
scan(client, symbols, cfg) -> list[dict] returning candidate dicts shaped like:

{
    "symbol": "XYZ",
    "strategy": "credit_spreads",
    "description": "...",
    "legs_summary": "Sell 100C / Buy 105C",
    "legs": [ {occ_symbol, instruction, ratio}, ... ],
    "expiration": "2026-08-21",
    "dte": 40,
    "est_price": 1.35,
    "is_credit": True,
    "max_loss_per_contract": 365.0,
    "score": 0.82,
    "rationale": "IV rank 68, short delta 0.19, credit/width 0.31",
}
"""

from datetime import datetime, date


def dte(expiration_str: str) -> int:
    exp = datetime.strptime(expiration_str, "%Y-%m-%d").date()
    return (exp - date.today()).days


def iv_rank(current_iv: float, iv_52w_low: float, iv_52w_high: float) -> float:
    """0-100 scale: where current IV sits within its 52-week range."""
    if iv_52w_high == iv_52w_low:
        return 50.0
    return max(0.0, min(100.0, 100 * (current_iv - iv_52w_low) / (iv_52w_high - iv_52w_low)))


def occ_symbol(underlying: str, expiration: str, option_type: str, strike: float) -> str:
    """Builds the OCC-format option symbol Schwab's API expects, e.g. AAPL  260821C00190000."""
    exp = datetime.strptime(expiration, "%Y-%m-%d").strftime("%y%m%d")
    cp = "C" if option_type.lower().startswith("c") else "P"
    strike_str = f"{int(round(strike * 1000)):08d}"
    return f"{underlying:<6}{exp}{cp}{strike_str}"


def nearest_by_delta(option_chain: list[dict], target_delta: float) -> dict | None:
    """option_chain: list of {delta, strike, bid, ask, symbol, ...} for one expiration/side."""
    if not option_chain:
        return None
    return min(option_chain, key=lambda o: abs(abs(o["delta"]) - target_delta))


def parse_occ_symbol(occ: str) -> dict:
    """Inverse of occ_symbol(). Returns {underlying, expiration ('YYYY-MM-DD'),
    option_type ('call'|'put'), strike}. Raises ValueError on malformed input.

    Used by paper_broker.py to figure out what it's filling, since the
    Schwab order payload only carries the OCC symbol, not the structured
    fields separately.
    """
    if len(occ) < 21:
        raise ValueError(f"'{occ}' doesn't look like a 21-char OCC option symbol")
    underlying = occ[0:6].strip()
    exp_str = occ[6:12]
    cp = occ[12]
    strike_str = occ[13:21]
    expiration = datetime.strptime(exp_str, "%y%m%d").strftime("%Y-%m-%d")
    option_type = "call" if cp.upper() == "C" else "put"
    strike = int(strike_str) / 1000.0
    return {"underlying": underlying, "expiration": expiration, "option_type": option_type, "strike": strike}


def filter_liquid_chain(sides: dict, max_spread_pct: float = 0.20) -> dict:
    """Drops any call/put contract whose quoted bid/ask spread exceeds
    max_spread_pct of mid. Call this right after market_data.get_option_chain
    returns a per-expiration {"calls": [...], "puts": [...]} dict, before
    doing anything else with it — a contract with a 300% spread shouldn't
    even be considered as a candidate leg, whether or not it happens to be
    the closest-to-target-delta one. Contracts with a missing/zero bid or
    ask are also dropped (can't compute a spread, and a $0 bid usually means
    "no one is actually making a market here" anyway).
    """
    def _ok(c):
        bid, ask = c.get("bid"), c.get("ask")
        if not bid or not ask or bid <= 0 or ask <= 0:
            return False
        mid = (bid + ask) / 2
        if not mid:
            return False
        return (ask - bid) / mid <= max_spread_pct

    return {
        "calls": [c for c in sides.get("calls", []) if _ok(c)],
        "puts": [c for c in sides.get("puts", []) if _ok(c)],
    }
