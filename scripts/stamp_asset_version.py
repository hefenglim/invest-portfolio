"""Stamp local <script src>/<link href> asset references in web/*.html with ?v=<version>.

Why this exists (2026-07-07 stale-cache incident): the frontend has NO build step, so
asset URLs are hand-written. Browsers that cached web/*.js under heuristic freshness
(StaticFiles used to send no Cache-Control) kept serving stale scripts for days after a
deploy, breaking pages whose fresh HTML called new helpers (fmt.aiAttrib). The server
now sends ``Cache-Control: no-cache`` (see api/app.py ``_NoCacheStaticFiles``) — the
class fix — and this stamp changes the asset URL whenever the app version changes,
which flushes clients that cached assets BEFORE that header existed.

Single source of truth: ``portfolio_dash.__version__``. Rerunnable / idempotent —
it rewrites any existing ``?v=...`` token in place. Run after every version bump
(wired into the /ship-version checklist):

    .venv/Scripts/python scripts/stamp_asset_version.py

The contract tests in tests/contract/test_static_cache_discipline.py fail the suite
if any page's local asset reference is missing the current-version stamp.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from portfolio_dash import __version__  # noqa: E402

# A LOCAL asset reference: src="name.js" / href="name.css" (optionally with an existing
# ?v=... token). Bare relative filenames only — CDN/absolute URLs (https://...) never match.
_ASSET_REF = re.compile(
    r'(?P<attr>src|href)="(?P<name>[A-Za-z0-9._-]+\.(?:js|css))(?:\?v=[^"]*)?"'
)


def stamp(text: str, version: str) -> str:
    """Return ``text`` with every local .js/.css reference stamped ``?v=<version>``."""
    return _ASSET_REF.sub(lambda m: f'{m.group("attr")}="{m.group("name")}?v={version}"', text)


def main() -> int:
    web_dir = _ROOT / "web"
    changed = 0
    for html in sorted(web_dir.glob("*.html")):
        before = html.read_text(encoding="utf-8")
        after = stamp(before, __version__)
        if after != before:
            html.write_text(after, encoding="utf-8", newline="\n")
            changed += 1
            print(f"stamped {html.name}")
    print(f"done: {changed} file(s) updated to ?v={__version__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
