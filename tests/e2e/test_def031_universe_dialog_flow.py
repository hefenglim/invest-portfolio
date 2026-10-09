"""DEF-031 / DEF-032 (2026-09-23): the insight-task universe, driven through the real pages.

DEF-031 — the drawer's 「編輯標的」 was a toast
(「✓ 編輯標的 沿用既有標的選擇器（持倉＋觀察清單）」) with no dialog and no
request, and the dry-run preflight's R2/R4 fix routed to that same button.
DEF-032 — the create wizard listed and counted HOLDING ROWS, so AAPL held at
嘉信 and Moomoo was two checkboxes and 「全部持倉」 over-counted.

`_seed_dual_account` is exactly that book: AAPL in two accounts plus 2330 — 3 holding rows,
2 symbols. Each flow asserts what the user sees AND what reached the server.
"""

import pytest
from playwright.sync_api import Page, expect

from tests.conftest import _seed_dual_account
from tests.e2e.conftest import FlowServerFactory


def _per_symbol_task(page: Page, base: str, universe: object = None) -> int:
    sp = page.request.post(f"{base}/api/strategy-prompts",
                           data={"name": "S", "body": "{{kpis_json}}"})
    assert sp.ok, sp.text()
    body: dict[str, object] = {"name": "個股健檢", "scope": "per_symbol",
                               "strategy_ids": [sp.json()["id"]], "enabled": True}
    if universe is not None:
        body["universe"] = universe
    it = page.request.post(f"{base}/api/insight-tasks", data=body)
    assert it.ok, it.text()
    return int(it.json()["id"])


def _stored_universe(page: Page, base: str, tid: int) -> object:
    rows = page.request.get(f"{base}/api/insight-tasks").json()
    return next(r for r in rows if r["id"] == tid)["universe"]


@pytest.mark.e2e
def test_the_wizard_lists_and_counts_symbols_not_holding_rows(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_dual_account)
    page = fresh_page
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(f"{base}/pipeline-hub.html", wait_until="networkidle")
    page.click("#pp-add")
    page.wait_for_selector(".wz-steps")
    page.click(".wz-foot .btn-primary")                      # ① 觸發 → ② 範圍
    page.click(".wz-opt:has-text('單一標的')")
    expect(page.locator(".wz-opt:has-text('全部持倉')")).to_contain_text("2 檔")
    page.click(".wz-opt:has-text('自選標的')")
    assert page.locator(".pp-uni-picker input[value='AAPL']").count() == 1, (
        "AAPL is held in two accounts — it must still be ONE checkbox")
    aapl = page.locator(".pp-uni-picker label:has(input[value='AAPL'])")
    expect(aapl).to_contain_text("嘉信")
    expect(aapl).to_contain_text("Moomoo MY")
    page.click(".pp-uni-bar button:has-text('全部持倉')")
    rail = page.locator(".wz-rail")
    expect(rail).to_contain_text("自選 2 檔")
    expect(rail).to_contain_text("~$0.02")


@pytest.mark.e2e
def test_edit_universe_opens_a_dialog_writes_the_task_and_refreshes_the_card(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_dual_account)
    page = fresh_page
    tid = _per_symbol_task(page, base)
    puts: list[str] = []
    page.on("request", lambda r: puts.append(r.url) if r.method == "PUT" else None)
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(f"{base}/pipeline-hub.html", wait_until="networkidle")
    card_input = page.locator(".pp-card .pp-node").nth(1)
    expect(card_input.locator(".pp-node-text")).to_have_text("2 檔標的")
    card_input.click()                                        # ② 輸入 → drawer
    page.click(".pp-drawer button:has-text('編輯標的')")
    expect(page.locator(".pv-title")).to_have_text("編輯標的 — 個股健檢")
    page.click(".wz-opt[data-mode='custom']")
    boxes = page.locator(".pp-uni-picker input[type=checkbox]")
    expect(boxes).to_have_count(2)                            # AAPL once + 2330
    page.uncheck(".pp-uni-picker input[value='2330']")
    expect(page.locator(".pp-uni-count")).to_contain_text("將分析 1 檔標的")
    with page.expect_response(lambda r: r.request.method == "PUT"
                              and f"/api/insight-tasks/{tid}" in r.url) as resp:
        page.click(".pv-box button:has-text('儲存標的')")
    assert resp.value.ok
    expect(page.locator(".pv-box")).to_have_count(0)
    expect(page.locator(".pp-card .pp-node").nth(1).locator(".pp-node-text")).to_have_text(
        "1 檔標的")
    expect(page.locator(".pp-drawer")).to_contain_text("1 檔標的")   # drawer re-opened, fresh
    assert _stored_universe(page, base, tid) == {"mode": "custom", "symbols": ["AAPL"]}
    assert len(puts) == 1, puts


@pytest.mark.e2e
def test_the_preflight_edit_universe_fix_opens_the_dialog(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_dual_account)
    page = fresh_page
    _per_symbol_task(page, base, universe={"mode": "custom", "symbols": []})   # R2: empty
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(f"{base}/pipeline-hub.html", wait_until="networkidle")
    page.click(".pp-card button:has-text('乾跑預檢')")
    fix = page.locator(".pf-row button:has-text('編輯標的')")
    expect(fix).to_have_count(1)
    fix.click()
    expect(page.locator(".pv-title")).to_have_text("編輯標的 — 個股健檢")
    expect(page.locator(".wz-opt[data-mode='custom']")).to_have_class("wz-opt sel")
