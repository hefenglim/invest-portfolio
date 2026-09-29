"""E2E DEF-085: the dry run's red 「AI 額度」 row says where to fix it, and the button goes there.

H-05 (R10): 「✕ AI 額度 額度耗盡（剩餘 $-0.01000）」 with no button while G1/R2/R3/R4 had one,
and a verdict telling the owner to 「修復上方紅色項目」. The seeded book has no top-up, so the
quota is $0 and R6 fails for any task — the state H-05 reaches with −$0.01.
"""

import pytest
from playwright.sync_api import Page, expect

from tests.conftest import _seed_dual_account
from tests.e2e.conftest import FlowServerFactory


@pytest.mark.e2e
def test_the_quota_row_offers_the_quota_page_in_both_dialogs(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_dual_account)
    page = fresh_page
    sp = page.request.post(f"{base}/api/strategy-prompts",
                           data={"name": "S", "body": "{{kpis_json}}"})
    assert sp.ok, sp.text()
    it = page.request.post(f"{base}/api/insight-tasks", data={
        "name": "組合健檢", "scope": "portfolio", "strategy_ids": [sp.json()["id"]],
        "enabled": True})
    assert it.ok, it.text()
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(f"{base}/pipeline-hub.html", wait_until="networkidle")

    for opener in ("乾跑預檢", "為什麼沒跑？"):
        page.click(f".pp-card button:has-text('{opener}')")
        row = page.locator(".pf-row", has=page.locator(".pf-ref", has_text="R6"))
        expect(row).to_contain_text("額度耗盡（剩餘 $0.00）")
        expect(row.locator("button")).to_have_text("前往額度設定")
        if opener == "為什麼沒跑？":
            row.locator("button").click()
            page.wait_for_url("**/settings.html#llm")
            break
        page.keyboard.press("Escape")
        page.locator(".pv-backdrop").evaluate_all("ns => ns.forEach(n => n.remove())")
