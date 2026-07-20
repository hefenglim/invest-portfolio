"""E2E (Playwright, real server + real frontend): the R7 A4 detail-drawer 試算 rewire.

Drives the REAL stack (uvicorn subprocess + SQLite + served web/) opening the symbol-detail
drawer on the dashboard and exercising its 試算 (what-if) section, which now POSTs the real
``/api/whatif`` (a pure server compute — no LLM/provider stubbing needed) instead of computing
money in the browser.

Asserts:
  * HAPPY PATH — typing 股數 + 價格 renders the OLD → NEW pairs (持股 / 原始均價 / 調整均價 /
    權重) and the transaction figures straight from the REAL backend reply; ZERO console/page
    errors (every request is a 200).
  * ERROR PATH — with ``/api/whatif`` routed to 500 via ``page.route``, the section shows
    「試算暫不可用」 and NEVER falls back to local math; ZERO uncaught page errors and no console
    error OTHER than the intentional network-500 the browser logs for the blocked request.
"""

import json
from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, Route, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    """Re-enable loopback sockets PER TEST (pytest-socket re-bans before every test); each
    flow spawns a fresh isolated uvicorn (free-port probe + readiness poll need loopback)."""
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _collect_errors(page: Page) -> tuple[list[str], list[str]]:
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(getattr(m, "text", ""))
            if getattr(m, "type", None) == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    return console_errors, page_errors


def _open_drawer(page: Page, base_url: str) -> None:
    """Dashboard → open the 2330 (held golden symbol) detail drawer → wait for 試算 mount."""
    page.goto(base_url + "/index.html", wait_until="load")
    page.wait_for_selector(".kpi-card")  # dashboard async render landed
    page.evaluate("() => window.pdOpenSymbol('2330')")
    page.wait_for_selector(".sd-drawer .sd-sim")


def _is_expected_network_500(text: str) -> bool:
    """A 500 fulfilled via page.route makes the browser log a resource-load failure to the
    console — that is EXPECTED on the error-path test; a real defect surfaces as a pageerror
    or a DIFFERENT console error (undefined field / .toFixed TypeError)."""
    return ("Failed to load resource" in text) or ("500" in text) or ("whatif" in text)


@pytest.mark.e2e
def test_whatif_drawer_renders_old_and_new_from_backend(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """R7 A4: type 股數+價格 in the drawer 試算 → the REAL /api/whatif drives OLD → NEW pairs.

    Golden 2330 is held 1,000 sh in tw_broker (original avg 500, dividend-adjusted avg 495).
    A BUY of 100 @ 600 → 持股 1,000 → 1,100; four OLD → NEW pairs render + transaction figures.
    """
    base = flow_server(_seed_golden)
    page = fresh_page
    console_errors, page_errors = _collect_errors(page)

    _open_drawer(page, base)
    drawer = page.locator(".sd-drawer")

    # Switch to 加碼試算 (buy) — clears 股數 — then type qty + price; the debounced POST fires.
    drawer.get_by_role("button", name="加碼試算").click()
    with page.expect_response("**/api/whatif") as resp_info:
        drawer.locator("#sim-shares").fill("100")
        drawer.locator("#sim-price").fill("600")
    assert resp_info.value.status == 200, f"/api/whatif status {resp_info.value.status}"

    result = drawer.locator(".sd-sim-result")
    # Four OLD → NEW pairs (持股 / 原始均價 / 調整均價 / 權重), rendered from the backend reply.
    expect(result.locator(".sd-sim-pair")).to_have_count(4)
    hold_pair = result.locator(".sd-sim-pair").filter(has_text="持股")
    expect(hold_pair.locator(".sd-old")).to_have_text("1,000")   # held shares (backend)
    expect(hold_pair.locator(".sd-new")).to_have_text("1,100")   # after a +100 buy
    # the OLD original avg is the backend's real basis (500), not a fabricated local number.
    orig_pair = result.locator(".sd-sim-pair").filter(has_text="原始均價")
    expect(orig_pair.locator(".sd-old")).to_have_text("500.00")
    # transaction figures also render (成交金額 = 100 × 600 = 60,000).
    expect(result.get_by_text("成交金額")).to_be_visible()
    expect(result.get_by_text("總成本（含費稅）")).to_be_visible()

    assert not console_errors and not page_errors, (
        f"whatif drawer happy path: console={console_errors!r} page={page_errors!r}"
    )


@pytest.mark.e2e
def test_whatif_drawer_shows_unavailable_on_500(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """R7 A4: a 500 from /api/whatif → 「試算暫不可用」, never a fabricated local estimate.

    The section auto-fires a 試算 on open; with the endpoint routed to 500 the .then never
    renders and the .catch sets the honest 「試算暫不可用」 note. No uncaught page error (the
    PdApiError is handled) and no console error beyond the intentional network 500.
    """
    base = flow_server(_seed_golden)
    page = fresh_page
    console_errors, page_errors = _collect_errors(page)

    def _fail(route: Route) -> None:
        route.fulfill(status=500, content_type="application/json",
                      body=json.dumps({"error": {"code": "error", "message": "boom"}}))

    page.route("**/api/whatif", _fail)

    _open_drawer(page, base)
    drawer = page.locator(".sd-drawer")
    # Nudge an explicit run too (the auto-fire may already be in flight), then assert the note.
    drawer.locator("#sim-price").fill("601")
    expect(drawer.locator(".sd-sim-note")).to_have_text("試算暫不可用")
    # The result area holds NO fabricated rows (no local fallback math).
    expect(drawer.locator(".sd-sim-result .sd-sim-pair")).to_have_count(0)

    unexpected = [t for t in console_errors if not _is_expected_network_500(t)]
    assert not unexpected and not page_errors, (
        f"whatif drawer error path: unexpected console={unexpected!r} page={page_errors!r}"
    )
