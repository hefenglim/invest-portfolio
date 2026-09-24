"""DEF-044 (functional test manual G-09 / J-06, 2026-09-24): every page that can reach the API
loads ``web/names.js`` BEFORE ``web/api.js``.

``api.js`` resolves the backend's ``{account:<id>}`` tokens through ``window.pdNames`` on every
response (DEF-023's seam). Five pages — settings / insights / instruments / news / data-center
— loaded ``api.js`` and never ``names.js``, so the fallback printed the bare id (排程中心:
「fx_drift 帳戶 moomoo_my」). No guard asked the question "does every page that uses the seam
load the resolver?"; the seam's own test ran the two files in a node sandbox and never looked
at a page.

The rule here is stricter than "somewhere on the page": names.js is a dependency-free IIFE, and
loading it FIRST removes the window in which an early response is resolved before the resolver
exists. ``shell.js`` counts as reaching the API too — ``pdEnsureApi()`` injects ``api.js`` on a
page that lacks the tag. The load-time half (``api.js`` reports a missing names.js as a console
error, which the page-smoke e2e turns red) is run in ``test_account_ref_seam.py``.

Script order is read with an HTML parser, not a regex: the order of the ``<script src>``
elements IS the behaviour the browser executes.
"""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

import pytest

_WEB = Path(__file__).resolve().parents[2] / "web"
#: Scripts that put ``window.pdApi`` on the page (shell.js lazily injects api.js).
_API_DOORS = ("api.js", "shell.js")


class _Scripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.srcs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            src = dict(attrs).get("src")
            if src:
                self.srcs.append(src.split("?", 1)[0])


def _order(html: str) -> list[str]:
    parser = _Scripts()
    parser.feed(html)
    return parser.srcs


def _violation(srcs: list[str]) -> str | None:
    """Why this script order breaks the rule, or None when it holds."""
    doors = [i for i, s in enumerate(srcs) if s in _API_DOORS]
    if not doors:
        return None
    if "names.js" not in srcs:
        return f"reaches the API ({srcs[doors[0]]}) and never loads names.js"
    if srcs.index("names.js") > doors[0]:
        return f"loads names.js AFTER {srcs[doors[0]]}"
    return None


_PAGES = sorted(p for p in _WEB.glob("*.html"))


@pytest.mark.parametrize("page", _PAGES, ids=[p.name for p in _PAGES])
def test_every_page_that_reaches_the_api_loads_names_js_first(page: Path) -> None:
    why = _violation(_order(page.read_text(encoding="utf-8")))
    assert why is None, f"{page.name} {why} — account tokens would degrade to raw ids"


def test_the_rule_covers_the_pages_that_need_it() -> None:
    """Detection power: the rule is not vacuous — every page with scripts reaches the API."""
    reaching = [p.name for p in _PAGES
                if any(s in _API_DOORS for s in _order(p.read_text(encoding="utf-8")))]
    for name in ("settings.html", "insights.html", "instruments.html", "news.html",
                 "data-center.html", "index.html", "trades.html", "login.html"):
        assert name in reaching, name


def test_the_guard_bites() -> None:
    missing = '<script src="api.js?v=1"></script><script src="shell.js"></script>'
    late = '<script src="api.js"></script><script src="names.js"></script>'
    shell_only = '<script src="shell.js"></script><script src="app.js"></script>'
    good = '<script src="names.js?v=1"></script><script src="api.js?v=1"></script>'
    assert _violation(_order(missing)) == "reaches the API (api.js) and never loads names.js"
    assert _violation(_order(late)) == "loads names.js AFTER api.js"
    assert _violation(_order(shell_only)) is not None
    assert _violation(_order(good)) is None
    assert _violation(_order('<script src="app.js"></script>')) is None
