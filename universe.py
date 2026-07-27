"""
Index constituents source.

Schwab's API doesn't expose index membership, so we pull from
yfiua/index-constituents (https://github.com/yfiua/index-constituents),
which auto-updates NASDAQ-100 and S&P 500 constituent lists monthly from
Wikipedia and serves them as flat CSVs with no API key required:

  https://yfiua.github.io/index-constituents/constituents-nasdaq100.csv
  https://yfiua.github.io/index-constituents/constituents-sp500.csv

Both are third-party mirrors of Wikipedia data (itself sourced from the
official index provider pages), so they can lag a real-time index change
by up to a few weeks around reconstitution events — acceptable for a
scanner universe, not something to rely on for anything index-tracking-precise.

Fallback chain per index: live fetch -> local disk cache (if fresh enough)
-> local disk cache (stale, used anyway with a warning) -> bundled static
snapshot shipped in this repo (data/*.csv), so the scanner never hard-fails
just because GitHub Pages is unreachable.
"""

import csv
import io
import logging
import time
from pathlib import Path

import requests

log = logging.getLogger("universe")

SOURCES = {
    "NASDAQ100": "https://yfiua.github.io/index-constituents/constituents-nasdaq100.csv",
    "SP500": "https://yfiua.github.io/index-constituents/constituents-sp500.csv",
}

CACHE_DIR = Path("./data/universe_cache")
BUNDLED_DIR = Path("./data/bundled_snapshots")
CACHE_MAX_AGE_SECONDS = 7 * 24 * 3600  # treat as "fresh" for a week; indices don't churn daily


def _symbol_column(fieldnames: list[str]) -> str:
    for candidate in ("symbol", "Symbol", "ticker", "Ticker", "code", "Code"):
        if candidate in fieldnames:
            return candidate
    raise ValueError(f"Couldn't find a symbol column in {fieldnames}")


def _parse_csv(text: str) -> list[str]:
    reader = csv.DictReader(io.StringIO(text))
    col = _symbol_column(reader.fieldnames or [])
    symbols = []
    for row in reader:
        sym = row[col].strip().upper()
        if sym:
            # Schwab/OCC use '.' or '/' style for share classes (e.g. BRK.B);
            # some mirrors use '-' (BRK-B). Normalize to Schwab's convention.
            symbols.append(sym.replace("-", "."))
    return symbols


def _cache_path(index_name: str) -> Path:
    return CACHE_DIR / f"{index_name.lower()}.csv"


def _bundled_path(index_name: str) -> Path:
    return BUNDLED_DIR / f"{index_name.lower()}.csv"


def get_constituents(index_name: str, force_refresh: bool = False) -> list[str]:
    if index_name not in SOURCES:
        raise ValueError(f"Unknown index '{index_name}', expected one of {list(SOURCES)}")

    cache_path = _cache_path(index_name)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if not force_refresh and cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < CACHE_MAX_AGE_SECONDS:
            return _parse_csv(cache_path.read_text(encoding="utf-8"))

    try:
        resp = requests.get(SOURCES[index_name], timeout=10)
        resp.raise_for_status()
        cache_path.write_text(resp.text, encoding="utf-8")
        log.info("Refreshed %s constituents (%d bytes)", index_name, len(resp.text))
        return _parse_csv(resp.text)
    except Exception as e:
        log.warning("Live fetch of %s constituents failed (%s); falling back", index_name, e)

    if cache_path.exists():
        log.warning("Using stale cached %s constituents from %s", index_name, cache_path)
        return _parse_csv(cache_path.read_text(encoding="utf-8"))

    bundled = _bundled_path(index_name)
    if bundled.exists():
        log.warning("Using bundled snapshot for %s (%s) — refresh this periodically", index_name, bundled)
        return _parse_csv(bundled.read_text(encoding="utf-8"))

    raise RuntimeError(
        f"No source available for {index_name}: live fetch failed, no cache, no bundled snapshot. "
        f"Run `python universe.py --refresh` with network access at least once."
    )


def get_universe(index_names: list[str], extra_symbols: list[str], exclude_symbols: list[str]) -> list[str]:
    symbols = set(extra_symbols)
    for name in index_names:
        symbols.update(get_constituents(name))
    return sorted(symbols - set(exclude_symbols))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true", help="Force refresh and save a bundled snapshot")
    args = parser.parse_args()

    for idx in SOURCES:
        syms = get_constituents(idx, force_refresh=args.refresh)
        print(f"{idx}: {len(syms)} symbols")
        if args.refresh:
            BUNDLED_DIR.mkdir(parents=True, exist_ok=True)
            _bundled_path(idx).write_text(_cache_path(idx).read_text(encoding="utf-8"), encoding="utf-8")
            print(f"  saved bundled snapshot -> {_bundled_path(idx)}")
