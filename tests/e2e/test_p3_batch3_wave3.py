"""E2E for P3 batch 3 · Wave 3 (3A bell read-state / 3C news trigger / 3D toolbar / 3E inbox).

Same harness as tests/e2e/test_pages_smoke.py: a REAL uvicorn subprocess over a seeded
golden DB + a headless chromium page. Write flows (skip/unskip/confirm/undo) use an ISOLATED
flow_server + fresh_page (clean localStorage) so they are order-independent.
"""

import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest
from playwright.sync_api import Page
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.pricing.results import DividendEvent
from portfolio_dash.pricing.store import upsert_dividend_events
from portfolio_dash.shared.enums import Currency, Market
from tests.conftest import GOLDEN_NOW, _seed_golden


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    """Re-enable loopback sockets PER TEST (mirrors tests/e2e/test_flows_e1_e10.py).

    pytest-socket's --disable-socket re-bans sockets before every test; the session-scoped
    _e2e_loopback_socket lifts the ban only once. The flow_server free-port probe opens a
    fresh Python socket per test, so each flow test must re-lift the ban itself."""
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)

# Pages whose .panel-head / .toolbar rows must render one consistent button height (3D).
_TOOLBAR_PAGES = [
    "/index.html", "/trades.html", "/dividend-inbox.html", "/news.html",
    "/instruments.html", "/insights.html", "/cash.html",
]

_ROW_HEIGHT_JS = """() => {
  const bad = [];
  document.querySelectorAll('.panel-head, .toolbar').forEach((row) => {
    const btns = Array.from(row.querySelectorAll('.btn, .btn-export'))
      .filter((b) => b.offsetParent !== null);  // visible only
    if (btns.length < 2) return;
    const hs = btns.map((b) => Math.round(b.getBoundingClientRect().height * 100) / 100);
    const min = Math.min(...hs), max = Math.max(...hs);
    if (max - min > 1) bad.push({ cls: row.className, heights: hs });
  });
  return bad;
}"""


@pytest.mark.e2e
def test_toolbar_row_button_heights_consistent(
    live_server: str, browser_page: Page
) -> None:
    """3D: within every .panel-head / .toolbar row, all visible action buttons render at the
    SAME height (within 1px). Iterates the key pages; networkidle lets async-appended
    export/action buttons mount before the measurement."""
    page = browser_page
    for path in _TOOLBAR_PAGES:
        page.goto(live_server + path, wait_until="load")
        page.wait_for_load_state("networkidle")
        bad = page.evaluate(_ROW_HEIGHT_JS)
        assert not bad, f"{path}: uneven button heights in a toolbar row: {bad!r}"


@pytest.mark.e2e
def test_bell_dot_clears_on_open_and_relights_after_reset(
    flow_server: object, fresh_page: Page
) -> None:
    """3A: the bell dot lights for UNSEEN alert ids, clears when the panel is opened (ids
    marked seen in localStorage), stays clear across a reload, and re-lights once the
    seen-set is cleared. Golden seeds 2330 at ~94% weight -> a single_weight alert."""
    make = flow_server  # type: ignore[assignment]
    base = make(_seed_golden)  # type: ignore[operator]
    page = fresh_page

    # Non-dashboard page -> alerts.js fetches /api/alerts and renders the bell.
    page.goto(base + "/instruments.html", wait_until="load")
    page.wait_for_selector(".bell-count")  # unseen alert -> dot lit

    # Open the panel -> current ids marked seen -> the dot clears.
    page.click(".bell-btn")
    page.wait_for_selector(".bell-count", state="detached")

    # Reload: the same alert ids are already seen (localStorage) -> dot stays clear.
    with page.expect_response("**/api/alerts"):
        page.goto(base + "/instruments.html", wait_until="load")
    page.wait_for_timeout(300)  # let renderCount settle after the fetch
    assert page.query_selector(".bell-count") is None, "dot must stay clear once all ids seen"

    # Clear the seen-set -> the alert is unseen again -> the dot re-lights.
    page.evaluate("() => localStorage.removeItem('pd_alerts_seen')")
    page.goto(base + "/instruments.html", wait_until="load")
    page.wait_for_selector(".bell-count")  # relit


