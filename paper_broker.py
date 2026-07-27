"""
Local paper-trading broker. Implements the same method surface as
schwabdev.Client for the calls this codebase actually makes
(order_place, account_details, account_linked, transactions), and
transparently delegates everything else (option_chains, price_history,
quotes, movers) to a real, read-only Schwab client. That means every
other module in this repo — market_data.py, risk_manager.py,
trade_tracker.py, executor.py — works completely unmodified whether
you're paper trading or live. Only auth.build_client() knows the
difference (config: `mode: paper` vs `mode: live`).

This is a snapshot-fill engine, not a tick-level matching engine: fills
happen against the latest REST option-chain quote at order time (with
configurable slippage), not against a live streaming order book. That's
a deliberate simplification to match the rest of this codebase, which is
REST-polling based (15-min scan cycles), not a WebSocket tick pipeline —
if you want true streaming-tick fills, that's a bigger rebuild (see the
schwab-py streaming docs) and arguably overkill for validating a scanner
that only checks in every 15 minutes anyway.

Known simplifications (read before trusting paper P&L numbers):
  - No TRUE early assignment simulation. What this DOES do (see
    check_early_assignment_risk(), called daily by scanner.py alongside
    settle_expired_positions): if a short option goes deep ITM
    (underlying beyond strike by >= paper.early_assignment_itm_threshold_pct,
    default 5%) with time still left to expiration, it force-buys-back
    that leg at the current ask/bid (whichever side you'd actually pay to
    close) and realizes the P&L right then, instead of letting the paper
    account ride it to expiration collecting theta a real broker would
    never let you keep uncontested. This is a conservative approximation,
    not real assignment: it doesn't create the resulting stock position
    (see seed_stock_position() to test that manually), doesn't model
    dividend-driven early exercise timing on calls specifically, and the
    5% threshold is a blunt proxy for "an assignment-minded holder on the
    other side would plausibly exercise here" — tune
    paper.early_assignment_itm_threshold_pct if it's too aggressive/lax
    for your strategies. The goal is to stop wheel/credit-spread paper
    results from being biased optimistic in the exact same direction
    every time, not to perfectly model assignment mechanics.
  - No commissions/fees modeled (set `paper.commission_per_contract` if
    you want to approximate Schwab's $0.65/contract options fee — most
    accounts are $0 base commission but confirm your own).
  - Assignment doesn't create the resulting stock position, so paper mode
    won't organically produce the "existing long stock" covered-call leg
    of wheel.py — seed a paper stock position manually via
    seed_stock_position() if you want to test that path.
  - Mark-to-market for account_details() does one live quote lookup per
    open position, so it scales fine for max_concurrent_positions=8-ish
    but would get slow with a very large book.
"""

import logging
import sqlite3
import uuid
from datetime import date, datetime

from strategies.base import parse_occ_symbol

log = logging.getLogger("paper_broker")

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    cash REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_positions (
    id TEXT PRIMARY KEY,
    order_id TEXT,
    underlying TEXT,
    occ_symbol TEXT,
    option_type TEXT,
    strike REAL,
    expiration TEXT,
    side TEXT,               -- LONG or SHORT
    quantity INTEGER,
    entry_price REAL,        -- per-share price paid (LONG) or received (SHORT)
    opened_at TEXT,
    status TEXT DEFAULT 'OPEN',   -- OPEN, CLOSED
    closed_at TEXT,
    close_price REAL,
    realized_pnl REAL
);

