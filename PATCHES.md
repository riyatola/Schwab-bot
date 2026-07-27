# Patches to apply

Two files are full drop-in replacements (just overwrite):
- `iv_history.py` — `MIN_OBSERVATIONS_FOR_RANK` lowered 40→20, docstring updated.
- `optionstrategist_iv.py` — new file, drop into your project root.

Everything below is a targeted diff. Each block shows what to find and
what to replace it with (or where to insert).

---

## 1. `market_data.py` — prefer the external IV percentile, add liquidity helpers

**Constructor** — accept an optional external IV store:

```python
# find:
    def __init__(self, client, cfg: dict = None, iv_store: IVHistoryStore = None,
                 earnings_calendar: EarningsCalendar = None):
        self.client = client
        self._chain_cache = {}
        self._history_cache = {}
        self.cfg = cfg or {}
        self.iv_store = iv_store
        self.earnings_calendar = earnings_calendar

# replace with:
    def __init__(self, client, cfg: dict = None, iv_store: IVHistoryStore = None,
                 earnings_calendar: EarningsCalendar = None, external_iv_store=None):
        self.client = client
        self._chain_cache = {}
        self._history_cache = {}
        self._avg_volume_cache = {}
        self.cfg = cfg or {}
        self.iv_store = iv_store
        self.earnings_calendar = earnings_calendar
        # optionstrategist_iv.ExternalIVStore — real-history IV percentile,
        # checked before iv_store/realized-vol proxy. Optional; None is fine.
        self.external_iv_store = external_iv_store
```

**get_iv_rank** — check the external percentile first:

```python
# find:
    def get_iv_rank(self, symbol: str) -> float:
        """
        Prefers real IV rank computed from recorded ATM IV history (see
        iv_history.py) once enough daily snapshots have accumulated
        (~2 months). Falls back to a realized-volatility-percentile proxy
        for symbols without enough history yet, or if no IVHistoryStore
        was wired in at all.
        """
        if self.iv_store:
            real_rank = self.iv_store.iv_rank(symbol)
            if real_rank is not None:
                return real_rank

# replace with:
    def get_iv_rank(self, symbol: str) -> float:
        """
        Fallback chain, best data first:
          1. optionstrategist_iv.ExternalIVStore — McMillan's own IV
             percentile, backed by hundreds of days of real history,
             refreshed weekly. Used if wired in and the latest row is
             fresh (see ExternalIVStore.percentile's max_age_days).
          2. iv_history.py's own iv_rank() — your own recorded ATM IV
             snapshots, once MIN_OBSERVATIONS_FOR_RANK is met.
          3. Realized-volatility-percentile proxy (always available).
        """
        if self.external_iv_store:
            ext_rank = self.external_iv_store.percentile(symbol)
            if ext_rank is not None:
                return ext_rank

        if self.iv_store:
            real_rank = self.iv_store.iv_rank(symbol)
            if real_rank is not None:
                return real_rank
```//
(leave the rest of the method — the realized-vol fallback — unchanged)
```

**Add two new methods** anywhere inside `MarketDataAdapter` (e.g. right after `get_volume_ratio`):

```python
    def get_avg_volume(self, symbol: str, lookback: int = 20) -> float:
        """Average daily UNDERLYING share volume over `lookback` days — used
        as a liquidity prefilter (config: universe.min_avg_option_volume).
        NOTE on naming: this is underlying stock volume, not option-contract
        volume. Schwab's chain response does carry a per-contract totalVolume
        field, but checking that would mean pulling a full chain for every
        candidate symbol just to decide whether to scan it — chicken-and-egg
        for a universe-level prefilter. Underlying volume is a reasonable,
        cheap proxy: illiquid stocks reliably have illiquid options chains,
        the converse isn't guaranteed but is rare enough to not be worth the
        extra API calls here. Same convention backtest_engine.py's
        illiquid_volume_threshold already uses."""
        cache_key = (symbol, lookback)
        if cache_key in self._avg_volume_cache:
            return self._avg_volume_cache[cache_key]
        resp = self.client.price_history(symbol, periodType="month", period=1, frequencyType="daily")
        resp.raise_for_status()
        candles = resp.json().get("candles", [])
        vols = [c["volume"] for c in candles[-lookback:] if c.get("volume") is not None]
        avg = float(sum(vols) / len(vols)) if vols else 0.0
        self._avg_volume_cache[cache_key] = avg
        return avg

    def leg_spread_pct(self, occ_symbol: str) -> float | None:
        """Bid/ask spread as a fraction of mid for one OCC option symbol, or
        None if it can't be found in a freshly pulled chain. Mirrors
        BacktestMarketData.leg_spread_pct so live and backtest use the same
        liquidity gate. Used by risk_manager's hard spread gate (see
        risk_manager.py patch below)."""
        from strategies.base import parse_occ_symbol
        parsed = parse_occ_symbol(occ_symbol)
        underlying = parsed["underlying"]
        chain = self.get_option_chain(underlying, dte_min=0, dte_max=400)
        target_exp = parsed["expiration"]
        option_type = parsed["option_type"]
        target_strike = parsed["strike"]
        sides = chain.get(target_exp)
        if not sides:
            return None
        contracts = sides.get("calls", []) if option_type == "call" else sides.get("puts", [])
        for c in contracts:
            if abs(c["strike"] - target_strike) < 0.001:
                bid, ask = c.get("bid"), c.get("ask")
                if not bid or not ask:
                    return None
                mid = (bid + ask) / 2
                return (ask - bid) / mid if mid else None
        return None
