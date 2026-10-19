"""PERF spec 2026-10-18 §14.1–§14.2 (plan §6.3, package F): the chip and panel states the REAL
backend cannot produce cheaply — 進行緩慢, 結算中斷, 結算失敗 with its consequence, a self-check
difference, 25 runs of history — plus PD_SETTLE=off on the real backend.

The real flows (pending → running → clean, the prompt, the menu's full run, the live panel,
a finished report, cancel, a reload) are ``test_settlement_real_flow.py``. Here the settlement
API is stubbed on ``fresh_page`` only (removed at the end) with bodies in E's REAL shapes
(``tests/e2e/settlement_stubs.py``, pinned to the routes by
``tests/contract/test_settlement_stub_shapes.py``). The page is the session ``live_server``
(golden ledger, ``PD_SETTLE=off`` — so without a stub the real status says ``off``).
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.settlement_stubs import (
    SettleStub,
    change,
    changes_page,
    counter,
    progress,
    report,
    status,
    stubbed,
    summary,
    table,
    trigger,
)


def _open(page: Page, base: str, path: str = "/instruments.html") -> None:
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(base + path, wait_until="load")


def _chip(page: Page) -> Any:
    return page.locator("#settle-chip")


def _refresh(page: Page) -> None:
    page.evaluate("() => window.pdSettlement.refresh()")


@pytest.mark.e2e
def test_off_on_the_real_backend_shows_nothing_and_the_menu_keeps_the_replay(
    live_server: str, fresh_page: Page
) -> None:
    """No stub: E's real status under PD_SETTLE=off. No chip, no prompt, no banner — and the
    menu's 重新結算 is today's strict replay (POST /api/actions/recompute, then a reload)."""
    page = fresh_page
    recompute: list[str] = []
    page.on("request", lambda r: recompute.append(r.method)
            if "/api/actions/recompute" in r.url else None)
    _open(page, live_server, "/index.html")
    page.wait_for_function("() => window.pdSettlement && window.pdSettlement.status()")
    assert page.evaluate("() => window.pdSettlement.status().state") == "off"
    assert _chip(page).count() == 0 or not _chip(page).is_visible()
    assert page.locator(".modal-backdrop").count() == 0
    assert page.locator("[data-settle-banner]:not([hidden])").count() == 0
    page.locator(".refresh-wrap .btn-refresh").click()
    with page.expect_navigation(wait_until="load"):
        page.get_by_role("button", name=re.compile("重新結算（驗證並重建歷史）")).click()
    assert recompute == ["POST"]


@pytest.mark.e2e
def test_every_chip_state_is_a_class_and_its_text_comes_from_the_status(
    live_server: str, fresh_page: Page
) -> None:
    stub = SettleStub(status("clean"))
    with stubbed(fresh_page, stub):
        _open(fresh_page, live_server)
        chip = _chip(fresh_page)
        expect(chip).to_be_visible()
        # the chip sits right after the topbar's spacer, on a page with no data-chips
        assert fresh_page.evaluate(
            "() => document.querySelector('.topbar .header-spacer').nextElementSibling.id"
        ) == "settle-chip"
        cases = [
            (status("clean"), "st-chip-clean", "已結算"),
            (status("pending", dirty_from="2025-06-16", stale_from="2025-06-16"),
             "st-chip-pending", "待結算 · 2025-06-16 起"),
            (status("preparing", run=progress(412, status="preparing", percent=0)),
             "st-chip-run", "準備中"),
            (status("running", run=progress(412)), "st-chip-pulse", "結算中 58%"),
            (status("running", run=progress(412), slow=True), "st-chip-slow",
             "結算中 58% · 進行緩慢"),
            (status("interrupted", last_run=summary(411, status="interrupted",
                                                    status_text="結算中斷（未續跑）")),
             "st-chip-warn", "結算中斷 · 未續跑"),
            (status("failed", last_run=summary(411, status="failed", status_text="失敗")),
             "st-chip-fail", "結算失敗"),
            (status("selfcheck_diff", last_run=summary(411, selfcheck_diff=True)),
             "st-chip-fail", "自檢發現差異"),
        ]
        for st, cls, text in cases:
            stub.status = st
            _refresh(fresh_page)
            expect(chip).to_have_class(re.compile(r"\b" + cls + r"\b"))
            expect(chip).to_contain_text(text)
            assert chip.get_attribute("style") is None, "a state colour is a class (DEF-077)"
        # a status poll is background: the top progress bar never moved for it
        assert fresh_page.evaluate("() => window.pdApi.pending()") == 0


@pytest.mark.e2e
def test_a_slow_live_run_shows_every_section_in_the_servers_words(
    live_server: str, fresh_page: Page
) -> None:
    """進行緩慢 (the real backend would need 60 s without progress), the status's `queued`, the
    claimed trail in E's words — and #412's own report never says 「曾於 #412 嘗試」."""
    queued = {"tables": [table("transactions", 1, "2026-05-03", "2026-05-03")],
              "records_total": 1}
    stub = SettleStub(status("running", run=progress(412), slow=True, queued=queued))
    stub.changes["run:412"] = changes_page(
        [change(1, run_id=412),
         change(2, run_id=412, op="insert", old_day=None, new_day="2025-06-17",
                actor="job:quotes_tw", import_batch_id="b-7", attempted_run_ids=[411, 412])],
        counters=[counter(1, key="0050", earliest="2025-07-01", latest="2025-07-21",
                          changes=14, attempted_run_ids=[411, 412], state="claimed",
                          claimed_run_id=412)],
        tables=[table("transactions", 2, "2025-06-16", "2025-06-18"),
                table("prices", 14, "2025-07-01", "2025-07-21", keys=3)], run_id=412)
    with stubbed(fresh_page, stub):
        _open(fresh_page, live_server)
        _chip(fresh_page).click()
        body = fresh_page.locator(".st-drawer .st-body")
        expect(body).to_contain_text("重新結算：2025-06-16 至 2026-06-10，共 360 天")
        expect(body).to_contain_text(
            trigger("prompt", "2026-06-11T10:32:00+08:00", cause="交易變更"))
        expect(body).to_contain_text("已用 12 秒 · 預估還要 8.0 秒 · 進行緩慢")
        expect(body).to_contain_text("背景低優先序")
        expect(body).to_contain_text("交易 2 筆，2025-06-16 至 2025-06-18")
        expect(body).to_contain_text("價格 3 檔 14 筆，2025-07-01 至 2025-07-21")
        trail = fresh_page.locator(".st-drawer .st-table").first
        expect(trail).to_contain_text("2025-06-18 → 2025-06-16")
        expect(trail).to_contain_text("你")            # E's actor_text for the guest viewer
        expect(trail).to_contain_text("排程")
        expect(trail).to_contain_text("曾於 #411 嘗試")
        expect(trail).not_to_contain_text("#412")
        expect(trail.locator("tr.st-row-lead")).to_contain_text("0050")
        expect(trail.locator("tr.st-row-lead")).to_contain_text("14 筆")
        expect(body).to_contain_text("281 / 489 天 · 35 天/秒")
        expect(fresh_page.locator(".st-step-done .st-step-mark").first).to_have_text("✓")
        expect(fresh_page.locator(".st-step-running .st-step-mark")).to_have_text("▸")
        expect(body).to_contain_text("正在估值：2026-03-24（41 個部位）")
        expect(body).to_contain_text("完成後接著處理：交易 1 筆，2026-05-03")
        expect(body).to_contain_text("2330 於 2025-08-01 賣超")
        expect(body).not_to_contain_text("{account:")
        expect(body).to_contain_text("第 41 版 → 第 42 版（切換後生效）")
        expect(fresh_page.locator(".st-bar-fill")).to_have_attribute("style", "width: 58%;")
        before = stub.count("/settlement/status")
        fresh_page.wait_for_timeout(2600)
        assert stub.count("/settlement/status") - before >= 2


