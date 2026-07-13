"""E2E: the ✦ 新功能 badge + panel (per-feature seen) + 版本發佈資訊 history browser (WP-WN).

Runs against its OWN isolated uvicorn subprocess (guest mode, fresh whatsnew state), so
the seen writes do not pollute other tests. Mirrors the E1-E10 flow style
(tests/e2e/test_flows_e1_e10.py): expect-polling, never sleeps.
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
def test_whatsnew_per_feature_seen_and_persist(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """Fresh DB -> dot shows. Opening does NOT ack. 前往 clears only that feature's NEW
    (dot persists while others are unread). 全部標示已讀 clears the dot; a reload keeps it."""
    base = flow_server(_seed_golden)
    page = fresh_page
    page_errors: list[str] = []
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    # Fresh install: the ✦ button shows the unseen dot after whatsnew init.
    page.goto(base + "/index.html", wait_until="load")
    page.wait_for_selector("#wn-btn")
    page.wait_for_selector("#wn-btn .wn-dot")

    # Open the panel: version groups + at least one 前往 render. Opening does NOT ack, so
    # the dot is STILL present (round-3 change from the old open-acks-everything behaviour).
    page.click("#wn-btn")
    page.wait_for_selector(".wn-backdrop .wn-group")
    assert page.locator(".wn-backdrop .wn-go").count() > 0
    assert page.locator(".wn-backdrop .wn-new-pill").count() > 0
    assert page.locator("#wn-btn .wn-dot").count() == 1

    # Click the first 前往 (market-risk-alerts -> settings.html#alerts): marks THAT feature.
    first_key = page.locator(".wn-backdrop .wn-feat").first.get_attribute("data-wn-key")
    assert first_key
    page.locator(".wn-backdrop .wn-go").first.click()
    page.wait_for_url("**/settings.html#alerts")

    # The dot persists (other features are still unread). Reopen the panel: the clicked
    # feature's row has NO NEW pill, while other rows still do.
    page.wait_for_selector("#wn-btn .wn-dot")
    page.click("#wn-btn")
    page.wait_for_selector(".wn-backdrop .wn-group")
    seen_row = page.locator('.wn-backdrop .wn-feat[data-wn-key="' + first_key + '"]')
    seen_row.wait_for(state="visible")
    assert seen_row.locator(".wn-new-pill").count() == 0
    assert page.locator(".wn-backdrop .wn-new-pill").count() > 0  # others still unread

    # 全部標示已讀 clears every pill and the ambient dot.
    page.click(".wn-backdrop .wn-foot button")
    page.wait_for_selector("#wn-btn .wn-dot", state="detached")
    assert page.locator(".wn-backdrop .wn-new-pill").count() == 0

    # Reload: the acknowledgement persisted, so the dot does not come back.
    page.goto(base + "/settings.html", wait_until="load")
    page.wait_for_selector("#wn-btn")
    page.wait_for_load_state("networkidle")
    assert page.locator("#wn-btn .wn-dot").count() == 0

    assert not page_errors, page_errors


@pytest.mark.e2e
def test_whatsnew_callout_arrival_and_cancel_on_switch(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """前往 -> in-page callout + blink on the target; a tab switch cancels both and they do
    not resurface on switch-back (only a fresh 前往 re-arms)."""
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
    # blink wraps the rules block (the panel enclosing #alert-rules-wrap).
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


@pytest.mark.e2e
def test_whatsnew_history_browser_opens_from_settings(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """settings 一般 -> 版本發佈資訊 button -> history modal renders the first page of groups."""
    base = flow_server(_seed_golden)
    page = fresh_page
    page_errors: list[str] = []
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    page.goto(base + "/settings.html", wait_until="load")
    page.wait_for_selector("#gen-whatsnew")
    page.click("#gen-whatsnew")
    page.wait_for_selector(".wnh-backdrop .wnh-group")
    assert page.locator(".wnh-backdrop .wnh-group").count() > 0
    # a "載入更早版本" pager button exists (the catalog has more than one page of versions).
    assert page.locator(".wnh-backdrop .wnh-foot button").count() == 1

    assert not page_errors, page_errors
