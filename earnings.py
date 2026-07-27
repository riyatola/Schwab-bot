"""
Earnings calendar source: Finnhub (https://finnhub.io).

Why Finnhub: free tier (60 API calls/minute, no credit card) includes the
/calendar/earnings endpoint with forward-looking confirmed/estimated dates,
which is what earnings_vol.py needs. Sign up at https://finnhub.io/register
and set FINNHUB_API_KEY in your environment or config.yaml.

Verify current rate limits and endpoint shape against Finnhub's docs
(https://finnhub.io/docs/api/earnings-calendar) before relying on this in
production — free-tier terms on data vendors change without much notice.

Alternatives if you outgrow Finnhub's free tier or want confirmed-only dates:
  - Financial Modeling Prep (FMP) — /stable/earnings-calendar, free tier available
  - EODHD — /api/calendar/earnings, paid
  - api-ninjas — /v1/earningscalendar, free tier available
All three follow a similar "date + symbol" shape; swapping providers means
changing the two methods below, nothing else in the codebase.
"""

import logging
import os
import time
from datetime import date, datetime, timedelta

import requests

log = logging.getLogger("earnings")

FINNHUB_BASE = "https://finnhub.io/api/v1"
_CACHE_TTL_SECONDS = 6 * 3600  # earnings calendars don't need to be polled every scan cycle


class EarningsCalendar:
    def __init__(self, cfg: dict):
        self.api_key = (
            cfg.get("earnings", {}).get("finnhub_api_key")
            or os.environ.get("FINNHUB_API_KEY")
        )
        if not self.api_key:
            log.warning(
                "No Finnhub API key configured (earnings.finnhub_api_key in config.yaml "
                "or FINNHUB_API_KEY env var) — earnings_vol strategy will find no candidates."
            )
        self._calendar_cache = {}  # (from_date, to_date) -> (fetched_at, {symbol: date})
        self._historical_cache = {}  # symbol -> (fetched_at, [dates])

    def _get(self, path: str, params: dict) -> dict:
        params = {**params, "token": self.api_key}
        resp = requests.get(f"{FINNHUB_BASE}{path}", params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _fetch_window(self, from_date: date, to_date: date) -> dict:
        cache_key = (from_date.isoformat(), to_date.isoformat())
        cached = self._calendar_cache.get(cache_key)
        if cached and (time.time() - cached[0]) < _CACHE_TTL_SECONDS:
            return cached[1]

        if not self.api_key:
            return {}

        try:
            data = self._get("/calendar/earnings", {
                "from": from_date.isoformat(),
                "to": to_date.isoformat(),
            })
        except Exception as e:
            log.warning("Finnhub earnings calendar fetch failed: %s", e)
            return cached[1] if cached else {}

        by_symbol = {}
        for row in data.get("earningsCalendar", []):
            sym = row.get("symbol", "").upper()
            row_date = row.get("date")
            if sym and row_date:
                by_symbol[sym] = row_date  # last one wins if duplicated

        self._calendar_cache[cache_key] = (time.time(), by_symbol)
        return by_symbol

    def get_earnings_date(self, symbol: str, lookahead_days: int = 30):
        """Returns the next known earnings date for `symbol` within lookahead_days, or None."""
        today = date.today()
        window = self._fetch_window(today, today + timedelta(days=lookahead_days))
        raw = window.get(symbol.upper())
        if not raw:
            return None
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            return None

    def get_historical_earnings_dates(self, symbol: str, lookback_days: int = 900) -> list[date]:
        """Past earnings dates for computing historical move size. Finnhub's free tier
        exposes recent history via the same /calendar/earnings endpoint with a past
        date range (rather than a dedicated historical-only endpoint)."""
        # Use the new method internally for backwards compatibility
        data = self.get_historical_earnings_data(symbol, lookback_days)
        return [d for d, _, _ in data]

    def get_historical_earnings_data(self, symbol: str, lookback_days: int = 900) -> list[tuple[date, float, float]]:
        """Past earnings data as (date, actual_eps, estimated_eps)."""
        # Check cache first (we need to update the cache key since we're returning more data)
        cache_key = (symbol, lookback_days, "data")
        cached = self._historical_cache.get(cache_key)
        if cached and (time.time() - cached[0]) < _CACHE_TTL_SECONDS:
            return cached[1]

        if not self.api_key:
            return []

        today = date.today()
        try:
            data = self._get("/calendar/earnings", {
                "from": (today - timedelta(days=lookback_days)).isoformat(),
                "to": today.isoformat(),
                "symbol": symbol.upper(),
            })
        except Exception as e:
            log.warning("Finnhub historical earnings fetch failed for %s: %s", symbol, e)
            return cached[1] if cached else []

        earnings_data = []
        for row in data.get("earningsCalendar", []):
            raw_date = row.get("date")
            if not raw_date:
                continue
            try:
                d = datetime.strptime(raw_date, "%Y-%m-%d").date()
            except ValueError:
                continue
            actual = row.get("epsActual")
            estimate = row.get("epsEstimate")
            earnings_data.append((d, actual, estimate))

        earnings_data.sort()
        self._historical_cache[cache_key] = (time.time(), earnings_data)
        return earnings_data
