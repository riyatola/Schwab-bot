"""
Weekly importer for McMillan Analysis Corp's free volatility data page:
https://www.optionstrategist.com/calculators/free-volatility-data

WHAT THIS IS
------------
A plain-text, monospace table (updated Saturdays) of, per symbol:
  hv20 / hv50 / hv100  - historical (realized) vol, 20/50/100-day
  DATE                 - date of the last option data used
  curiv                - current implied volatility (in percentage points,
                          e.g. 26.58 means 26.58%, NOT 0.2658)
  Days/Percentile      - e.g. "600/ 74%ile" = McMillan has 600 daily IV
                          readings on file for this symbol, and today's
                          reading is higher than 74% of them.
  Close                - last underlying close

WHY THIS MATTERS FOR iv_history.py
-----------------------------------
iv_history.py builds IV rank from scratch out of your own daily snapshots,
and needs ~40 (MIN_OBSERVATIONS_FOR_RANK) before it trusts its own number.
McMillan's "Days/Percentile" column is the SAME concept (where does today's
IV sit in its own trailing history) but already computed from potentially
hundreds of trading days of real history you don't have to accumulate
yourself. So instead of waiting two months, you can use their percentile
as your IV rank starting the first week you run this importer.

WHY THIS IS A SEPARATE TABLE, NOT A ROW IN iv_history.iv_history
------------------------------------------------------------------
AUDIT_FINDINGS.md #5 already flags that market_data._parse_contract's IV
units (Schwab's `volatility` field) are unverified - could be "32.5" or
"0.325". iv_history.py's own IVHistoryStore.record() is fed straight from
that unverified field. optionstrategist.com's `curiv` is UNAMBIGUOUSLY
percentage points (verified against $VIX row: curiv ~15-20 tracks the
real VIX level). Silently commingling two feeds with possibly different
scales in the same rolling-window table would quietly corrupt every IV
rank computed from it - the kind of bug that doesn't throw, it just
biases every gate that reads min_iv_rank/max_iv_rank. So this module
keeps its own table (external_iv_rank) and market_data.py is patched
(see PATCHES.md) to prefer it explicitly, not merge it blindly.

USAGE
-----
Standalone (one-off or cron/Task Scheduler):
    python optionstrategist_iv.py                  # imports into ./bot.db
    python optionstrategist_iv.py --db ./bot.db
    python optionstrategist_iv.py --symbols AAPL,MSFT,NVDA   # limit universe

Library use (wired into scanner.py's scheduler - see PATCHES.md):
    from optionstrategist_iv import run_import
    run_import(db_path="./bot.db")

RATE LIMITING / ETIQUETTE
--------------------------
The data only changes once a week (Saturdays). Don't poll this more than
once a day regardless of your cron schedule - run_import() no-ops (log +
return) if it already has a row for today's ISO date, so an accidental
daily cron won't hammer their server.
"""

import argparse
import html
import logging
import re
import sqlite3
from datetime import date, datetime

import requests

log = logging.getLogger("optionstrategist_iv")

SOURCE_URL = "https://www.optionstrategist.com/calculators/free-volatility-data"
_REQUEST_TIMEOUT = 30
_USER_AGENT = "Mozilla/5.0 (compatible; schwab-options-bot IV importer; personal use)"

