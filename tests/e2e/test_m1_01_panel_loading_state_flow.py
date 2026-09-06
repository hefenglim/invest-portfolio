"""E2E M1-01: while /api/dashboard is in flight the dashboard's four reserved-height hosts
(and #twr-chart while /api/performance/twr is in flight) must SAY they are loading, and
must stop saying so on every terminal path — data, empty payload, or failure.

Measured before the fix (E2, /api/dashboard held 4 s; the real fetch takes 1.5-2.6 s):
``kpi_children:0, holdings_rows:0, busy_spins:0, loading_text:false, skeletons:0,
trend_children:0`` — a 190px empty KPI band, a 360px empty trend chart, a 280px empty
holdings table and a 270px empty sector chart, with nothing in any of them but the 2px
network bar at the very top of the window. The whole site's only loading convention was
that bar; no panel had a loading state at all.

The convention this file pins (owner ruling: TEXT, not a shimmer skeleton; scope: the
four index hosts + TWR — the rest of the site is a later, per-page roll-out):

  * a host carries ``.is-loading`` and its ``::after`` paints 「載入中…」 — generated
    content, NOT a child node, so nothing lands inside ``<tbody>`` (every e2e file waits on
    ``#holdings-body tr`` as its "loaded" signal) and nothing sits under ``echarts.init``;
  * the four index hosts carry the class IN THE HTML, so the panels say 載入中… from first
    paint — before app.js has even downloaded — and the JS only ever REMOVES it;
  * removal happens at the top of every terminal path, before any data branch, so an empty
    payload draws its real empty state (``尚無趨勢資料`` / 0 rows / ``—`` cards) and a failed
    fetch leaves ``#dash-load-error`` standing ALONE. The state is "unknown" until the
    answer is in, and only then does the page say what it knows — the same line
    ``web/instruments.js`` draws with its ``listKnown`` flag (M6-04); here the flag is
    structural (nothing on this page renders before ``D`` resolves), so the class is the
    flag.

Every assertion reads the ``::after`` computed content + the host's visibility rather than
``innerText`` (generated content is not part of innerText), and the width sweep measures
the page WHILE the placeholder is up, because an overlay that widens the document would
be a new defect of exactly the class ``test_no_horizontal_scroll.py`` exists to catch.
"""

import sqlite3
from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, Route, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory

_LOADING = "載入中"
_HOSTS = ("#kpi-band", ".table-wrap:has(#holdings-body)", "#trend-chart", "#sector-chart")
_TWR = "#twr-chart"
_DASH = "**/api/dashboard"
_TWR_API = "**/api/performance/twr*"

# One host's loading face: the class, the generated text, and whether a viewer can see it.
_STATE_JS = """(sel) => {
  const n = document.querySelector(sel);
  if (!n) return null;
  const after = getComputedStyle(n, '::after').content;
  return {
    is_loading: n.classList.contains('is-loading'),
    says_loading: after.indexOf('載入中') !== -1,
    after: after,
    visible: n.checkVisibility(),
    height: Math.round(n.getBoundingClientRect().height),
    text: n.innerText.trim().slice(0, 40),
  };
}"""
_ANY_LOADING_JS = """() => {
  const cls = document.querySelectorAll('.is-loading').length;
  const says = Array.from(document.querySelectorAll('*')).filter(
    (n) => getComputedStyle(n, '::after').content.indexOf('載入中') !== -1).length;
  return {cls: cls, says: says, body: document.body.innerText.includes('載入中')};
}"""
_SCROLL_JS = """() => {
  const se = document.scrollingElement;
  return {sw: se.scrollWidth, cw: se.clientWidth};
}"""


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _seed_none(conn: sqlite3.Connection) -> None:
    """Accounts only — a fresh install: holdings, trend, sector and TWR are all empty."""
    seed_accounts(conn)
    conn.commit()


def _watch(page: Page) -> tuple[list[str], list[str]]:
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(getattr(m, "text", repr(m)))
            if getattr(m, "type", None) == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    return console_errors, page_errors


