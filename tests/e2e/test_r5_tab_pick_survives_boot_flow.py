"""E2E (R5, DEF-065 gate stability): a tab clicked while 交易 is still loading stays open.

Found as a flaky gate, not by a verifier: in the second of the three full e2e runs DEF-065
asked for, ``test_def049_batch_undo_oversell_flow`` clicked 「CSV 匯入」 and then found the
最近匯入 row hidden. ``web/input.js``'s ``boot()`` awaits ``/api/input/context`` and only then
calls ``showTab('manual')`` — so any tab picked before the context arrived was taken back the
moment it did, whenever the server answered a little slower than the click. The same happens
to a person on a slow connection: 「CSV 匯入」 opens and closes again under the pointer.

The test holds ``/api/input/context`` open, clicks the tab, then releases the response and
asserts the pane the owner chose is still the one showing.
"""

from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, Route, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


@pytest.mark.e2e
def test_a_tab_picked_before_the_context_loads_is_not_taken_back(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_golden)
    page = fresh_page
    held: list[Route] = []
    released = [False]

    def _hold(route: Route) -> None:
        if released[0]:
            route.continue_()
        else:
            held.append(route)

    page.route("**/api/input/context", _hold)
    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_function("() => !!window.pdAckConfirm")   # input.js ran (it loads before)
    page.wait_for_timeout(200)
    assert held, "the page never asked for /api/input/context"
    page.click("#tab-csv")
    expect(page.locator("#pane-csv")).to_have_class("mode-pane active")
    # Now the context arrives — boot() finishes and must NOT reset the tab.
    released[0] = True
    with page.expect_response("**/api/input/context"):
        for route in held:
            route.continue_()
    page.wait_for_timeout(300)  # boot()'s synchronous tail (init* + default tab) has run
    expect(page.locator("#pane-csv")).to_have_class("mode-pane active")
    expect(page.locator("#pane-manual")).not_to_have_class("mode-pane active")
    expect(page.locator("#tab-csv")).to_have_class("active")


@pytest.mark.e2e
def test_with_no_pick_the_page_still_opens_on_manual_entry(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_golden)
    page = fresh_page
    with page.expect_response("**/api/input/context"):
        page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_timeout(300)
    expect(page.locator("#pane-manual")).to_have_class("mode-pane active")