CREATE TABLE IF NOT EXISTS paper_orders (
    id TEXT PRIMARY KEY,
    account_hash TEXT,
    payload_json TEXT,
    status TEXT,
    created_at TEXT
);
"""

OPTION_MULTIPLIER = 100
PAPER_ACCOUNT_HASH = "PAPER0"


class FakeResponse:
    """Mimics just enough of a requests.Response for this codebase's usage
    pattern (resp.status_code, resp.raise_for_status(), resp.json(), resp.headers)."""

    def __init__(self, status_code, json_body=None, headers=None, text=""):
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}
        self.headers = headers or {}
        self.text = text or str(self._json)

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"Paper broker error {self.status_code}: {self.text}")


class PaperBrokerClient:
    def __init__(self, real_client, cfg: dict, db_path: str = "./bot.db"):
        self.real_client = real_client
        paper_cfg = cfg.get("paper", {})
        self.starting_cash = paper_cfg.get("starting_cash", 100_000.0)
        self.slippage_bps = paper_cfg.get("slippage_bps", 5)        # 5 bps ~ optimistic but nonzero friction
        self.commission_per_contract = paper_cfg.get("commission_per_contract", 0.0)
        self.max_qty_wide_spread = paper_cfg.get("max_qty_wide_spread", 1)
        # See check_early_assignment_risk() / module docstring — how far ITM
        # (as a % move of underlying beyond strike) a short option has to be,
        # with time still left, before we force-buy it back rather than let
        # the paper account ride it to expiration.
        self.early_assignment_itm_threshold_pct = paper_cfg.get("early_assignment_itm_threshold_pct", 0.05)

        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._ensure_account()

    # ---------- delegate everything read-only to the real client ----------

    def __getattr__(self, name):
        # Only reached for attributes not defined on this class, i.e.
        # option_chains, price_history, quotes, movers, transactions
        # (transactions IS overridden below, so it won't hit this path).
        return getattr(self.real_client, name)

    # ---------- account setup ----------

    def _ensure_account(self):
        row = self.conn.execute("SELECT cash FROM paper_account WHERE id=1").fetchone()
        if not row:
            self.conn.execute("INSERT INTO paper_account (id, cash) VALUES (1, ?)", (self.starting_cash,))
            self.conn.commit()
            log.info("Paper account initialized with $%.2f starting cash", self.starting_cash)

    def _get_cash(self) -> float:
        return self.conn.execute("SELECT cash FROM paper_account WHERE id=1").fetchone()[0]

    def _adjust_cash(self, delta: float):
        self.conn.execute("UPDATE paper_account SET cash = cash + ? WHERE id=1", (delta,))
        self.conn.commit()

    def seed_stock_position(self, symbol: str, quantity: int, avg_price: float):
        """Manually seed a long stock position for testing wheel.py's
        covered-call path, since paper mode doesn't simulate assignment.
        Not called automatically anywhere."""
        self.conn.execute(
            "INSERT INTO paper_positions (id, underlying, occ_symbol, option_type, strike, expiration, "
            "side, quantity, entry_price, opened_at, status) VALUES (?, ?, 'EQUITY', 'equity', 0, '', "
            "'LONG', ?, ?, ?, 'OPEN')",
            (str(uuid.uuid4()), symbol.upper(), quantity, avg_price, datetime.utcnow().isoformat()),
        )
        self.conn.commit()

    # ---------- schwabdev.Client-compatible surface ----------

    def account_linked(self):
        return FakeResponse(200, [{"hashValue": PAPER_ACCOUNT_HASH}])

    def linked_accounts(self):
        return FakeResponse(200, [{"hashValue": PAPER_ACCOUNT_HASH}])

    def account_details(self, account_hash: str, fields: str = None):
        cash = self._get_cash()
        rows = self.conn.execute(
            "SELECT underlying, occ_symbol, option_type, strike, expiration, side, quantity, entry_price "
            "FROM paper_positions WHERE status='OPEN'"
        ).fetchall()

        positions = []
        market_value = 0.0
        for underlying, occ, opt_type, strike, expiration, side, qty, entry_price in rows:
            if occ == "EQUITY":
                positions.append({
                    "instrument": {"symbol": underlying, "assetType": "EQUITY"},
                    "symbol": underlying,
                    "assetType": "EQUITY",
                    "longQuantity": qty,
                    "shortQuantity": 0,
                    "averagePrice": entry_price,
                })
                market_value += qty * entry_price  # not live-marked; good enough for position-limit checks
                continue

            last = self._mark_option(underlying, expiration, opt_type, strike)
            mtm_price = last if last is not None else entry_price
            sign = 1 if side == "LONG" else -1
            market_value += sign * mtm_price * qty * OPTION_MULTIPLIER

            positions.append({
                "instrument": {"symbol": occ, "assetType": "OPTION"},
                "symbol": occ,
                "assetType": "OPTION",
                "longQuantity": qty if side == "LONG" else 0,
                "shortQuantity": qty if side == "SHORT" else 0,
                "averagePrice": entry_price,
            })

        body = {
            "securitiesAccount": {
                "currentBalances": {
                    "liquidationValue": round(cash + market_value, 2),
                    "cashBalance": round(cash, 2),
                },
                "roundTrips": 0,
                "positions": positions,
            }
        }
        return FakeResponse(200, body)

    def transactions(self, account_hash: str, symbol: str = None, types: str = None):
        query = "SELECT underlying, realized_pnl FROM paper_positions WHERE status='CLOSED'"
        params = []
        if symbol:
            query += " AND underlying=?"
            params.append(symbol.upper())
        rows = self.conn.execute(query, params).fetchall()
        return FakeResponse(200, [{"netAmount": pnl} for _, pnl in rows if pnl is not None])

    def order_place(self, account_hash: str, payload: dict):
        return self.place_order(account_hash, payload)
        
    def place_order(self, account_hash: str, payload: dict):
        order_id = str(uuid.uuid4())
        legs = payload.get("orderLegCollection", [])
        filled_legs = []

        try:
            for leg in legs:
                occ = leg["instrument"]["symbol"]
                qty = leg["quantity"]
                instruction = leg["instruction"]
                parsed = parse_occ_symbol(occ)

                bid, ask = self._get_leg_bid_ask(parsed["underlying"], parsed["expiration"],
                                                  parsed["option_type"], parsed["strike"])
                if bid is None or ask is None:
                    raise RuntimeError(f"No live quote found for {occ} — can't paper-fill")

                # Check for wide spread and cap quantity if needed
                mid = (bid + ask) / 2
                spread_pct = (ask - bid) / mid if mid else 0
                if spread_pct > 0.20 and qty > self.max_qty_wide_spread:
                    raise RuntimeError(f"{occ}: spread {spread_pct:.0%} too wide to fill {qty} contracts (cap {self.max_qty_wide_spread} on illiquid names)")

                # Use tiered slippage
                effective_slippage_bps = self._tiered_slippage_bps(bid, ask)
                slip = effective_slippage_bps / 10_000.0

                if instruction.startswith("BUY"):
                    fill_price = ask * (1 + slip)
                    side = "LONG"
                    cash_delta = -(fill_price * qty * OPTION_MULTIPLIER) - (self.commission_per_contract * qty)
                else:  # SELL_TO_OPEN
                    fill_price = bid * (1 - slip)
                    side = "SHORT"
                    cash_delta = (fill_price * qty * OPTION_MULTIPLIER) - (self.commission_per_contract * qty)

                filled_legs.append((parsed, side, qty, fill_price, cash_delta))
        except Exception as e:
            log.warning("Paper order failed to fill: %s", e)
            return FakeResponse(400, {}, text=str(e))

        # All legs quoted successfully — commit the fills atomically.
        now = datetime.utcnow().isoformat()
        for parsed, side, qty, fill_price, cash_delta in filled_legs:
            self.conn.execute(
                "INSERT INTO paper_positions (id, order_id, underlying, occ_symbol, option_type, strike, "
                "expiration, side, quantity, entry_price, opened_at, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')",
                (str(uuid.uuid4()), order_id, parsed["underlying"],
                 f"{parsed['underlying']}_{parsed['expiration']}_{parsed['option_type']}_{parsed['strike']}",
                 parsed["option_type"], parsed["strike"], parsed["expiration"], side, qty, fill_price, now),
            )
            self._adjust_cash(cash_delta)

        import json
        self.conn.execute(
            "INSERT INTO paper_orders (id, account_hash, payload_json, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (order_id, account_hash, json.dumps(payload), "FILLED", now),
        )
        self.conn.commit()
        log.info("Paper-filled order %s (%d leg(s))", order_id, len(filled_legs))

        return FakeResponse(201, {}, headers={"Location": f"paper://orders/{order_id}"})

    # ---------- quoting helpers ----------

    def _tiered_slippage_bps(self, bid, ask) -> float:
        """Returns slippage in basis points, tiered based on quoted spread width."""
        if not bid or not ask:
            return self.slippage_bps
        mid = (bid + ask) / 2
        spread_pct = (ask - bid) / mid if mid else 0
        if spread_pct > 0.15:  # 15% or more spread
            return self.slippage_bps * 5
        if spread_pct > 0.08:  # 8-15% spread
            return self.slippage_bps * 2
        return self.slippage_bps

    def _get_leg_bid_ask(self, underlying, expiration, option_type, strike):
        try:
            resp = self.real_client.option_chains(
                symbol=underlying, contractType="ALL", strikeCount=40,
                fromDate=expiration, toDate=expiration,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.warning("Quote lookup failed for %s: %s", underlying, e)
            return None, None

        exp_map = data.get("callExpDateMap" if option_type == "call" else "putExpDateMap", {})
        for exp_key, by_strike in exp_map.items():
            for strike_str, contracts in by_strike.items():
                if abs(float(strike_str) - strike) < 0.001:
                    c = contracts[0]
                    return c.get("bid", 0.0), c.get("ask", 0.0)
        return None, None

    def _mark_option(self, underlying, expiration, option_type, strike):
        bid, ask = self._get_leg_bid_ask(underlying, expiration, option_type, strike)
        if bid is None or ask is None:
            return None
        return round((bid + ask) / 2, 4)

    # ---------- early assignment risk, run daily by scanner.py ----------

    def check_early_assignment_risk(self):
        """Force-buys-back any SHORT option position that's gone deep ITM
        with time still left on the clock, instead of letting the paper
        account hold it (and collect theta on it) all the way to
        expiration — see module docstring's "Known simplifications" for
        why this exists and what it does/doesn't model. Call once a day,
        alongside settle_expired_positions() (that one only handles
        options that have already hit expiration; this one handles ones
        that haven't yet but arguably wouldn't survive that long for real).

        Threshold is symmetric around the strike as a % of the strike
        price, configurable via paper.early_assignment_itm_threshold_pct
        (default 0.05 = 5%):
          - short PUT:  underlying <= strike * (1 - threshold)
          - short CALL: underlying >= strike * (1 + threshold)
        """
        today = date.today().isoformat()
        rows = self.conn.execute(
            "SELECT id, underlying, option_type, strike, expiration, quantity, entry_price "
            "FROM paper_positions WHERE status='OPEN' AND occ_symbol != 'EQUITY' "
            "AND side='SHORT' AND expiration > ?",
            (today,),
        ).fetchall()
        if not rows:
            return 0

        threshold = self.early_assignment_itm_threshold_pct
        forced = 0
        for pos_id, underlying, option_type, strike, expiration, qty, entry_price in rows:
            try:
                resp = self.real_client.quotes([underlying])
                resp.raise_for_status()
                last = resp.json()[underlying]["quote"]["lastPrice"]
            except Exception as e:
                log.warning("Couldn't check assignment risk on %s (no underlying quote): %s", underlying, e)
                continue
            if not last or not strike:
                continue

            if option_type == "put":
                is_deep_itm = last <= strike * (1 - threshold)
            else:
                is_deep_itm = last >= strike * (1 + threshold)
            if not is_deep_itm:
                continue

            # Cost to close a short option is the ask (you're buying it back).
            bid, ask = self._get_leg_bid_ask(underlying, expiration, option_type, strike)
            close_price = ask if ask is not None else max(0.0, last - strike if option_type == "call"
                                                            else strike - last)  # intrinsic fallback

            realized = (entry_price - close_price) * qty * OPTION_MULTIPLIER
            self._adjust_cash(-close_price * qty * OPTION_MULTIPLIER)

            self.conn.execute(
                "UPDATE paper_positions SET status='CLOSED', closed_at=?, close_price=?, realized_pnl=? WHERE id=?",
                (datetime.utcnow().isoformat(), close_price, round(realized, 2), pos_id),
            )
            forced += 1
            log.warning(
                "Forced buyback (early-assignment risk): %s %s %s strike=%.2f underlying=%.2f "
                "close_price=%.2f realized_pnl=%.2f",
                underlying, option_type, expiration, strike, last, close_price, round(realized, 2),
            )

        self.conn.commit()
        if forced:
            log.warning("Forced buyback on %d deep-ITM short position(s) — see early_assignment_itm_threshold_pct", forced)
        return forced

    # ---------- expiration settlement, run daily by scanner.py ----------

    def settle_expired_positions(self):
        """Settles any open option position past its expiration at
        intrinsic value (no early-assignment modeling — see module
        docstring). Call this once a day after market close in paper mode."""
        today = date.today().isoformat()
        rows = self.conn.execute(
            "SELECT id, underlying, option_type, strike, side, quantity, entry_price "
            "FROM paper_positions WHERE status='OPEN' AND occ_symbol != 'EQUITY' AND expiration <= ?",
            (today,),
        ).fetchall()
        if not rows:
            return 0

        settled = 0
        for pos_id, underlying, option_type, strike, side, qty, entry_price in rows:
            try:
                resp = self.real_client.quotes([underlying])
                resp.raise_for_status()
                last = resp.json()[underlying]["quote"]["lastPrice"]
            except Exception as e:
                log.warning("Couldn't settle %s (no underlying quote): %s", underlying, e)
                continue

            intrinsic = max(0.0, last - strike) if option_type == "call" else max(0.0, strike - last)
            if side == "LONG":
                realized = (intrinsic - entry_price) * qty * OPTION_MULTIPLIER
                self._adjust_cash(intrinsic * qty * OPTION_MULTIPLIER)
            else:
                realized = (entry_price - intrinsic) * qty * OPTION_MULTIPLIER
                self._adjust_cash(-intrinsic * qty * OPTION_MULTIPLIER)

            self.conn.execute(
                "UPDATE paper_positions SET status='CLOSED', closed_at=?, close_price=?, realized_pnl=? WHERE id=?",
                (datetime.utcnow().isoformat(), intrinsic, round(realized, 2), pos_id),
            )
            settled += 1

        self.conn.commit()
        if settled:
            log.info("Settled %d expired paper position(s)", settled)
        return settled