def _state(page: Page, sel: str) -> dict[str, object]:
    st = page.evaluate(_STATE_JS, sel)
    assert st is not None, f"{sel} is not on the page"
    return dict(st)


def _assert_loading(page: Page, sel: str, during: str) -> None:
    st = _state(page, sel)
    assert st["says_loading"] and st["visible"] and int(str(st["height"])) > 0, (
        f"{sel} shows nothing while {during} is in flight: {st}")


def _assert_settled(page: Page, sel: str, after: str) -> None:
    st = _state(page, sel)
    assert not st["is_loading"] and not st["says_loading"], (
        f"{sel} still says 載入中… after {after}: {st}")


def _assert_nothing_loading(page: Page, after: str) -> None:
    got = page.evaluate(_ANY_LOADING_JS)
    assert got["cls"] == 0 and got["says"] == 0 and not got["body"], (
        f"a loading state survived {after}: {got}")


def _hold(page: Page, pattern: str) -> list[Route]:
    held: list[Route] = []
    page.route(pattern, lambda r: held.append(r))   # hold, do not answer
    return held


def _release(page: Page, pattern: str, held: list[Route]) -> None:
    assert held, f"{pattern} was never requested"
    held[0].continue_()
    page.unroute(pattern)


def _twr_settled(page: Page) -> None:
    page.wait_for_function(
        "() => { const c = document.querySelector('#twr-chart');"
        " return c && !c.hidden"
        " && (c.querySelector('canvas') || c.querySelector('.empty-state')); }")


