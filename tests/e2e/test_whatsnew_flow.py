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


@pytest.mark.e2e
def test_whatsnew_callout_arrival_and_cancel_on_switch(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """前往 -> in-page callout + 10s flash on the target; a tab switch cancels both and
    they do not resurface on switch-back (only a fresh 前往 re-arms)."""
    base = flow_server(_seed_golden)
    page = fresh_page
    page_errors: list[str] = []
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    # Open the panel and click the first 前往 (market-risk-alerts -> settings.html#alerts).
    page.goto(base + "/index.html", wait_until="load")
    page.wait_for_selector("#wn-btn")
    page.click("#wn-btn")
    page.wait_for_selector(".wn-backdrop .wn-go")
    page.locator(".wn-backdrop .wn-go").first.click()

    # Arrives on the alerts tab: the callout is visible with the feature title, and the
    # flash wraps the rules block (the panel enclosing #alert-rules-wrap).
    page.wait_for_url("**/settings.html#alerts")
    callout = page.locator(".wn-callout")
    callout.wait_for(state="visible")
    assert callout.locator(".wn-callout-title").inner_text() == "市場風險預警"
    page.wait_for_selector(".wn-flash #alert-rules-wrap")

    # Switch tab (hashchange): callout + flash vanish immediately.
    page.evaluate("window.location.hash = 'accounts'")
    page.wait_for_selector(".wn-callout", state="detached")
    page.wait_for_selector(".wn-flash", state="detached")

    # Switch back to #alerts: they do NOT resurface.
    page.evaluate("window.location.hash = 'alerts'")
    page.wait_for_load_state("networkidle")
    assert page.locator(".wn-callout").count() == 0
    assert page.locator(".wn-flash").count() == 0

    assert not page_errors, page_errors
