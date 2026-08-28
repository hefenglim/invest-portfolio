"""The AI-failure-log panel renders and behaves on an empty log (AI-D72).

Why an e2e and not just the API contract test: on 2026-08-28 a one-character escaping slip
made `data-center.js` unparseable. A JS parse error kills the WHOLE file, so the panel —
and the unrelated db-stats table above it — rendered empty while every Python test stayed
green. Only a browser noticed. This pins the panel itself rather than the page in general,
so a future break says which feature died.

The empty state is the interesting one to assert. "Nothing has failed yet" and "measured
zero" are different statements, and the buttons must not offer to download or clear a log
with nothing in it.
"""

import pytest
from playwright.sync_api import Page

from tests.e2e.conftest import assert_page_ok


@pytest.mark.e2e
def test_fail_log_panel_renders_its_empty_state(
    live_server: str, browser_page: Page
) -> None:
    page = browser_page
    assert_page_ok(page, live_server, "/data-center.html", root_selector="#fl-body")
    page.wait_for_selector("#fl-cap", timeout=15000)
    page.wait_for_function(
        "() => document.querySelector('#fl-cap').textContent.trim().length > 0",
        timeout=15000,
    )

    # capacity is disclosed, not implied
    cap = page.inner_text("#fl-cap")
    assert "0" in cap and "300" in cap, cap

    # the empty state is a sentence, never a bare 0 that reads as a measurement
    assert "沒有失敗記錄" in page.inner_text("#fl-body")

    # nothing to download, nothing to clear
    assert page.is_disabled("#fl-download")
    assert page.is_disabled("#fl-clear")


@pytest.mark.e2e
def test_fail_log_panel_controls_exist_and_are_wired(
    live_server: str, browser_page: Page
) -> None:
    """The three controls must be present AND bound.

    A panel whose buttons are decorative passes a "does the page load" smoke. Refresh is
    the one control that is safe to press on an empty log, and pressing it must not
    produce a console error — which is what a dead handler or a bad fetch would emit.
    """
    page = browser_page
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))

    page.goto(f"{live_server}/data-center.html", wait_until="domcontentloaded")
    page.wait_for_selector("#fl-refresh", timeout=15000)
    for sel in ("#fl-refresh", "#fl-download", "#fl-clear"):
        assert page.query_selector(sel) is not None, sel

    page.click("#fl-refresh")
    page.wait_for_timeout(1500)
    assert not errors, errors
    assert "0" in page.inner_text("#fl-cap")
