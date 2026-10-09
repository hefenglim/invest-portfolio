"""Every ``POST /api/export/*`` route must have a real frontend caller.

Written 2026-08-27 as the counter-evidence for two orphaned exports, and kept because the
way they stayed orphaned is not visible to any other gate:

* ``/api/export/tax-package`` — R1 added the 申報用原始成本 column (``realized_original`` plus
  its per-currency subtotal, the number that actually goes on a return) to a package the
  browser could not ask for.
* ``/api/export/ledgers`` — the whole-ledger zip, built and tested, reachable only by curl.

Both had a *card* in the settings 匯出中心, which is why neither read as missing: the cards
render a ⬇ glyph and a 「產生並下載」 button whose handler fired a **success toast** and
downloaded nothing (「已排入產生佇列」 — there is no queue). A button that reports success and
does nothing is worse than no button, and it is invisible to a wiring sweep that asks only
whether a control has a listener.

The check is deliberately structural — router source vs `web/` source — rather than a
behavioural test, because the failure mode is an endpoint nobody calls, and nothing that
exercises the endpoint can see that.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_ROUTER = _REPO / "portfolio_dash" / "api" / "routers" / "export.py"
_WEB = _REPO / "web"

#: Routes intentionally without a browser caller. EMPTY, and adding an entry needs a stated
#: reason — the whole point is that "nobody calls it" must be a decision, never a drift.
_NO_CALLER_ALLOWED: frozenset[str] = frozenset()


def _post_export_paths() -> set[str]:
    src = _ROUTER.read_text(encoding="utf-8")
    return set(re.findall(r'@router\.post\(\s*"(/export/[^"]+)"', src))


#: ``pdApi.get('<path>')`` calls are stripped before the search. ``/api/export/ledgers``
#: has BOTH a GET (what would the zip contain?) and a POST (build it); without this the
#: GET's literal satisfies the POST's coverage and the orphaned zip stays invisible —
#: which is exactly what happened on this test's own first run.
_GET_CALL = re.compile('pdApi\\.get\\(\\s*[\'"]/api/export/[^\'"]+[\'"]')


def _web_sources() -> str:
    parts = []
    for path in sorted(_WEB.rglob("*")):
        if path.suffix in {".js", ".html"} and path.name != "echarts.min.js":
            parts.append(path.read_text(encoding="utf-8"))
    return _GET_CALL.sub("", "\n".join(parts))


def test_every_post_export_route_is_reachable_from_the_browser() -> None:
    web = _web_sources()
    orphans = sorted(
        p for p in _post_export_paths()
        if p not in _NO_CALLER_ALLOWED and f"'/api{p}'" not in web and f'"/api{p}"' not in web
    )
    assert not orphans, (
        "these export endpoints exist but no page can ask for them: "
        + ", ".join(orphans)
    )


def test_the_export_centre_downloads_instead_of_claiming_a_queue() -> None:
    """The 匯出中心 cards must call the download seam, not congratulate the user.

    Pinned as its own assertion because the route-coverage test above is satisfied by ANY
    caller anywhere — including a page that merely mentions the path in a comment. This one
    is about the specific surface that lied.
    """
    src = (_WEB / "settings-alerts.js").read_text(encoding="utf-8")
    centre = src[src.index("/* ============ E7: 匯出中心 ============ */"):]
    # The CALL, not the phrase: the comment that records this defect quotes the old string
    # on purpose, and a test that forbade the words would forbid the history too.
    assert "toast('已排入產生佇列'" not in centre, (
        "the export centre still toasts a fake queue instead of downloading"
    )
    assert "pdApi.download" in centre, "the export centre never calls the download seam"
