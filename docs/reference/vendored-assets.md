# Vendored front-end assets

Third-party front-end code that ships **inside this repository** rather than being loaded from
a CDN at runtime. One entry today.

The authority for what is correct is **`tests/contract/test_vendored_assets.py`** — it holds
the pinned constants, and `scripts/vendor_echarts.py` imports them rather than restating them,
so the downloader and the guard cannot disagree. This page explains; the test decides.

---

## ECharts 5.5.0 — `web/echarts.min.js`

| | |
| --- | --- |
| Version | 5.5.0 |
| Source | `https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js` |
| Mirror | `https://unpkg.com/echarts@5.5.0/dist/echarts.min.js` |
| Size | 1,029,203 bytes |
| sha256 | `42f8329d989b6f6539dd2b15bbdf0d82025762ac112fbb60dc57b27d7bcf3946` |
| SRI | `sha384-o5uz97et3bErHvpKfD4Jz4n0JfhJDWABFuF4NP+iEEDxE1VwMWJ19QGR0lqFZnr6` |
| Licence | Apache-2.0 (banner retained verbatim at the head of the file) |
| Vendored | 2026-08-12, owner ruling |

### Why

A `cdn.jsdelivr.net` outage took the charts down. The failure was worse than blank charts:
`web/charts.js::initAll` threw at the first `echarts.init()`, *before* `wireModeOnce()`, so the
trend-mode / TWR-window / value-range buttons rendered normally and carried **no click
listeners at all** — controls that look alive and silently do nothing — and every subsequent
theme toggle raised an uncaught error.

The **webfont deliberately stays on Google's CDN**. It is presentational: `--font-ui` and
`--font-num` (`web/styles.css`) carry full fallback chains ending in a generic family, 9 of the
18 pages never load the font link at all, and the e2e suite already runs the entire app against
an **empty** Google Fonts stylesheet with the 18-page × 6-width horizontal-scroll guard green.
A font outage is cosmetic and continuously tested; a chart-library outage was neither.

### Why the file sits flat in `web/`, not in `web/vendor/`

`scripts/stamp_asset_version.py` and `tests/contract/test_static_cache_discipline.py` share a
regex that matches **bare relative filenames only** — `[A-Za-z0-9._-]+\.(?:js|css)`, no `/`, no
`:`. A file at `web/vendor/echarts.min.js` would be invisible to both: it would silently drop
out of the `?v=<version>` cache-busting discipline *and* out of the guard that enforces it.
Flat keeps the asset inside the existing rules instead of carving out an exception.

### Where it is loaded — four sites

Three are `<head>` tags; the fourth is the one a grep of the HTML will never find.

| # | Site | Kind |
| --- | --- | --- |
| 1 | `web/index.html` | parser-blocking `<head>` tag |
| 2 | `web/insights.html` | parser-blocking `<head>` tag |
| 3 | `web/settings.html` | parser-blocking `<head>` tag |
| 4 | `web/shell.js::pdEnsureDrawer` | **runtime-injected**, on every page, on first symbol-drawer open |

The other 15 pages deliberately get **no `<head>` tag**. Eager-loading ~1 MB on `cash.html` for
a drawer most sessions never open would trade a rare outage for a permanent regression; site 4
covers them lazily.

No `defer` / `async` on the three tags: the consumers (`charts.js` et al.) are ordinary
bottom-of-`<body>` scripts, and `defer` would invert execution order against them. No
`integrity` attribute either — it would put the hash in three more places, and the pin is
already enforced by the contract test, by `scripts/vendor_echarts.py`, and by
`scripts/verify_live.py` against the deployed instance.

### Line endings — `.gitattributes` is load-bearing

This repo is developed on Windows with `core.autocrlf=true`. A minified bundle has no NUL
bytes, so git's heuristic classifies it as **text** and rewrites LF → CRLF on checkout.
Measured 2026-08-12 before the fix: the file checked out as **1,029,248 bytes** with 45
conversions and a different sha256 — on a file nobody had edited. It still *runs* (JS tolerates
CRLF), so nothing but the digest would have noticed.

`.gitattributes` therefore carries `*.min.js -text`, and
`test_vendored_asset_is_exempt_from_eol_conversion` fails the build if that rule disappears.

### Upgrading

1. Pick the target version and get its real size + sha256 (e.g. download it once by hand, or
   run step 3 and read the mismatch message — it prints both `got` and `want`).
2. Edit **`tests/contract/test_vendored_assets.py`**: `ECHARTS_VERSION`, `ECHARTS_SIZE`,
   `ECHARTS_SHA256`. That file is the single machine-readable copy of the pin.
3. `.venv/Scripts/python scripts/vendor_echarts.py` — it fetches from **both** mirrors,
   requires them to be byte-identical, requires the pin to match, and only then writes.
4. `git add web/echarts.min.js` — the artifact is **committed**.
5. `pytest tests/contract/test_vendored_assets.py tests/e2e/test_echarts_selfhosted.py`.
6. Review the ECharts changelog for breaking chart-option changes; the dashboard, the symbol
   drawer, the insights calibration chart, the dividends card and the LLM-usage chart are all
   consumers.

> **Never** edit the constants merely to make the test pass. Flipping the pin to match whatever
> arrived only tests the test. The constants are what you intend to ship; the artifact must
> come to them.

### Not allowed

A **custom or partial** ECharts build (tree-shaken, `echarts/core` + hand-picked charts) would
need a Node toolchain and a build step, which `.claude/rules/stack.md` forbids. Copying a
pre-minified `dist` file into the repo is a file copy, not a build step — the locked "no
bundler, no build step" rule is untouched.