```

---

## 2. `strategies/base.py` — spread-filtering helper, used by every strategy module

Add this function (anywhere, e.g. after `nearest_by_delta`):

```python
def filter_liquid_chain(sides: dict, max_spread_pct: float = 0.20) -> dict:
    """Drops any call/put contract whose quoted bid/ask spread exceeds
    max_spread_pct of mid. Call this right after market_data.get_option_chain
    returns a per-expiration {"calls": [...], "puts": [...]} dict, before
    doing anything else with it — a contract with a 300% spread shouldn't
    even be considered as a candidate leg, whether or not it happens to be
    the closest-to-target-delta one. Contracts with a missing/zero bid or
    ask are also dropped (can't compute a spread, and a $0 bid usually means
    "no one is actually making a market here" anyway)."""
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
```

---

## 3. Each strategy module — apply the filter right after fetching the chain

Add a config default under each strategy in `config.yaml` (see section 6),
then in every strategy module, filter `sides` before using it. Example for
`strategies/credit_spreads.py`:

```python
# find:
from strategies.base import dte, iv_rank, occ_symbol, nearest_by_delta

# replace with:
from strategies.base import dte, iv_rank, occ_symbol, nearest_by_delta, filter_liquid_chain

# find (inside scan(), the expiration loop):
        for expiration, sides in chain.items():
            days = dte(expiration)

# replace with:
        max_spread_pct = s_cfg.get("max_leg_spread_pct", 0.20)
        for expiration, sides in chain.items():
            sides = filter_liquid_chain(sides, max_spread_pct)
            days = dte(expiration)
```

Apply the same pattern (import `filter_liquid_chain`, filter `sides` right
after `for expiration, sides in chain.items():`) in:
- `strategies/directional.py`
- `strategies/earnings_vol.py` (filter `sides` before computing `atm_call`/`atm_put`)
- `strategies/wheel.py` (both the CSP loop and the covered-call loop)

This means a wide-spread contract is simply invisible to `nearest_by_delta` —
the strategy will pick the next-nearest-delta contract that actually has a
tradeable spread, rather than flagging (and sending you an approval for) a
contract you'd get a terrible fill on.

---

## 4. `risk_manager.py` — hard gate as defense-in-depth (mirrors backtest's `max_spread_pct_skip`)

Strategy-level filtering (above) stops wide-spread *contracts* from being
chosen in the first place. This adds a second, independent check right
before approval — catches any leg that still ends up wide (e.g. the spread
widened between the scan and the gate check a few seconds later).

```python
# find:
class RiskManager:
  def __init__(self, cfg: dict, db, guardrails=None):
        self.cfg = cfg["risk"]
        self.db = db
        self.guardrails = guardrails