@pytest.mark.e2e
def test_the_idle_panel_shows_state_reports_and_steps_through_the_history(
    live_server: str, fresh_page: Page
) -> None:
    last = summary(430, had_changes=False)
    last_change = summary(425, had_changes=True, reason="prompt",
                          started_by="user:guest",
                          trigger_text=trigger("prompt", "2026-06-09T09:12:00+08:00",
                                               cause="交易變更"))
    stub = SettleStub(status("clean", last_run=last, last_change_run=last_change))
    stub.runs = [summary(430 - i, had_changes=(i % 5 == 0)) for i in range(25)]
    for r in stub.runs:
        stub.reports[r["run_id"]] = report(r["run_id"], summary=r)
    stub.reports[425] = report(425, summary=last_change, changes={
        "tables": [table("transactions", 1, "2025-06-16", "2025-06-16")], "records_total": 1})
    stub.changes["run:425"] = changes_page([change(1, run_id=425, state="settled",
                                                   settled_run_id=425)], run_id=425)
    with stubbed(fresh_page, stub):
        _open(fresh_page, live_server)
        _chip(fresh_page).click()
        body = fresh_page.locator(".st-drawer .st-body")
        expect(body).to_contain_text("已結算至 2026-06-10 · 第 41 版")
        expect(body).to_contain_text("最近一次結算 #430")
        expect(body).to_contain_text("最近一次結算變更 #425")
        expect(body).to_contain_text("你 於 09:12 啟動（確認提示：交易變更）")
        expect(body).to_contain_text("交易 1 筆，2025-06-16")
        expect(body).to_contain_text("未發現待釐清項目")
        expect(body).to_contain_text("月結與年結無變動")
        expect(fresh_page.get_by_role("button", name="重新結算（驗證並重建歷史）")).to_be_visible()
        hist = fresh_page.locator(".st-sec[data-sec='history'] tbody tr")
        expect(hist).to_have_count(20)
        expect(fresh_page.locator(".st-sec[data-sec='history'] .st-pager-info")).to_have_text(
            "1–20／共 25 筆")
        hist.nth(19).click()
        expect(fresh_page.locator(".st-card-title")).to_have_text("結算 #411")
        fresh_page.get_by_role("button", name="下一筆").click()
        expect(fresh_page.locator(".st-card-title")).to_have_text("結算 #410")
        assert any("offset=20" in u for u in stub.gets if "/settlement/runs?" in u)
        fresh_page.get_by_role("button", name="上一筆").click()
        expect(fresh_page.locator(".st-card-title")).to_have_text("結算 #411")
        fresh_page.get_by_role("button", name="← 回到目前狀態").click()
        fresh_page.locator(".st-filter input").check()
        expect(hist).to_have_count(5)
        fresh_page.locator(".st-sec[data-sec='history'] > summary").click()
        fresh_page.wait_for_function(   # <details> fires `toggle` asynchronously
            "() => (JSON.parse(localStorage.getItem('pd_settle_sections') || '{}')).history"
            " === false")


