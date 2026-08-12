"""ECharts is self-hosted, and the dashboard survives its absence (owner ruling 2026-08-12).

``tests/contract/test_vendored_assets.py`` scans the source. These two drive the browser,
because a source scan cannot see either of the things that actually matter here.

**T5 covers the load site that had no coverage at all.** ``web/shell.js::pdEnsureDrawer``
lazy-loads ECharts when the symbol drawer is first opened — on EVERY page, not just the three
that carry a ``<head>`` tag. Every existing drawer test reaches the drawer from
``/index.html`` (``test_symbol_drawer_tx_chart_flow.py``, ``test_whatif_drawer_flow.py``, and
``test_corporate_actions_flow.py::_open_drawer``, which navigates to ``/index.html`` before
calling ``pdOpenSymbol``), where the ``<head>`` tag has already set ``window.echarts`` — so
``pdEnsureDrawer``'s ``window.echarts ? null : pdLoadScript(...)`` branch never took the load
path. This test opens the drawer from ``/instruments.html``, which has no ``<head>`` tag, so
the lazy load is exercised for the first time.

**T6 covers the degraded path.** With the file absent the failure was not blank charts:
``charts.js::initAll`` threw at ``echarts.init`` before reaching ``wireModeOnce()``, so
``#trend-mode`` / ``#twr-windows`` / ``#value-ranges`` were rendered but carried NO click
listeners — dead controls with no cue — and every later theme toggle raised an uncaught error.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import pytest
from playwright.sync_api import ConsoleMessage, Page, Request, Route, expect

from tests.e2e.conftest import ALLOWED_REMOTE_HOSTS, _is_app_url

_ECHARTS_PATH = "/echarts.min.js"


@pytest.mark.e2e
def test_drawer_loads_echarts_locally_from_a_non_dashboard_page(
    live_server: str, fresh_page: Page
) -> None:
    """/instruments.html → click a symbol → the drawer mounts, having fetched ECharts locally.

    A fresh context (not the shared session page) so the browser cache cannot be pre-warmed
    by an earlier ``/index.html`` visit, which would hide a regression by never re-requesting.

    Asserts, in order: the page does NOT eager-load the 1 MB library (the 15 non-chart pages
    deliberately have no ``<head>`` tag — see the ruling); opening the drawer DOES fetch it,
    from the app's own origin; the canvas really mounts (proving the library evaluated, not
    merely that a 200 came back); and the only remote host touched along the way is the
    webfont's, which the ruling deliberately left in place.
    """
    page = fresh_page
    requests: list[str] = []
    third_party: list[str] = []
    console_errors: list[str] = []

    def _on_request(req: Request) -> None:
        requests.append(req.url)
        if not _is_app_url(req.url):
            third_party.append(req.url)

    def _on_console(msg: ConsoleMessage) -> None:
        if msg.type == "error":
            console_errors.append(msg.text)

    page.on("request", _on_request)
    page.on("console", _on_console)

    page.goto(live_server + "/instruments.html", wait_until="load")
    page.wait_for_selector("#inst-body tr .sym-code")

    eager = [u for u in requests if _ECHARTS_PATH in u]
    assert not eager, (
        "/instruments.html eager-loads ECharts — the drawer is supposed to lazy-load it, so "
        f"pages that never open a drawer never pay the ~1 MB: {eager}"
    )

    row = page.locator("#inst-body tr").filter(has=page.locator(".sym-code", has_text="2330"))
    row.locator(".sym-cell").first.click()

    # #sd-chart only gets a <canvas> child once echarts.init() ran against real price history.
    page.wait_for_selector(".sd-drawer #sd-chart canvas")

    fetched = [u for u in requests if _ECHARTS_PATH in u]
    assert fetched, (
        "the drawer mounted without ever requesting " + _ECHARTS_PATH + " — either the page "
        "eager-loaded it after all, or shell.js is loading the library from somewhere else"
    )
    assert all(_is_app_url(u) for u in fetched), f"ECharts came from off-origin: {fetched}"
    unexpected = [u for u in third_party if urlparse(u).netloc not in ALLOWED_REMOTE_HOSTS]
    assert not unexpected, f"a NEW remote dependency appeared: {unexpected}"
    assert not console_errors, f"/instruments.html drawer console errors: {console_errors}"


@pytest.mark.e2e
def test_dashboard_degrades_legibly_without_echarts(
    live_server: str, fresh_page: Page
) -> None:
    """ECharts served empty → the dashboard says so and KEEPS ITS BUTTONS ALIVE.

    The page-level route wins over the context-level third-party stub, so this simulates the
    library being unavailable while everything else works — the exact shape of the outage the
    vendoring exists to prevent, now also the shape of a botched deploy that ships the HTML
    without the asset.

    The button assertion is the point. Blank charts are an obvious, self-explaining failure;
    controls that render normally and silently do nothing are not.
    """
    page = fresh_page
    page_errors: list[str] = []
    console_errors: list[str] = []

    def _kill_echarts(route: Route) -> None:
        route.fulfill(status=200, content_type="application/javascript", body="")

    page.route("**/echarts.min.js*", _kill_echarts)
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)

    page.goto(live_server + "/index.html", wait_until="load")
    page.wait_for_selector(".kpi-card")  # the async /api/dashboard render completed

    expect(page.locator("#trend-chart .empty-state")).to_be_visible()
    expect(page.locator("#sector-chart .empty-state")).to_be_visible()

    # The controls are alive: switching to 績效比較 still re-titles the panel and marks the tab.
    twr_btn = page.locator('#trend-mode .range-btn[data-mode="twr"]')
    twr_btn.click()
    expect(page.locator("#trend-title")).to_have_text("績效比較（時間加權報酬）")
    expect(twr_btn).to_have_class(re.compile(r"\bactive\b"))

    # A theme toggle after the failed load must not throw (charts.js's handler used to assume
    # that a non-null payload implied built charts).
    page.evaluate("() => window.dispatchEvent(new CustomEvent('pd-theme-change'))")
    page.wait_for_timeout(300)

    assert not page_errors, f"/index.html without ECharts raised: {page_errors}"
    assert not console_errors, f"/index.html without ECharts logged: {console_errors}"