# replace with:
class RiskManager:
  def __init__(self, cfg: dict, db, guardrails=None, market_data=None):
        self.cfg = cfg["risk"]
        self.db = db
        self.guardrails = guardrails
        self.market_data = market_data
        self.max_leg_spread_pct = cfg.get("liquidity", {}).get("max_leg_spread_pct", 0.20)
```

Add a new method (anywhere in the class):

```python
def passes_spread_gate(self, candidate: dict) -> bool:
        """Hard gate: reject if ANY leg's current spread exceeds
        max_leg_spread_pct. Fails OPEN (returns True) if market_data wasn't
        wired in or a lookup errors — this is a liquidity sanity check, not
        a hard risk cap like max_risk_per_trade_pct, so a data hiccup
        shouldn't block an otherwise-good, already-filtered candidate."""
        if not self.market_data or not self.max_leg_spread_pct:
            return True
        for leg in candidate.get("legs", []):
            occ = leg.get("occ_symbol")
            if not occ:
                continue
            try:
                spread_pct = self.market_data.leg_spread_pct(occ)
            except Exception:
                log.debug("Spread check failed for %s — allowing through (fail-open)", occ)
                continue
            if spread_pct is not None and spread_pct > self.max_leg_spread_pct:
                log.info("Rejecting %s (%s): leg %s spread %.0f%% exceeds %.0f%% cap",
                          candidate["symbol"], candidate["strategy"], occ,
                          spread_pct * 100, self.max_leg_spread_pct * 100)
                return False
        return True
```

Wire it into `gate()`:

```python
# find:
def gate(self, candidate: dict, snapshot: dict) -> bool:
    if candidate.get("suggested_qty", 1) < 1:

# replace with:
def gate(self, candidate: dict, snapshot: dict) -> bool:
    if not self.passes_spread_gate(candidate):
        return False
    if candidate.get("suggested_qty", 1) < 1:
```

And in `scanner.py`'s `main()`, pass `market_data` in when constructing it:

```python
# find:
    risk = RiskManager(cfg, db, guardrails=guardrails)

# replace with:
    risk = RiskManager(cfg, db, guardrails=guardrails, market_data=market_data)
```

---

## 5. `scanner.py` — wire up the universe volume prefilter + the weekly IV-import cron job

**Imports** — add near the top:

```python
from optionstrategist_iv import run_import as run_optionstrategist_import, ExternalIVStore
```

**MarketDataAdapter construction** — pass the external IV store in:

```python
# find:
    iv_store = IVHistoryStore()
    ts_store = TimeSeriesStore()
    earnings_calendar = EarningsCalendar(cfg)
    market_data = MarketDataAdapter(client, cfg=cfg, iv_store=iv_store, earnings_calendar=earnings_calendar)

# replace with:
    iv_store = IVHistoryStore()
    ts_store = TimeSeriesStore()
    earnings_calendar = EarningsCalendar(cfg)
    external_iv_store = ExternalIVStore()
    market_data = MarketDataAdapter(client, cfg=cfg, iv_store=iv_store, earnings_calendar=earnings_calendar,
                                     external_iv_store=external_iv_store)
```

**Universe volume prefilter** — in `get_universe()`, filtering needs
`market_data`, which isn't currently passed in. Simplest change: filter in
`run_scan_cycle` right after `symbols = get_universe(...)`, since
`market_data` is already in scope there:

```python
# find (inside run_scan_cycle, right after the active_bans filter):
    log.info("Scanning %d symbols (%d banned symbols excluded)", len(symbols), len(active_bans))

# replace with:
    log.info("Scanning %d symbols (%d banned symbols excluded)", len(symbols), len(active_bans))

    min_avg_vol = cfg["universe"].get("min_avg_option_volume", 0)
    if min_avg_vol:
        liquid_symbols = []
        for s in symbols:
            try:
                if market_data.get_avg_volume(s) >= min_avg_vol:
                    liquid_symbols.append(s)
            except Exception:
                log.debug("Volume lookup failed for %s — excluding from this cycle", s)
        log.info("Liquidity prefilter: %d/%d symbols meet min_avg_option_volume=%s",
                  len(liquid_symbols), len(symbols), min_avg_vol)
        symbols = liquid_symbols
