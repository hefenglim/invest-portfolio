"""DEF-034 (2026-09-23): one click on 「代號查無時自動 AI 辨識」 sends ONE request, however many
times the page has reloaded its data.

The verifier's reproduction, verbatim: 系統設定 › AI 與額度 → save the 低額度警示 threshold
twice → click the switch → three ``PUT /api/ui-prefs`` and three toasts, because every save
re-ran ``boot()`` and ``boot()`` re-bound the switch. This drives exactly that sequence in a
real browser and counts the requests that reach the server.
"""

import pytest
from playwright.sync_api import Page, expect

from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory


@pytest.mark.e2e
def test_the_auto_ai_switch_sends_one_put_after_two_threshold_saves(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_golden)
    page = fresh_page
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(f"{base}/settings.html#llm", wait_until="networkidle")
    threshold = page.locator("#quota-threshold")
    for value in ("2", "3"):                       # two saves → two boot() reloads
        with page.expect_response(
            lambda r: r.request.method == "GET" and "/api/llm/config" in r.url
        ):
            threshold.fill(value)
            threshold.dispatch_event("change")
    prefs_puts: list[str] = []
    page.on("request", lambda r: prefs_puts.append(r.url)
            if r.method == "PUT" and "/api/ui-prefs" in r.url else None)
    switch = page.locator("#pref-auto-ai")
    was_on = "on" in (switch.get_attribute("class") or "")
    with page.expect_response(lambda r: r.request.method == "PUT" and "/api/ui-prefs" in r.url):
        switch.click()
    page.wait_for_timeout(800)                     # let any stacked duplicate land too
    assert len(prefs_puts) == 1, (
        f"one click sent {len(prefs_puts)} PUT /api/ui-prefs — listeners stacked on reload")
    expect(switch).to_have_class("toggle" if was_on else "toggle on")
    stored = page.request.get(f"{base}/api/ui-prefs").json()
    assert stored["auto_ai_resolve"] is (not was_on)
