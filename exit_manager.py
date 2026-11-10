"""
Exit manager: monitors open positions for stop-loss/take-profit and
generates approval requests for exits.

Same principle as entry: no auto-execution — everything needs a human tap.
"""

import logging
import sqlite3
import json
import uuid
from datetime import date, datetime

log = logging.getLogger("exit_manager")


class ExitManager:
    def __init__(self, cfg: dict, db, client, account_hash: str, market_data=None):
        self.cfg = cfg
        self.db = db
        self.conn = db.conn
        self.client = client
        self.account_hash = account_hash
        self.market_data = market_data

        # Default exit config if not in config.yaml
        self.exit_cfg = self.cfg.get("exits", {})
        self.enabled = self.exit_cfg.get("enabled", True)

    def get_open_trades(self):
        """Get all open trades from the trades table."""
        return self.conn.execute(
            "SELECT id, symbol, order_json, entry_price, stop_loss, take_profit, is_credit, strategy "
            "FROM trades WHERE status='EXECUTED' AND (outcome='OPEN' OR outcome IS NULL)"
        ).fetchall()

    def calculate_position_mtm(self, order_json: str):
        """Calculate current mark-to-market price of the position using real market data."""
        import json
        from bs_pricing import bs_price_and_delta
        from strategies.base import parse_occ_symbol

        legs = json.loads(order_json).get("orderLegCollection", [])
        if not legs:
            return None

        mtm = 0.0
        for leg in legs:
            instr = leg.get("instrument", {})
            occ = instr.get("symbol")
            if not occ:
                continue

            try:
                parsed = parse_occ_symbol(occ)
            except ValueError:
                log.warning(f"Couldn't parse OCC symbol {occ}")
                continue

            # Try real market data first
            current_price = None
            if self.market_data:
                try:
                    chain = self.market_data.get_option_chain(
                        parsed["underlying"],
                        dte_min=1,
                        dte_max=(datetime.strptime(parsed["expiration"], "%Y-%m-%d").date() - datetime.now().date()).days + 1,
                    )
                    # Find the contract in the chain
                    for exp, sides in chain.items():
                        for option_type, contracts in (("call", sides.get("calls", [])), ("put", sides.get("puts", []))):
                            if exp == parsed["expiration"] and option_type == parsed["option_type"]:
                                for c in contracts:
                                    if abs(c["strike"] - parsed["strike"]) < 0.01:
                                        current_price = (c["bid"] + c["ask"]) / 2
                                        break
                            if current_price:
                                break
                        if current_price:
                            break
                except Exception:
                    log.debug(f"Couldn't get real quote for {occ}")

            # Fall back to synthetic pricing
            if not current_price and self.market_data:
                try:
                    spot = self.market_data.get_last_price(parsed["underlying"])
                    if spot:
                        dte = max(1, (datetime.strptime(parsed["expiration"], "%Y-%m-%d").date() - datetime.now().date()).days)
                        # Try to get IV from chain or use fallback
                        iv = 0.30
                        if self.market_data:
                            chain = self.market_data.get_option_chain(
                                parsed["underlying"], dte_min=dte, dte_max=dte + 7
                            )
                            for exp, sides in chain.items():
                                avg_iv = []
                                for option_type, contracts in (("call", sides.get("calls", [])), ("put", sides.get("puts", []))):
                                    avg_iv.extend([c["iv"] for c in contracts if c.get("iv") is not None])
                                if avg_iv:
                                    iv = sum(avg_iv) / len(avg_iv)
                                    break
                        current_price, _ = bs_price_and_delta(
                            spot, parsed["strike"], dte, iv, parsed["option_type"]
                        )
                except Exception:
                    log.debug(f"Synthetic pricing failed for {occ}")

            if current_price:
                # Calculate value contribution of this leg:
                # - SELL_TO_OPEN: we collected credit, so current price is what we'd pay to close (negative)
                # - BUY_TO_OPEN: we paid debit, so current price is what we'd get to close (positive)
                instr = leg.get("instruction", "")
                qty = leg.get("quantity", 0)
                sign = 1 if instr.startswith("BUY") else -1
                mtm += sign * current_price * (qty / 1 if "ratio" not in locals() else 1)

        return round(mtm, 2) if mtm else None

    def _force_exit_dte(self, strategy: str):
        """Per-strategy forced-exit DTE (strategies.<name>.exits.force_exit_dte).
        A position at or below this DTE gets a time-decay exit alert — for
        LEAPS (120) that means rolling/closing well before the last ~6 months
        of accelerated theta decay. Returns None when the strategy has no
        such rule configured."""
        return (self.cfg.get("strategies", {}).get(strategy, {})
                .get("exits", {}).get("force_exit_dte"))

    def _position_dte(self, order_json: str):
        """Shortest DTE across the position's option legs, or None."""
        from strategies.base import parse_occ_symbol
        legs = json.loads(order_json).get("orderLegCollection", [])
        dtes = []
        for leg in legs:
            occ = leg.get("instrument", {}).get("symbol")
            if not occ:
                continue
            try:
                parsed = parse_occ_symbol(occ)
            except ValueError:
                continue
            exp = datetime.strptime(parsed["expiration"], "%Y-%m-%d").date()
            dtes.append((exp - date.today()).days)
        return min(dtes) if dtes else None

    def _resolve_thresholds(self, strategy, entry_price, stop_loss, take_profit):
        """Absolute stop/take levels (per share) win if they were set at
        entry; otherwise fall back to per-strategy percentage levels from
        exits.default_stop_loss_pct / exits.default_take_profit_pct. LEAPS
        entries never set absolute levels, so theirs resolve here."""
        exits_cfg = self.cfg.get("exits", {})
        if stop_loss is None and entry_price:
            pct = exits_cfg.get("default_stop_loss_pct", {}).get(strategy)
            if pct is not None:
                stop_loss = pct * abs(entry_price)
        if take_profit is None and entry_price:
            pct = exits_cfg.get("default_take_profit_pct", {}).get(strategy)
            if pct is not None:
                take_profit = pct * abs(entry_price)
        return stop_loss, take_profit

    def check_triggers(self):
        """Check all open trades for stop-loss/take-profit/time-decay triggers."""
        if not self.enabled:
            return []

        open_trades = self.get_open_trades()
        if not open_trades:
            return []

        triggers = []
        for (
            trade_id, symbol, order_json, entry_price, stop_loss, take_profit, is_credit, strategy
        ) in open_trades:
            stop_loss, take_profit = self._resolve_thresholds(strategy, entry_price, stop_loss, take_profit)
            force_exit_dte = self._force_exit_dte(strategy)
            pos_dte = self._position_dte(order_json)

            # Skip only if literally no exit rule can ever fire for this trade.
            if stop_loss is None and take_profit is None and force_exit_dte is None:
                continue
            # Skip if we already generated an exit approval
            existing_exit = self.conn.execute(
                "SELECT id FROM approvals WHERE id IN (SELECT exit_approval_id FROM trades WHERE id=?) AND status='PENDING'",
                (trade_id,),
            ).fetchone()
            if existing_exit:
                continue

            # Get current MTM
            current_price = self.calculate_position_mtm(order_json)
            if current_price is None:
                continue

            # Check triggers. Time-decay (per-strategy force_exit_dte) takes
            # priority — it's a scheduled exit, not a price-driven one.
            trigger_type = None
            if force_exit_dte is not None and pos_dte is not None and pos_dte <= force_exit_dte:
                trigger_type = "time_decay_exit"
            elif is_credit:
                # For credit trades:
                # - Stop loss: entry - current >= stop_loss (losing money)
                # - Take profit: entry - current <= -take_profit (making money)
                if stop_loss is not None and (entry_price - current_price) >= stop_loss:
                    trigger_type = "stop_loss"
                elif take_profit is not None and (entry_price - current_price) <= -take_profit:
                    trigger_type = "take_profit"
            else:
                # For debit trades:
                # - Stop loss: current - entry <= -stop_loss
                # - Take profit: current - entry >= take_profit
                if stop_loss is not None and (current_price - entry_price) <= -stop_loss:
                    trigger_type = "stop_loss"
                elif take_profit is not None and (current_price - entry_price) >= take_profit:
                    trigger_type = "take_profit"

            if trigger_type:
                triggers.append({
                    "trade_id": trade_id,
                    "symbol": symbol,
                    "strategy": strategy,
                    "order_json": order_json,
                    "entry_price": entry_price,
                    "current_price": current_price,
                    "is_credit": is_credit,
                    "trigger_type": trigger_type,
                })

        return triggers

    def build_exit_candidate(self, trigger: dict) -> dict:
        """Build an exit candidate dict for approval/execution."""
        import json
        legs = json.loads(trigger["order_json"]).get("orderLegCollection", [])
        exit_legs = []
        for leg in legs:
            instr = leg.get("instruction", "")
            # Reverse instructions for exit
            if instr == "BUY_TO_OPEN":
                exit_instr = "SELL_TO_CLOSE"
            elif instr == "SELL_TO_OPEN":
                exit_instr = "BUY_TO_CLOSE"
            elif instr == "BUY_TO_CLOSE":
                exit_instr = "SELL_TO_OPEN"  # Shouldn't happen, but just in case
            elif instr == "SELL_TO_CLOSE":
                exit_instr = "BUY_TO_OPEN"
            else:
                exit_instr = instr  # Fallback

            exit_legs.append({
                "occ_symbol": leg.get("instrument", {}).get("symbol"),
                "instruction": exit_instr,
                "ratio": 1,
                # Preserve the original contract count so a multi-contract
                # LEAPS exit closes the whole position, not just one lot.
                "quantity": leg.get("quantity", 1),
            })

        return {
            "symbol": trigger["symbol"],
            "strategy": f"exit_{trigger['trigger_type']}",
            "description": f"Exit {trigger['symbol']} {trigger['strategy']} (trigger: {trigger['trigger_type']})",
            "legs": exit_legs,
            "legs_summary": ", ".join([f"{l['instruction']} {l['occ_symbol']}" for l in exit_legs]),
            "expiration": "",
            "dte": 0,
            "est_price": trigger["current_price"],
            "is_credit": not trigger.get("is_credit", False),  # Reverse credit/debit for exit
            "trade_id": trigger["trade_id"],
            "trigger_type": trigger["trigger_type"],
            "entry_price": trigger["entry_price"],
            "current_price": trigger["current_price"],
        }


def build_exit_order_payload(candidate: dict) -> dict:
    """Build Schwab order payload for exit, same pattern as build_order_payload in executor.py."""
    import json
    legs = candidate["legs"]

    order_legs = []
    for leg in legs:
        order_legs.append({
            "instruction": leg["instruction"],
            # Use the leg's own contract count (carried from the original
            # order by build_exit_candidate); hardcoding 1 broke
            # multi-contract exits.
            "quantity": leg.get("quantity", 1) * leg.get("ratio", 1),
            "instrument": {
                "symbol": leg["occ_symbol"],
                "assetType": "OPTION",
            },
        })

    order_type = "NET_CREDIT" if candidate.get("is_credit") else "NET_DEBIT"

    return {
        "orderType": order_type,
        "session": "NORMAL",
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "price": f"{candidate['est_price']:.2f}",
        "orderLegCollection": order_legs,
    }