@pytest.mark.e2e
def test_news_manual_fetch_button_smoke(
    live_server: str, browser_page: Page
) -> None:
    """3C: the 抓取新聞 button posts /api/news/run. The golden server is GUEST -> 403, which
    the page surfaces as a fail toast (no unhandled rejection). Asserts the toolbar rendered,
    the button click produced a fail toast, and NO page errors / no non-403 console errors."""
    page = browser_page
    console_errors: list[str] = []
    page_errors: list[str] = []

    def _on_console(msg: object) -> None:
        if getattr(msg, "type", None) == "error":
            console_errors.append(getattr(msg, "text", repr(msg)))

    def _on_pageerror(exc: object) -> None:
        page_errors.append(str(exc))

    page.on("console", _on_console)
    page.on("pageerror", _on_pageerror)
    try:
        page.goto(live_server + "/news.html", wait_until="load")
        page.wait_for_selector("#nw-toolbar #nw-run")
        with page.expect_response("**/api/news/run") as resp_info:
            page.click("#nw-run")
        assert resp_info.value.status == 403  # guest lockdown
        page.wait_for_selector(".toast-fail")
    finally:
        page.remove_listener("console", _on_console)
        page.remove_listener("pageerror", _on_pageerror)
    # A non-2xx fetch logs a browser "Failed to load resource" console error — that is the
    # DELIBERATE 403, not an app defect. Everything else stays fatal.
    app_errors = [e for e in console_errors if "Failed to load resource" not in e]
    assert not app_errors and not page_errors, (
        f"news manual fetch: console errors={app_errors!r}; page errors={page_errors!r}"
    )


def _seed_with_event(conn: sqlite3.Connection) -> None:
    _seed_golden(conn)
    upsert_dividend_events(conn, [DividendEvent(
        instrument="2330", market=Market.TW, ex_date=date(2026, 5, 20),
        cash_amount=Decimal("2.75"), currency=Currency.TWD, source="finmind")],
        fetched_at=GOLDEN_NOW)


@pytest.mark.e2e
def test_inbox_skip_then_unskip_resurfaces(
    flow_server: object, fresh_page: Page
) -> None:
    """3E: skip an inbox item -> it moves to 已忽略 -> 取消忽略 -> it re-surfaces."""
    make = flow_server  # type: ignore[assignment]
    base = make(_seed_with_event)  # type: ignore[operator]
    page = fresh_page

    page.goto(base + "/dividend-inbox.html", wait_until="load")
    page.wait_for_selector("#inbox-list .inbox-item")  # 2330 event detected

    # Skip the item -> leaves the list, appears under the (collapsed) 已忽略 list.
    page.click("#inbox-list .inbox-item .inbox-actions button:has-text('略過')")
    page.wait_for_selector("#inbox-list .inbox-item", state="detached")
    page.wait_for_selector("#inbox-skipped:not([hidden])")
    page.wait_for_selector("#inbox-skipped-list .sk-row", state="attached")

    # Expand the collapsible, then un-skip -> the item re-surfaces in the inbox list.
    page.click("#inbox-skipped > summary")
    page.click("#inbox-skipped-list .sk-row button:has-text('取消忽略')")
    page.wait_for_selector("#inbox-list .inbox-item")


@pytest.mark.e2e
def test_inbox_confirm_then_undo_resurfaces(
    flow_server: object, fresh_page: Page
) -> None:
    """3E: confirm an inbox item -> the 已入帳 strip shows it -> 復原 (delete the ledger row,
    ack the confirm dialog) -> the item re-surfaces in the inbox."""
    make = flow_server  # type: ignore[assignment]
    base = make(_seed_with_event)  # type: ignore[operator]
    page = fresh_page

    page.goto(base + "/dividend-inbox.html", wait_until="load")
    page.wait_for_selector("#inbox-list .inbox-item")

    # Confirm the single item (direct action, no dialog) -> the undo strip appears.
    page.click("#inbox-list .inbox-item .inbox-actions button:has-text('確認入帳')")
    page.wait_for_selector("#inbox-confirmed-strip.show")
    page.wait_for_selector("#inbox-confirmed-strip .icf-row")

    # 復原 -> confirm dialog -> delete the dividend ledger row -> item re-surfaces.
    page.click("#inbox-confirmed-strip .icf-row button:has-text('復原')")
    page.wait_for_selector(".modal")
    page.click(".modal-foot .btn-danger")
    page.wait_for_selector("#inbox-list .inbox-item")  # re-detected + re-surfaced