```

Note the honest caveat already in the `market_data.py` patch: this checks
*underlying* share volume, not option-contract volume (Schwab doesn't give
you historical per-contract volume cheaply at the universe-scan stage) —
it's a proxy, and a good one, but not literally what the config key name
implies. Renaming it to `min_avg_underlying_volume` in your own config
would be more honest; kept `min_avg_option_volume` above only because
that's the key you already asked to raise to 1000.

**Weekly IV-import cron job** — in `main()`, alongside your other
`scheduler.add_job` calls:

```python
    scheduler.add_job(
        lambda: run_optionstrategist_import(db_path="./bot.db"),
        "cron",
        day_of_week="sun",   # source data refreshes Saturdays; Sunday morning safely picks it up
        hour=6, minute=0,
    )
```

`run_import()` is idempotent per calendar day on its own (see
`optionstrategist_iv.already_imported_today`), so even if this job or a
manual run fires more than once in a day it won't re-hit the site.

---

## 6. `config.yaml` — new/changed keys

```yaml
universe:
  min_avg_option_volume: 1000   # was 500 — see note above, this is underlying volume, not option volume

liquidity:
  max_leg_spread_pct: 0.20      # used by risk_manager's hard gate (patch #4) and as the default
                                 # each strategy module falls back to if it doesn't set its own

strategies:
  credit_spreads:
    max_leg_spread_pct: 0.20    # optional per-strategy override, else falls back to liquidity.max_leg_spread_pct
  directional:
    max_leg_spread_pct: 0.20
  earnings_vol:
    max_leg_spread_pct: 0.20
  wheel:
    max_leg_spread_pct: 0.20
```

(If you'd rather not add per-strategy keys, just have each strategy module
read `cfg.get("liquidity", {}).get("max_leg_spread_pct", 0.20)` instead of
`s_cfg.get("max_leg_spread_pct", 0.20)` in the patch #3 snippet — either
works, per-strategy just lets you loosen it for wheel/credit_spreads and
keep it tight for earnings_vol/directional if you want.)

---

## 7. Running the importer outside the bot process (true cron/Task Scheduler)

You don't strictly need this if scanner.py is always running (the
APScheduler job in #5 covers it) — but a standalone OS-level schedule is
worth having as a backstop in case the bot process is down over a weekend.

**Windows (Task Scheduler)** — from an elevated prompt, adjust the path:

```bat
schtasks /create /tn "OptionStrategistIVImport" /tr "C:\Users\USER\Desktop\schwab-options-bot\venv\Scripts\python.exe C:\Users\USER\Desktop\schwab-options-bot\optionstrategist_iv.py --db C:\Users\USER\Desktop\schwab-options-bot\bot.db" /sc weekly /d SUN /st 06:00
```

**Linux/macOS (cron)** — `crontab -e`, add:

```
0 6 * * 0 cd /path/to/schwab-options-bot && ./venv/bin/python optionstrategist_iv.py --db ./bot.db >> ./iv_import.log 2>&1
```

Both are safe to run in addition to the in-process APScheduler job —
`run_import`'s idempotency check means the second one that fires in a
given day is a no-op.

---

## Summary of what changed and why

| Ask | What was actually wrong | Fix |
|---|---|---|
| IV rank usable sooner | `iv_history.py` needs 40-60 days of your own snapshots | Added `optionstrategist_iv.py` — real multi-year IV percentile, usable week 1. Lowered `MIN_OBSERVATIONS_FOR_RANK` to 20 as a secondary ramp-up for symbols outside that feed's coverage. |
| Skip illiquid names | `universe.min_avg_option_volume` existed in config.yaml but was **never actually read anywhere in the code** | Added `MarketDataAdapter.get_avg_volume()` and wired an actual filter into `run_scan_cycle` |
| Don't send wide-spread trades to approval | No per-contract spread check existed outside the backtest engine | Added `filter_liquid_chain()` in `strategies/base.py`, applied in all 4 strategy modules, plus a second hard gate in `risk_manager.py` mirroring `backtest_engine.py`'s existing `max_spread_pct_skip` |
