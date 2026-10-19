"""PERF spec 2026-10-18 §14.1–§14.4 (plan §6.3, package F): the settlement chip, its detail
panel, the re-settle prompt and the menu's 重新結算 — in a real browser.

The settlement API is STUBBED here (`page.route` on `fresh_page` only, removed at the end):
the backend (package E) is built in parallel, and the plan moves these flows onto the real
backend (`flow_server(settle=True)`) at the wave-2 merge. Every stubbed body is built through
the Pydantic contract models of ``portfolio/settlement_models.py`` and dumped exactly as the
API dumps them, so a stub can never drift from the contract the frontend is built against.
The page itself is served by the session ``live_server`` (golden ledger, ``PD_SETTLE=off``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Page, Route, expect

from portfolio_dash.portfolio.settlement_models import (
    PHASES,
    ChangeRecord,
    RunProgress,
    RunReport,
    RunSummary,
    SettlementStatus,
)

_PATTERN = "**/api/settlement/**"


def _dump(model: Any) -> Any:
    return model.model_dump(mode="json", by_alias=True)


def status(state: str, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "state": state, "generation": 41, "settled_through": "2026-06-10", "calc_ok": True,
        "live_from": "2026-06-11", "final_through": "2026-06-10",
    }
    base.update(kw)
    return _dump(SettlementStatus(**base))


def summary(run_id: int, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "run_id": run_id, "status": "published", "status_text": "完成", "reason": "nightly",
        "trigger_text": "每晚例行結算於 06:40 啟動", "started_at": "2026-06-10T06:40:00+08:00",
        "finished_at": "2026-06-10T06:40:09+08:00", "duration_s": 9.2, "eta_initial_s": 10.0,
        "range_from": "2026-06-10", "range_to": "2026-06-10", "days": 1,
        "generation_before": 40, "generation_after": 41,
    }
    base.update(kw)
    return _dump(RunSummary(**base))


def report(run_id: int, **kw: Any) -> dict[str, Any]:
    sm = kw.pop("summary", None) or summary(run_id)
    base: dict[str, Any] = {
        "summary": sm,
        "inputs": {"scope": "nightly", "reason": "nightly", "through": "2026-06-10"},
        "range": {"from": sm["range_from"], "to": sm["range_to"], "days": sm["days"],
                  "kept_before": 400, "kind": "dirty"},
        "versions": {"generation_before": 40, "generation_after": 41,
                     "settled_through_before": "2026-06-09", "settled_through_after": "2026-06-10",
                     "calc_version_before": "a1b2c3d", "calc_version": "a1b2c3d",
                     "reporting_ccy_before": "TWD", "reporting_ccy": "TWD"},
        "validation": {"status": "done", "findings": []},
        "steps": [{"key": k, "label": label, "status": "done", "elapsed_s": 0.4}
                  for k, label in PHASES],
        "history": {"counts": {}, "note": None},
    }
    base.update(kw)
    return _dump(RunReport(**base))


def progress(run_id: int, **kw: Any) -> dict[str, Any]:
    phases = []
    for i, (key, label) in enumerate(PHASES):
        st = "done" if i < 3 else ("running" if i == 3 else "pending")
        ph: dict[str, Any] = {"key": key, "label": label, "status": st}
        if st == "done":
            ph.update(started_at="2026-06-11T10:32:02+08:00", elapsed_s=1.8,
                      detail="86 檔 · 價格 74,393 筆" if key == "load" else None)
        if key == "valuate":
            ph.update(done=281, total=489, unit="天", rate="35 天/秒", elapsed_s=7.9,
                      started_at="2026-06-11T10:32:04+08:00",
                      detail="已寫入 2025-06 至 2026-03（10 個月）")
        if key == "close":
            ph.update(total=17, unit="個期間")
        phases.append(ph)
    base: dict[str, Any] = {
        "run_id": run_id, "status": "running", "reason": "prompt",
        "started_by": "user:owner", "trigger_text": "{started_by} 於 10:32 啟動（交易更正）",
        "range": {"from": "2025-06-16", "to": "2026-06-10", "days": 360, "kept_before": 1694,
                  "kind": "dirty"},
        "versions": {"generation_before": 41, "settled_through_before": "2026-06-10",
                     "calc_version_before": "a1b2c3d", "calc_version": "a1b2c3d",
                     "reporting_ccy_before": "TWD", "reporting_ccy": "TWD"},
        "started_at": "2026-06-11T10:32:02+08:00", "elapsed_s": 12.4, "percent": 58,
        "eta_s": 8.0, "eta_initial_s": 18.0,
        "eta_basis": "逐日估值實測速度 35 天/秒；其餘階段依上次結算的耗時",
        "priority": "背景低優先序",
        "changes": {"tables": [{"table": "transactions", "label": "交易", "changes": 2,
                                "earliest": "2025-06-16", "latest": "2025-06-18"},
                               {"table": "prices", "label": "價格", "keys": 3, "changes": 14,
                                "earliest": "2025-07-01", "latest": "2025-07-21"}],
                    "records_total": 2},
        "validation": {"status": "done", "findings": [
            {"kind": "oversold", "day": "2025-08-01", "account_id": "tw_broker",
             "symbol": "2330",
             "text": "以待釐清處理：{account:tw_broker} 2330 於 2025-08-01 賣超"}]},
        "phases": phases,
        "current": {"day": "2026-03-24", "positions": 41},
        "queued": [{"from": "2026-05-03", "changes": 1, "reason": "結算期間新增的變更"}],
        "on_cancel": "保留第 41 版（結算至 2026-06-10）；這 2 筆交易與 14 筆價格變更回到待結算",
    }
    base.update(kw)
    return _dump(RunProgress(**base))


def change(i: int, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": i, "seq": 100 + i, "table": "transactions", "op": "update", "row_key": str(500 + i),
        "old_account_id": "tw_broker", "new_account_id": "tw_broker", "old_symbol": "2330",
        "new_symbol": "2330", "old_day": "2025-06-18", "new_day": "2025-06-16",
        "actor": "user:owner", "written_at": "2026-06-11T10:31:40+08:00", "state": "claimed",
        "claimed_run_id": 412,
    }
    base.update(kw)
    return _dump(ChangeRecord(**base))


class SettleStub:
    """The stubbed /api/settlement/* — mutable, and it records every request it answered."""

    def __init__(self, st: dict[str, Any]) -> None:
        self.status = st
        self.reports: dict[int, dict[str, Any]] = {}
        self.runs: list[dict[str, Any]] = []
        self.changes: list[dict[str, Any]] = []
        self.periods: list[dict[str, Any]] = []
        self.posts: list[tuple[str, Any]] = []
        self.gets: list[str] = []

    def handle(self, route: Route) -> None:
        req = route.request
        url = urlparse(req.url)
        path = url.path
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if req.method == "POST":
            body = json.loads(req.post_data) if req.post_data else None
            self.posts.append((path, body))
            if path.endswith("/run"):
                return self._json(route, {"run_id": 900 + len(self.posts)}, 202)
            return self._json(route, {}, 200)
        self.gets.append(req.url)
        if path.endswith("/settlement/status"):
            return self._json(route, self.status)
        if path.endswith("/settlement/runs"):
            rows = [r for r in self.runs if not q.get("only_changes") or r["had_changes"]]
            off, lim = int(q.get("offset", 0)), int(q.get("limit", 20))
            return self._json(route, {"rows": rows[off:off + lim], "total_count": len(rows)})
        m = re.search(r"/settlement/runs/(\d+)(/diff)?$", path)
        if m:
            if m.group(2):
                return self._json(route, {"rows": [], "total_count": 0})
            rep = self.reports.get(int(m.group(1)))
            if rep is None:
                return self._json(route, {"error": {"code": "not_found",
                                                    "message": "已超過保留期限"}}, 404)
            return self._json(route, rep)
        if path.endswith("/settlement/changes"):
            off, lim = int(q.get("offset", 0)), int(q.get("limit", 50))
            rows = self.changes[off:off + lim]
            return self._json(route, {"rows": rows, "total_count": len(self.changes)})
        if path.endswith("/settlement/periods"):
            return self._json(route, {"rows": self.periods, "total_count": len(self.periods)})
        return self._json(route, {"error": {"code": "not_found", "message": "無此路徑"}}, 404)

    @staticmethod
    def _json(route: Route, body: Any, status_code: int = 200) -> None:
        route.fulfill(status=status_code, content_type="application/json",
                      body=json.dumps(body, ensure_ascii=False))

    def count(self, needle: str) -> int:
        return sum(1 for u in self.gets if needle in u)


@contextmanager
def stubbed(page: Page, stub: SettleStub,
            extra: list[tuple[str, Callable[[Route], None]]] | None = None) -> Iterator[SettleStub]:
    """Install the stub (and any extra routes) on THIS page only; always removed."""
    page.route(_PATTERN, stub.handle)
    for pattern, handler in extra or []:
        page.route(pattern, handler)
    try:
        yield stub
    finally:
        page.unroute(_PATTERN, stub.handle)
        for pattern, handler in extra or []:
            page.unroute(pattern, handler)


def _open(page: Page, base: str, path: str = "/instruments.html") -> None:
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(base + path, wait_until="load")


def _chip(page: Page) -> Any:
    return page.locator("#settle-chip")


def _refresh(page: Page) -> None:
    page.evaluate("() => window.pdSettlement.refresh()")


@pytest.mark.e2e
def test_off_shows_no_chip_no_prompt_and_no_banner(live_server: str, fresh_page: Page) -> None:
    stub = SettleStub(status("off", needs_prompt=True, dirty_from="2025-06-16",
                             stale_from="2025-06-16"))
    with stubbed(fresh_page, stub):
        _open(fresh_page, live_server, "/index.html")
        fresh_page.wait_for_function("() => window.pdSettlement && window.pdSettlement.status()")
        assert _chip(fresh_page).count() == 0 or not _chip(fresh_page).is_visible()
        assert fresh_page.locator(".modal-backdrop").count() == 0
        assert fresh_page.locator("[data-settle-banner]:not([hidden])").count() == 0


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
def test_the_live_panel_shows_the_progress_and_polls_every_second(
    live_server: str, fresh_page: Page
) -> None:
    stub = SettleStub(status("running", run=progress(412)))
    stub.changes = [change(1), change(2, op="insert", old_day=None, new_day="2025-06-17",
                                     actor="job:quotes_tw", import_batch_id="b-7",
                                     attempted_run_ids=[411])]
    with stubbed(fresh_page, stub):
        _open(fresh_page, live_server)
        _chip(fresh_page).click()
        body = fresh_page.locator(".st-drawer .st-body")
        expect(body).to_contain_text("重新結算：2025-06-16 至 2026-06-10，共 360 天")
        expect(body).to_contain_text("owner 於 10:32 啟動（交易更正）")
        expect(body).to_contain_text("已用 12 秒 · 預估還要 8.0 秒")
        expect(body).to_contain_text("背景低優先序")
        expect(body).to_contain_text("交易 2 筆，2025-06-16 至 2025-06-18")
        expect(body).to_contain_text("價格 3 檔 14 筆，2025-07-01 至 2025-07-21")
        # every claimed record, account names resolved from the {account:…} tokens' ids
        trail = fresh_page.locator(".st-drawer .st-table").first
        expect(trail).to_contain_text("2025-06-18 → 2025-06-16")
        expect(trail).to_contain_text("曾於 #411 嘗試")
        expect(trail).to_contain_text("排程")
        expect(body).to_contain_text("逐日估值")
        expect(body).to_contain_text("281 / 489 天 · 35 天/秒")
        expect(fresh_page.locator(".st-step-done .st-step-mark").first).to_have_text("✓")
        expect(fresh_page.locator(".st-step-running .st-step-mark")).to_have_text("▸")
        expect(body).to_contain_text("正在估值：2026-03-24（41 個部位）")
        expect(body).to_contain_text("完成後接著處理：2026-05-03 起的變更（1 筆）")
        expect(body).to_contain_text("2330 於 2025-08-01 賣超")
        expect(body).not_to_contain_text("{account:")
        expect(body).to_contain_text("第 41 版 → 第 42 版（切換後生效）")
        expect(body).to_contain_text("逐日估值實測速度 35 天/秒")
        expect(fresh_page.locator(".st-bar-fill")).to_have_attribute("style", "width: 58%;")
        # polling: about once a second while the panel is open and the run is active
        before = stub.count("/settlement/status")
        fresh_page.wait_for_timeout(2600)
        assert stub.count("/settlement/status") - before >= 2
        # 取消（保留目前版本）: the consequence is the server's on_cancel, asked in confirmDialog
        expect(body).to_contain_text("取消的結果：保留第 41 版")
        fresh_page.get_by_role("button", name="取消（保留目前版本）").click()
        modal = fresh_page.locator(".modal")
        expect(modal).to_contain_text("這 2 筆交易與 14 筆價格變更回到待結算")
        modal.get_by_role("button", name="取消重新結算").click()
        fresh_page.wait_for_function(
            "() => !document.querySelector('.modal-backdrop')")
        assert ("/api/settlement/cancel", {}) in stub.posts


@pytest.mark.e2e
def test_the_idle_panel_shows_state_reports_and_steps_through_the_history(
    live_server: str, fresh_page: Page
) -> None:
    last = summary(430, had_changes=False)
    last_change = summary(425, had_changes=True, reason="prompt", status_text="完成",
                          trigger_text="{started_by} 於 09:12 啟動（交易更正）",
                          started_by="user:owner")
    stub = SettleStub(status("clean", last_run=last, last_change_run=last_change))
    stub.runs = [summary(430 - i, had_changes=(i % 5 == 0)) for i in range(25)]
    for r in stub.runs:
        stub.reports[r["run_id"]] = report(r["run_id"], summary=r)
    stub.reports[425] = report(425, summary=last_change,
                               changes={"tables": [{"table": "transactions", "label": "交易",
                                                    "changes": 1, "earliest": "2025-06-16",
                                                    "latest": "2025-06-16"}],
                                        "records_total": 1})
    stub.changes = [change(1)]
    with stubbed(fresh_page, stub):
        _open(fresh_page, live_server)
        _chip(fresh_page).click()
        body = fresh_page.locator(".st-drawer .st-body")
        expect(body).to_contain_text("已結算至 2026-06-10 · 第 41 版")
        expect(body).to_contain_text("最近一次結算 #430")
        expect(body).to_contain_text("最近一次結算變更 #425")
        expect(body).to_contain_text("交易 1 筆，2025-06-16")
        expect(body).to_contain_text("未發現待釐清項目")
        expect(body).to_contain_text("月結與年結無變動")
        expect(fresh_page.get_by_role("button", name="重新結算（驗證並重建歷史）")).to_be_visible()
        # the history: paged 20, newest first
        hist = fresh_page.locator(".st-sec[data-sec='history'] tbody tr")
        expect(hist).to_have_count(20)
        expect(fresh_page.locator(".st-sec[data-sec='history'] .st-pager-info")).to_have_text(
            "1–20／共 25 筆")
        # open the 20th row (#411), then 下一筆 crosses into the second page (#410)
        hist.nth(19).click()
        expect(fresh_page.locator(".st-card-title")).to_have_text("結算 #411")
        fresh_page.get_by_role("button", name="下一筆").click()
        expect(fresh_page.locator(".st-card-title")).to_have_text("結算 #410")
        assert any("offset=20" in u for u in stub.gets if "/settlement/runs?" in u)
        fresh_page.get_by_role("button", name="上一筆").click()
        expect(fresh_page.locator(".st-card-title")).to_have_text("結算 #411")
        # the nightly filter: only runs that settled a change
        fresh_page.get_by_role("button", name="← 回到目前狀態").click()
        fresh_page.locator(".st-filter input").check()
        expect(hist).to_have_count(5)
        # a section's collapsed state is remembered per browser
        fresh_page.locator(".st-sec[data-sec='history'] > summary").click()
        fresh_page.wait_for_function(   # <details> fires `toggle` asynchronously
            "() => (JSON.parse(localStorage.getItem('pd_settle_sections') || '{}')).history"
            " === false")


@pytest.mark.e2e
def test_a_reload_keeps_the_last_report(live_server: str, fresh_page: Page) -> None:
    last = summary(430, status="failed", status_text="失敗", had_changes=True)
    stub = SettleStub(status("failed", last_run=last))
    stub.reports[430] = report(430, summary=last, failure={
        "phase": "valuate", "reason_text": "逐日估值失敗：記憶體不足",
        "consequence": "保留第 41 版（結算至 2026-06-10），今天這段照常即時計算"})
    with stubbed(fresh_page, stub):
        for _ in range(2):   # the panel reads the report from the server: a reload keeps it
            _open(fresh_page, live_server)
            _chip(fresh_page).click()
            fail = fresh_page.locator(".st-sec.st-sec-fail")
            expect(fail).to_contain_text("逐日估值")
            expect(fail).to_contain_text("逐日估值失敗：記憶體不足")
            expect(fail).to_contain_text("保留第 41 版")
            expect(fresh_page.get_by_role("button", name="重試")).to_be_visible()
            fresh_page.reload(wait_until="load")
        # 重試 re-runs the failed run's scope
        _open(fresh_page, live_server)
        _chip(fresh_page).click()
        fresh_page.get_by_role("button", name="重試").click()
        expect(fresh_page.locator(".toast")).to_contain_text("重新結算已開始")
        assert ("/api/settlement/run", {"scope": "dirty"}) in stub.posts


@pytest.mark.e2e
def test_the_prompt_is_asked_once_per_seq_and_starts_a_dirty_run(
    live_server: str, fresh_page: Page
) -> None:
    pending = {"tables": [{"table": "transactions", "label": "交易", "changes": 2,
                           "earliest": "2025-06-16", "latest": "2025-06-18"}],
               "records_total": 2}
    st = status("pending", needs_prompt=True, dirty_from="2025-06-16",
                stale_from="2025-06-16", prompt={"from": "2025-06-16", "days": 489, "est_s": 9.6},
                pending=pending)
    st["seq"] = 7     # the additive status field the prompt keys on
    stub = SettleStub(st)
    with stubbed(fresh_page, stub):
        _open(fresh_page, live_server)
        modal = fresh_page.locator(".modal.st-prompt")
        expect(modal).to_contain_text("要重新結算歷史資料嗎？")
        expect(modal).to_contain_text(
            "這筆變更影響 2025-06-16 起的歷史結算（共 489 天；待結算：交易 2 筆）")
        expect(modal).to_contain_text("重新結算約需 9.6 秒，以背景低優先序執行")
        modal.get_by_role("button", name="立即重新結算").click()
        fresh_page.wait_for_function(
            "() => !document.querySelector('.modal-backdrop')")
        assert ("/api/settlement/run", {"scope": "dirty"}) in stub.posts
        # the same seq is not asked again in this browser session …
        fresh_page.reload(wait_until="load")
        fresh_page.wait_for_function("() => window.pdSettlement && window.pdSettlement.status()")
        fresh_page.wait_for_timeout(400)
        assert fresh_page.locator(".modal.st-prompt").count() == 0
        # … a new change (seq 8) after a write is: debounced on pd-mutated
        st8 = dict(st)
        st8["seq"] = 8
        stub.status = st8
        posts = len(stub.posts)
        fresh_page.evaluate(
            "() => document.dispatchEvent(new CustomEvent('pd-mutated', "
            "{detail: {method: 'PUT', path: '/api/ledgers/transactions/1'}}))")
        expect(fresh_page.locator(".modal.st-prompt")).to_be_visible()
        fresh_page.locator(".modal.st-prompt").get_by_role("button", name="稍後").click()
        fresh_page.wait_for_timeout(300)
        assert len(stub.posts) == posts, "稍後 starts nothing"
        expect(_chip(fresh_page)).to_contain_text("待結算 · 2025-06-16 起")


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


@pytest.mark.e2e
def test_the_menu_starts_a_full_run_and_off_keeps_the_replay(
    live_server: str, fresh_page: Page
) -> None:
    stub = SettleStub(status("clean"))
    recompute: list[str] = []

    def _recompute(route: Route) -> None:
        recompute.append(route.request.method)
        route.fulfill(status=200, content_type="application/json", body="{}")

    with stubbed(fresh_page, stub, [("**/api/actions/recompute", _recompute)]):
        _open(fresh_page, live_server, "/index.html")
        expect(_chip(fresh_page)).to_be_visible()
        fresh_page.locator(".refresh-wrap .btn-refresh").click()
        fresh_page.get_by_role("button", name=re.compile("重新結算（驗證並重建歷史）")).click()
        expect(fresh_page.locator(".toast")).to_contain_text("重新結算已開始（驗證並重建歷史）")
        assert ("/api/settlement/run", {"scope": "full"}) in stub.posts
        assert not recompute
        # PD_SETTLE=off: there is no history to rebuild — the item is today's strict replay
        stub.status = status("off")
        _refresh(fresh_page)
        fresh_page.locator(".refresh-wrap .btn-refresh").click()
        with fresh_page.expect_navigation(wait_until="load"):
            fresh_page.get_by_role("button", name=re.compile("重新結算（驗證並重建歷史）")).click()
        assert recompute == ["POST"]
