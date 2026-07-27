# Bundled snapshots

Fallback CSVs used only if a live fetch from yfiua/index-constituents fails
AND there's no local cache yet (e.g. very first run with no network access
to GitHub Pages). These are placeholders — populate them for real before
relying on this fallback path:

    python universe.py --refresh

This hits the live source once and writes a real snapshot into this folder
(nasdaq100.csv, sp500.csv) so the fallback is actually useful rather than
just these two-row stubs.