# ---------------------------------------------------------------------------------------
def test_index_hosts_say_loading_while_the_dashboard_is_in_flight(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """The red one: hold /api/dashboard, and every reserved-height host must visibly say
    載入中… — then hold /api/performance/twr and the TWR host must too. Releasing each
    hold must clear the state (counter-proof 1: it never outlives the answer)."""
    base = flow_server(_seed_golden)
    page = fresh_page
    console_errors, page_errors = _watch(page)

    held = _hold(page, _DASH)
    page.goto(base + "/index.html", wait_until="load")
    page.wait_for_function("() => document.readyState === 'complete'")
    assert held, "the dashboard request was not intercepted"
    for sel in _HOSTS:
        _assert_loading(page, sel, "/api/dashboard")
    # The TWR host is hidden in 市值 mode and must NOT be pre-announced as loading.
    assert not _state(page, _TWR)["is_loading"], "#twr-chart claims loading before any fetch"

    _release(page, _DASH, held)
    page.wait_for_selector(".kpi-card")
    page.wait_for_selector("#holdings-body tr")
    for sel in _HOSTS:
        _assert_settled(page, sel, "the dashboard landed")
    _assert_nothing_loading(page, "the dashboard landed")

    held_twr = _hold(page, _TWR_API)
    page.click('#trend-mode .range-btn[data-mode="twr"]')
    expect(page.locator(_TWR)).to_be_visible()
    _assert_loading(page, _TWR, "/api/performance/twr")
    _release(page, _TWR_API, held_twr)
    _twr_settled(page)
    _assert_settled(page, _TWR, "the TWR series landed")
    _assert_nothing_loading(page, "the TWR series landed")

    assert not console_errors and not page_errors, (
        f"console={console_errors!r} page={page_errors!r}")


def test_an_empty_ledger_settles_into_its_empty_state_not_the_loading_text(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """Counter-proof 2: a fresh install has NO holdings, trend, sector or TWR data. Once the
    (empty) answer is in, every host must draw its real empty state — 載入中… left standing
    would be exactly the placeholder-impersonating-an-empty-state lie M6-04 closed."""
    base = flow_server(_seed_none)
    page = fresh_page
    console_errors, page_errors = _watch(page)

    page.goto(base + "/index.html", wait_until="load")
    page.wait_for_selector(".kpi-card")
    for sel in _HOSTS:
        _assert_settled(page, sel, "an empty dashboard landed")
    _assert_nothing_loading(page, "an empty dashboard landed")
    assert page.locator("#holdings-body tr").count() == 0, "a fresh install has no holdings"
    trend = page.inner_text("#trend-chart")
    assert "尚無" in trend or "不足" in trend, f"trend host is not in its empty state: {trend!r}"

    page.click('#trend-mode .range-btn[data-mode="twr"]')
    page.wait_for_selector("#twr-chart .empty-state")
    _assert_settled(page, _TWR, "an empty TWR answer landed")
    assert _LOADING not in page.inner_text(_TWR)

    assert not console_errors and not page_errors, (
        f"console={console_errors!r} page={page_errors!r}")


def test_a_failed_fetch_clears_the_loading_state_and_leaves_the_error_alone(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """Counter-proof 3: a 500 on /api/dashboard must leave exactly ONE thing on the page —
    #dash-load-error — with no host still saying 載入中…; and a 500 on the TWR series must
    settle #twr-chart into its own failure notice the same way."""
    base = flow_server(_seed_golden)
    page = fresh_page
    console_errors, page_errors = _watch(page)

    held = _hold(page, _DASH)
    page.goto(base + "/index.html", wait_until="load")
    page.wait_for_function("() => document.readyState === 'complete'")
    assert held, "the dashboard request was not intercepted"
    held[0].fulfill(status=500, content_type="application/json", body='{"detail":"boom"}')
    page.wait_for_selector("#dash-load-error")
    expect(page.locator("#dash-load-error")).to_have_count(1)
    for sel in _HOSTS:
        _assert_settled(page, sel, "the dashboard failed")
    _assert_nothing_loading(page, "the dashboard failed")
    page.unroute(_DASH)

    # TWR failure needs a LOADED page (a failed dashboard never wires the mode buttons).
    page.reload(wait_until="load")
    page.wait_for_selector(".kpi-card")
    held_twr = _hold(page, _TWR_API)
    page.click('#trend-mode .range-btn[data-mode="twr"]')
    expect(page.locator(_TWR)).to_be_visible()
    _assert_loading(page, _TWR, "/api/performance/twr")
    held_twr[0].fulfill(status=500, content_type="application/json", body='{"detail":"boom"}')
    page.wait_for_selector("#twr-chart .empty-state")
    assert "績效比較載入失敗" in page.inner_text(_TWR)
    _assert_settled(page, _TWR, "the TWR fetch failed")
    _assert_nothing_loading(page, "the TWR fetch failed")
    page.unroute(_TWR_API)

    real_console = [e for e in console_errors if "status of 500" not in e]  # the forced 500s
    assert not real_console and not page_errors, (
        f"console={real_console!r} page={page_errors!r}")


@pytest.mark.parametrize("width", (1440, 768, 390))
def test_the_loading_placeholder_never_scrolls_the_page_sideways(
    flow_server: FlowServerFactory, fresh_page: Page, width: int
) -> None:
    """The placeholder is measured WHILE it is up: an overlay that widened the document at
    any of the three owner widths would be a new page-level horizontal scroll."""
    base = flow_server(_seed_golden)
    page = fresh_page
    page.set_viewport_size({"width": width, "height": 900})

    held = _hold(page, _DASH)
    page.goto(base + "/index.html", wait_until="load")
    page.wait_for_function("() => document.readyState === 'complete'")
    _assert_loading(page, "#kpi-band", "/api/dashboard")
    m = page.evaluate(_SCROLL_JS)
    assert m["sw"] <= m["cw"], (
        f"index.html @ {width}px scrolls sideways WHILE loading: scrollWidth={m['sw']} > "
        f"clientWidth={m['cw']}")

    _release(page, _DASH, held)
    page.wait_for_selector(".kpi-card")
    m = page.evaluate(_SCROLL_JS)
    assert m["sw"] <= m["cw"], (
        f"index.html @ {width}px scrolls sideways after loading: scrollWidth={m['sw']} > "
        f"clientWidth={m['cw']}")