# One data row looks like (whitespace-padded, hv columns sometimes replaced
# by literal "SERIAL OPTION" text - we don't need hv20/50/100 for our
# purposes, so the regex just skips over them non-greedily):
#   AAPL                                36    29    27  260710   26.58   600/ 61%ile  315.33
#   @AEH                             SERIAL OPTION      260710   27.00   388/ 91%ile  126.80
# Percentile and Close are sometimes glued together with no space when
# Close is a big number (e.g. "590/ 59%ile53807.00"), so Close is captured
# as "whatever numeric follows immediately after %ile".
ROW_RE = re.compile(
    r'^(?P<symbol>[\$@A-Za-z0-9\.\-]{1,12})\s+.*?'
    r'(?P<date>\d{6})\s+'
    r'(?P<curiv>\d+(?:\.\d+)?)\s+'
    r'(?P<nobs>\d+)/\s*'
    r'(?P<pct>\d+)%ile\s*'
    r'(?P<close>-?\d+(?:\.\d+)?)\s*$'
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS external_iv_rank (
    symbol TEXT NOT NULL,
    as_of_date TEXT NOT NULL,      -- ISO date of the OPTION data (row's DATE column), not the scrape date
    scraped_at TEXT NOT NULL,      -- when we actually fetched it
    curiv_pct REAL,                -- implied vol in percentage points, e.g. 26.58
    n_observations INTEGER,        -- McMillan's own trailing sample size
    iv_percentile REAL,            -- 0-100, McMillan's own computed percentile -> use this as IV rank
    underlying_close REAL,
    PRIMARY KEY (symbol, as_of_date)
);
CREATE INDEX IF NOT EXISTS idx_ext_iv_symbol ON external_iv_rank(symbol);

CREATE TABLE IF NOT EXISTS external_iv_import_log (
    scrape_date TEXT PRIMARY KEY,  -- ISO date we ran the importer (not the data's own date)
    rows_parsed INTEGER,
    rows_stored INTEGER
);
"""


def _strip_tags(html_text: str) -> str:
    """Crude tag stripper. Good enough here because the data lives inside a
    <pre>/<textarea>-style block where line breaks are real newlines in the
    HTML source, not <br> tags - stripping tags with a regex preserves them.
    Avoids a hard dependency on BeautifulSoup (not in requirements.txt)."""
    text = re.sub(r"<script.*?</script>", " ", html_text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


def fetch_raw_html(url: str = SOURCE_URL, timeout: int = _REQUEST_TIMEOUT) -> str:
    resp = requests.get(url, headers={"User-Agent": _USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def parse_rows(html_text: str, symbols_filter: set[str] = None) -> list[dict]:
    """Returns a list of dicts: symbol, as_of_date (date), curiv_pct,
    n_observations, iv_percentile, underlying_close. Skips anything that
    doesn't match the row shape (headers, nav, footer banners) rather than
    raising - the page has a couple of repeated header/footer banners
    mid-table (pagination artifacts) that should just be ignored."""
    text = _strip_tags(html_text)
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = ROW_RE.match(line)
        if not m:
            continue
        symbol = m.group("symbol").upper()
        if symbols_filter and symbol not in symbols_filter:
            continue
        try:
            as_of = datetime.strptime(m.group("date"), "%y%m%d").date()
        except ValueError:
            continue
        rows.append({
            "symbol": symbol,
            "as_of_date": as_of,
            "curiv_pct": float(m.group("curiv")),
            "n_observations": int(m.group("nobs")),
            "iv_percentile": float(m.group("pct")),
            "underlying_close": float(m.group("close")),
        })
    return rows


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def already_imported_today(conn: sqlite3.Connection) -> bool:
    today = date.today().isoformat()
    row = conn.execute(
        "SELECT 1 FROM external_iv_import_log WHERE scrape_date=?", (today,)
    ).fetchone()
    return row is not None


def store_rows(conn: sqlite3.Connection, rows: list[dict]) -> int:
    now = datetime.utcnow().isoformat()
    payload = [
        (r["symbol"], r["as_of_date"].isoformat(), now, r["curiv_pct"],
         r["n_observations"], r["iv_percentile"], r["underlying_close"])
        for r in rows
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO external_iv_rank "
        "(symbol, as_of_date, scraped_at, curiv_pct, n_observations, iv_percentile, underlying_close) "
        "VALUES (?,?,?,?,?,?,?)",
        payload,
    )
    conn.execute(
        "INSERT OR REPLACE INTO external_iv_import_log (scrape_date, rows_parsed, rows_stored) VALUES (?,?,?)",
        (date.today().isoformat(), len(rows), len(payload)),
    )
    conn.commit()
    return len(payload)


def run_import(db_path: str = "./bot.db", symbols: list[str] = None, force: bool = False) -> int:
    """Main entry point. Idempotent per calendar day (the source data only
    changes weekly, so re-running same-day is a no-op unless force=True).
    Returns the number of symbol rows stored (0 if skipped/no data)."""
    conn = _connect(db_path)
    if not force and already_imported_today(conn):
        log.info("Already imported optionstrategist.com data today - skipping (use force=True to override).")
        return 0

    log.info("Fetching %s", SOURCE_URL)
    try:
        raw = fetch_raw_html()
    except Exception as e:
        log.warning("Fetch failed: %s", e)
        return 0

    symbols_filter = {s.upper() for s in symbols} if symbols else None
    rows = parse_rows(raw, symbols_filter=symbols_filter)
    if not rows:
        log.warning("Parsed 0 rows - page format may have changed, check ROW_RE against a fresh fetch.")
        return 0

    stored = store_rows(conn, rows)
    log.info("Stored IV percentile data for %d symbols (as-of dates vary by symbol's own last option data).",
              stored)
    return stored


class ExternalIVStore:
    """Read-side helper for market_data.py (see PATCHES.md). Wraps the same
    bot.db external_iv_rank table this module writes to."""

    def __init__(self, db_path: str = "./bot.db"):
        self.conn = _connect(db_path)

    def percentile(self, symbol: str, max_age_days: int = 10) -> float | None:
        """Returns McMillan's own IV percentile (0-100) for `symbol` if we
        have a row within max_age_days of today, else None (caller should
        fall back to iv_history.py's own rank, then the realized-vol
        proxy - same fallback chain market_data.get_iv_rank already uses).
        max_age_days defaults to 10 to comfortably span the weekly
        (Saturday) refresh cadence plus a couple of days of slack."""
        row = self.conn.execute(
            "SELECT as_of_date, iv_percentile FROM external_iv_rank "
            "WHERE symbol=? ORDER BY as_of_date DESC LIMIT 1",
            (symbol.upper(),),
        ).fetchone()
        if not row:
            return None
        as_of_date, pct = row
        age_days = (date.today() - datetime.strptime(as_of_date, "%Y-%m-%d").date()).days
        if age_days > max_age_days:
            return None
        return pct


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="./bot.db", help="Path to bot.db (default ./bot.db)")
    parser.add_argument("--symbols", default=None,
                         help="Comma-separated symbol allowlist (default: import everything on the page)")
    parser.add_argument("--force", action="store_true", help="Re-import even if already run today")
    args = parser.parse_args()

    symbol_list = [s.strip() for s in args.symbols.split(",")] if args.symbols else None
    n = run_import(db_path=args.db, symbols=symbol_list, force=args.force)
    print(f"Imported/updated {n} symbol rows into {args.db} (table external_iv_rank).")