@pytest.mark.e2e
def test_a_failed_run_shows_its_consequence_keeps_it_on_reload_and_retries(
    live_server: str, fresh_page: Page
) -> None:
    last = summary(430, status="failed", status_text="失敗", had_changes=True)
    stub = SettleStub(status("failed", last_run=last))
    stub.reports[430] = report(
        430, summary=last, summary_lines=["本次未生效，已退回待結算"],
        changes={"tables": [table("transactions", 1, "2026-06-05", "2026-06-05")],
                 "records_total": 1, "handed_back": True},
        failure={"phase": "逐日估值", "reason_text": "逐日估值失敗：記憶體不足",
                 "consequence": "保留第 41 版（結算至 2026-06-10），今天這段照常即時計算"})
    stub.changes["run:430"] = changes_page(
        [change(1, run_id=430, state="pending", attempted_run_ids=[430])], run_id=430)
    with stubbed(fresh_page, stub):
        for _ in range(2):   # the panel reads the report from the server: a reload keeps it
            _open(fresh_page, live_server)
            _chip(fresh_page).click()
            fail = fresh_page.locator(".st-sec.st-sec-fail")
            expect(fail).to_contain_text("逐日估值")
            expect(fail).to_contain_text("逐日估值失敗：記憶體不足")
            expect(fail).to_contain_text("保留第 41 版")
            card = fresh_page.locator(".st-card").first
            expect(card.locator(".st-summary-lines")).to_contain_text("本次未生效，已退回待結算")
            # the handed-back record reads E's page sentence, once — not 「曾於 #430 嘗試」
            trail = card.locator(".st-sec[data-sec='rep-changes'] .st-table")
            expect(trail).to_contain_text("本次未生效，已退回待結算")
            expect(trail).not_to_contain_text("#430")
            expect(fresh_page.get_by_role("button", name="重試")).to_be_visible()
            fresh_page.reload(wait_until="load")
        _open(fresh_page, live_server)
        _chip(fresh_page).click()
        fresh_page.get_by_role("button", name="重試").click()
        expect(fresh_page.locator(".toast")).to_contain_text("重新結算已開始")
        assert ("/api/settlement/run", {"scope": "dirty"}) in stub.posts


