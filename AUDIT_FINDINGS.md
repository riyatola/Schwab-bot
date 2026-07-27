# Strategy & Core Logic Audit — Findings

Scope: `strategies/*.py`, `base.py`, `learning.py`, `executor.py`, `notifier.py`,
`risk_manager.py`, `guardrails.py`, `market_data.py`, `paper_broker.py`.

## Fixed in this pass

1. **`notifier.py` — dead duplicate method (real bug).**
   `TelegramNotifier.send_learning_report` was defined twice. Python silently
   keeps only the last definition, so the first ~15-line version never ran —
   any edits to it would have had zero effect. Removed the dead copy.

2. **`learning.py` — multiplier ignored expectancy despite claiming to use it
   (real bug, risk-relevant).**
   The module docstring says the rule-based multiplier is "derived from
   realized win rate and expectancy," but the code only used win rate:
   `raw = 0.5 + win_rate`. For strategies with an asymmetric payoff — like
   short premium (credit spreads, wheel) where wins are small and consistent
   but losses are occasionally large — this rewards a strategy on win rate
   alone even when its actual expectancy (avg realized P&L) is negative.
   That's the textbook way an options-selling bot looks great for months and
   then gives it all back in one trade.

   Fixed by blending win rate and average realized P&L (scaled by a new
   `learning.expectancy_scale_dollars` config, default $100/trade) into the
   multiplier, both clamped to the existing floor/ceiling. Now a high win
   rate with negative expectancy nets out near or below neutral instead of
   getting boosted.

3. **`executor.py` — confusing dead ternary (cosmetic, not a functional bug).**
   `"SINGLE" if len(legs) == 1 else "SINGLE"` always evaluates to the same
   value. Not wrong — Schwab does represent multi-leg spreads as one
   `orderStrategyType=SINGLE` order with multiple legs — but written this way
   it looks like an unfinished branch and invites someone to "fix" it into
   something incorrect later. Replaced with a plain value + comment.

4. **`earnings_vol.py` — redundant API calls (efficiency/rate-limit risk).**
   `get_last_price(symbol)` was called once per *expiration* inside the scan
   loop, even though the underlying price doesn't depend on expiration.
   Across a ~600-symbol universe with multiple expirations per symbol in the
   DTE window, this was inflating quote-API call volume for no reason. Moved
   the call outside the expiration loop (once per symbol). Also replaced the
   `__import__("datetime")` hack with a normal import.

## Flagged, not changed — worth your judgment call

5. **IV field units unverified.** `market_data._parse_contract` reads
   `c.get("volatility", 0.0)` from Schwab's option chain response as IV.
   Whether that field is a percentage (e.g. `32.5`) or a decimal (`0.325`)
   affects `iv_history.py`'s IV-rank math and every `min_iv_rank` /
   `max_iv_rank` gate in your strategies. I can't verify this without live
   API access — worth confirming against a real response payload before you
   size anything off IV rank.

6. **`risk_manager.account_snapshot()`**: `day_trades_left` is populated from
   `data.get("roundTrips", 0)`, which is most likely "round trips already
   used," not "round trips remaining" — and it's not actually read anywhere
   else in the codebase (the PDT check only uses `net_liq` and a hardcoded
   `is_day_trade=False`). Dead/mislabeled field — low priority, but rename or
   remove it so it doesn't get load-bearing by accident later.

7. **Startup ordering dependency between `trade_tracker.py` and
   `guardrails.py`/`learning.py`.** `guardrails.evaluate_all_symbols()` and
   `learning`'s SQL queries read `trades.outcome`, a column only added by
   `trade_tracker.ensure_trade_outcome_columns()` at `TradeTracker.__init__`.
   Currently this works because `scanner.main()` constructs `TradeTracker`
   before the scheduler ever fires those jobs — but it's an implicit
   ordering contract, not an enforced one. If that construction order ever
   changes, you get a silent `sqlite3.OperationalError: no such column`
   at the first scheduled run, not at startup. Low priority to fix (move the
   migration into `approval_manager.Db.__init__` so it's unconditional), but
   worth knowing about.

8. **Scoring is duplicated logic in `wheel.py`.** The CSP score and covered
   call score use the identical formula (`0.5 + min(credit/strike, 0.1)*5`).
   Not a bug, but it means the score doesn't distinguish between "cash I'm
   putting up for a new position" and "opportunistic income on stock I
   already hold" — the latter is structurally lower-risk (no assignment risk
   worry, no new capital), which the score doesn't currently reflect. A
   candidate for tuning once you have real closed-trade data to fit against.

## Not touched

Everything in `risk_manager.py`'s hard gates, `approval_manager.py`,
`guardrails.py`'s ban logic, and `sentiment.py` looked internally consistent
— no bugs found there on this pass.
