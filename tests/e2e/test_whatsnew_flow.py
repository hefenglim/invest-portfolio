"""E2E: the ✦ 新功能 badge + panel (WP-WN) against the real server + real frontend.

Runs against its OWN isolated uvicorn subprocess (guest mode, fresh whatsnew_config ->
seen="0"), so the acknowledge write does not pollute other tests. Mirrors the E1-E10
flow style (tests/e2e/test_flows_e1_e10.py): expect-polling, never sleeps.
"""

from collections.abc import Iterator

import pytest
from playwright.sync_api import Page
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    """Re-enable loopback sockets per test (pytest-socket re-bans before each test)."""
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


@pytest.mark.e2e
def test_whatsnew_badge_panel_and_persist(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """Fresh DB -> dot shows; opening ✦ renders groups + acknowledges; reload stays clean."""
    base = flow_server(_seed_golden)
    page = fresh_page
    page_errors: list[str] = []
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    # Fresh install (seen="0"): the ✦ button shows the unseen dot after whatsnew init.
    page.goto(base + "/index.html", wait_until="load")
    page.wait_for_selector("#wn-btn")
    page.wait_for_selector("#wn-btn .wn-dot")

    # Open the panel: version groups + at least one 前往 button render.
    page.click("#wn-btn")
    page.wait_for_selector(".wn-backdrop .wn-group")
    assert page.locator(".wn-backdrop .wn-group").count() > 0
    assert page.locator(".wn-backdrop .wn-go").count() > 0
    # Opening acknowledges: the dot is cleared optimistically (and persisted server-side).
    page.wait_for_selector("#wn-btn .wn-dot", state="detached")

    # Esc dismisses the panel; the dot stays gone.
    page.keyboard.press("Escape")
    page.wait_for_selector(".wn-backdrop", state="detached")
    assert page.locator("#wn-btn .wn-dot").count() == 0

    # Reload: the acknowledgement persisted, so the dot does not come back.
    page.goto(base + "/index.html", wait_until="load")
    page.wait_for_selector("#wn-btn")
    page.wait_for_load_state("networkidle")
    assert page.locator("#wn-btn .wn-dot").count() == 0

    assert not page_errors, page_errors