@pytest.mark.e2e
def test_a_selfcheck_difference_names_what_differed(
    live_server: str, fresh_page: Page
) -> None:
    """Review F #4: a period-close difference never reaches settle_run_diff — the report's
    `selfcheck` and `summary_lines` are the only place it shows."""
    last = summary(431, reason="selfcheck", selfcheck_diff=True, had_changes=False,
                   trigger_text=trigger("selfcheck", "2026-06-07T04:30:00+08:00",
                                        started_by=None))
    stub = SettleStub(status("selfcheck_diff", last_run=last))
    line = "自檢：與第 41 版逐列比對，發現 3 處差異，最早於 2026-03-31（settled_period）"
    stub.reports[431] = report(431, summary=last, summary_lines=[line], selfcheck={
        "compared_through": "2026-06-06", "differences": 3, "first_day": "2026-03-31",
        "first_table": "settled_period"})
    with stubbed(fresh_page, stub):
        _open(fresh_page, live_server)
        _chip(fresh_page).click()
        body = fresh_page.locator(".st-drawer .st-body")
        expect(body).to_contain_text("每週自檢發現差異")
        expect(body.locator(".st-summary-lines")).to_contain_text(line)
        sc = body.locator(".st-sec[data-sec='rep-selfcheck']")
        expect(sc).to_contain_text("比對至")
        expect(sc).to_contain_text("2026-06-06")
        expect(sc).to_contain_text("3 處")
        expect(sc).to_contain_text("2026-03-31（月結與年結）")


@pytest.mark.e2e
def test_on_a_phone_the_chip_fits_and_the_panel_is_a_full_height_sheet(
    live_server: str, fresh_page: Page
) -> None:
    stub = SettleStub(status("pending", dirty_from="2025-06-16", stale_from="2025-06-16"))
    with stubbed(fresh_page, stub):
        fresh_page.set_viewport_size({"width": 390, "height": 844})
        fresh_page.goto(live_server + "/index.html", wait_until="load")
        expect(_chip(fresh_page)).to_be_visible()
        m = fresh_page.evaluate(
            "() => ({sw: document.scrollingElement.scrollWidth,"
            " cw: document.scrollingElement.clientWidth})")
        assert m["sw"] <= m["cw"] + 1, f"the chip pushes the page sideways: {m}"
        _chip(fresh_page).click()
        fresh_page.wait_for_timeout(400)   # the drawer's slide-in animation (0.18 s)
        box = fresh_page.locator(".st-drawer").bounding_box()
        assert box is not None
        assert box["width"] >= 389 and box["height"] >= 843, box
        assert box["x"] <= 1, box
