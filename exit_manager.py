"""
Exit manager: monitors open positions for stop-loss/take-profit and
generates approval requests for exits.

Same principle as entry: no auto-execution — everything needs a human tap.
"""

import logging
import sqlite3
import json
import uuid
from datetime import datetime

log = logging.getLogger("exit_manager")


class ExitManager:
    def __init__(self, cfg: dict, db, client, account_hash: str, market_data=None, sentiment=None):
        self.cfg = cfg
        self.db = db
        self.conn = db.conn
        self.client = client
        self.account_hash = account_hash
        self.market_data = market_data
        self.sentiment = sentiment
        self.exit_cfg = self.cfg.get("exits", {})
        self.enabled = self.exit_cfg.get("enabled", True)
        self.last_risk_off_alert_date = None

    def get_open_trades(self):
        """Get all open trades from the trades table."""
        return self.conn.execute(
            "SELECT id, symbol, order_json, entry_price, stop_loss, take_profit, is_credit, strategy, expiration "
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
                    dte = (datetime.strptime(parsed["expiration"], "%Y-%m-%d").date() - datetime.now().date()).days
                    chain = self.market_data.get_option_chain(
                        parsed["underlying"],
                        dte_min=1,
                        dte_max=dte + 1,
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
                mtm += sign * current_price * (qty / 1)

        return round(mtm, 2) if mtm else None

    def check_triggers(self):
        """Check all open trades for stop-loss/take-profit triggers and force_exit_dte."""
        if not self.enabled:
            return []

        # 1. Get current macro regime
        regime = "NEUTRAL"
        if self.sentiment and self.sentiment.cfg.get("enabled", True):
            regime = self.sentiment.current_regime()

        open_trades = self.get_open_trades()
        if not open_trades:
            return []

        triggers = []
        for (
            trade_id, symbol, order_json, entry_price, stop_loss, take_profit, is_credit, strategy, expiration
        ) in open_trades:
            # Skip if we already generated an exit approval
            existing_exit = self.conn.execute(
                "SELECT id FROM approvals WHERE id IN (SELECT exit_approval_id FROM trades WHERE id=?) AND status='PENDING'",
                (trade_id,),
            ).fetchone()
            if existing_exit:
                continue

            # 2. Check force exit by DTE
            strategy_exit_cfg = self.cfg.get("strategies", {}).get(strategy, {}).get("exits", {})
            force_exit_dte = strategy_exit_cfg.get("force_exit_dte", self.exit_cfg.get("force_exit_dte", 7))
            exp_date = None
            remaining_days = None
            if expiration:
                try:
                    exp_date = datetime.strptime(expiration, "%Y-%m-%d").date()
                    remaining_days = (exp_date - datetime.now().date()).days
                    if remaining_days <= force_exit_dte:
                        # Force exit regardless of other conditions
                        current_price = self.calculate_position_mtm(order_json)
                        if current_price is not None:
                            triggers.append({
                                "trade_id": trade_id,
                                "symbol": symbol,
                                "strategy": strategy,
                                "order_json": order_json,
                                "entry_price": entry_price,
                                "current_price": current_price,
                                "trigger_type": "time_decay_exit",
                            })
                            continue  # Skip other checks for this trade
                except ValueError:
                    log.warning(f"Couldn't parse expiration date: {expiration}")

            # Calculate dynamic stop-loss even if stored stop_loss is null
            dynamic_stop = None
            if entry_price is not None and exp_date is not None and remaining_days is not None:
                # Get current IV rank and ATR for the underlying
                iv_rank = self.market_data.get_iv_rank(symbol) if self.market_data else 50
                atr = self.market_data.get_atr(symbol) if self.market_data else None
                
                # Compute base stop as percentage of entry
                base_stop_pct = self.exit_cfg.get("default_stop_loss_pct", {}).get(strategy, 0.50)
                
                # Adjust for IV: higher IV -> wider stop
                iv_adjust = 1 + (iv_rank / 100 - 0.5) * self.exit_cfg.get("iv_adjustment_factor", 0.5)  # range 0.75..1.25
                # Adjust for time decay: as DTE decreases, tighten stop
                dte = remaining_days
                time_adjust = 1 - (1 - dte / force_exit_dte) * self.exit_cfg.get("time_decay_factor", 0.2)
                # Clamp between min and max
                dynamic_stop_pct = base_stop_pct * iv_adjust * time_adjust
                dynamic_stop_pct = max(self.exit_cfg.get("min_stop_pct", 0.10), min(self.exit_cfg.get("max_stop_pct", 0.50), dynamic_stop_pct))
                
                # Calculate absolute stop loss value
                if is_credit:
                    dynamic_stop = entry_price * dynamic_stop_pct
                else:
                    dynamic_stop = entry_price * dynamic_stop_pct

            # Use dynamic stop if no stored stop_loss, otherwise use stored value
            effective_stop_loss = stop_loss if stop_loss is not None else dynamic_stop
            if effective_stop_loss is None and take_profit is None:
                continue

            # Get current MTM
            current_price = self.calculate_position_mtm(order_json)
            if current_price is None:
                continue

            # Apply risk-off tightening if needed
            if self.sentiment and self.sentiment.cfg.get("enabled", True):
                regime = self.sentiment.current_regime()
                factor = self.exit_cfg.get("risk_off_stop_loss_factor", 0.5)
                if regime == "RISK_OFF" and strategy in ("directional", "earnings_vol") and effective_stop_loss is not None:
                    effective_stop_loss = effective_stop_loss * factor

            # Check triggers
            trigger_type = None
            if is_credit:
                # For credit trades:
                # - Stop loss: entry - current >= effective_stop_loss (losing money)
                # - Take profit: entry - current <= -take_profit (making money)
                if effective_stop_loss is not None and (entry_price - current_price) >= effective_stop_loss:
                    trigger_type = "stop_loss"
                elif take_profit is not None and (entry_price - current_price) <= -take_profit:
                    trigger_type = "take_profit"
            else:
                # For debit trades:
                # - Stop loss: current - entry <= -effective_stop_loss
                # - Take profit: current - entry >= take_profit
                if effective_stop_loss is not None and (current_price - entry_price) <= -effective_stop_loss:
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
                    "trigger_type": trigger_type,
                    "regime": regime,
                    "stop_loss_original": stop_loss,
                    "stop_loss_effective": effective_stop_loss,
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
    # Find original qty from original trade?
    # Wait, original order has qty in order_json, let's get from candidate?
    # For now, assume 1 contract per leg (we can refine later)
    qty = 1

    order_legs = []
    for leg in legs:
        order_legs.append({
            "instruction": leg["instruction"],
            "quantity": qty * leg.get("ratio", 1),
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
