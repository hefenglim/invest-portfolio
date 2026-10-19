"""PERF spec 2026-10-18 §11.4, §14.4–§14.6 (plan §6.3, package F): what the dashboard's
history panels do with the settlement — the 更新前版本 banner, 月度成績's settled closes
(the marks, the 月／年 toggle, the empty state, the 待釐清 note) and the in-place refresh when
a swap is announced.

Stubbed in E's real shapes (``tests/e2e/settlement_stubs.py``, pinned to the routes by
``tests/contract/test_settlement_stub_shapes.py``): ``/api/settlement/*`` and ``/api/snapshots``
on ``fresh_page`` only, removed at the end. The plan moves the 月度成績 flows onto the real
backend at W4 (the year closes and the snapshot fields are package G's).
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e.settlement_stubs import (
    SettleStub,
    period,
    progress,
    restatement,
    status,
    stubbed,
)

_SNAPSHOTS = "**/api/snapshots*"


def _month(month: str, **kw: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "month": month, "as_of": month + "-28T23:59:00+08:00", "reporting_ccy": "TWD",
        "total_value": "1234567.89", "total_return": "234567.8", "total_return_rate": "0.2345",
        "xirr": "0.1234", "by_currency": {"TWD": "1234567.89"}, "kind": "M",
        "period_twr": "0.0123", "notes": {},
    }
    row.update(kw)
    return row


class SnapStub:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.hits = 0

    def handle(self, route: Route) -> None:
        self.hits += 1
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"rows": self.rows, "total_count": len(self.rows)},
                                      ensure_ascii=False))


def _dashboard(page: Page, base: str) -> None:
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(base + "/index.html", wait_until="load")
    page.wait_for_selector(".kpi-card")


@pytest.mark.e2e
def test_the_stale_banner_is_on_the_history_panels_and_never_on_the_kpi_band(
    live_server: str, fresh_page: Page
) -> None:
    stub = SettleStub(status("pending", dirty_from="2025-06-16", stale_from="2025-06-16",
                             stale_to="2026-04-30"))
    snaps = SnapStub([_month("2026-06"), _month("2026-05", stale=True)])
    with stubbed(fresh_page, stub, [(_SNAPSHOTS, snaps.handle)]):
        _dashboard(fresh_page, live_server)
        trend = fresh_page.locator(".trend-panel .st-banner")
        expect(trend).to_be_visible()
        expect(trend).to_contain_text("更新前版本 · 尚未包含 2025-06-16 起的變更")
        expect(trend.locator(".st-banner-twr")).to_be_hidden()   # 市值 mode
        monthly = fresh_page.locator("#snapshots-panel .st-banner")
        expect(monthly).to_contain_text("更新前版本 · 尚未包含 2025-06-16 起的變更")
        assert fresh_page.locator("#kpi-band .st-banner").count() == 0
        # 績效比較: the join of stale and live days can distort a period return
        fresh_page.locator("#trend-mode .range-btn[data-mode='twr']").click()
        expect(trend.locator(".st-banner-twr")).to_be_visible()
        expect(trend).to_contain_text("期間報酬可能失真")
        # [查看結算] opens the panel on the pending state
        trend.get_by_role("button", name="查看結算").click()
        expect(fresh_page.locator(".st-drawer")).to_contain_text(
            "待結算：歷史走勢與月度成績尚未包含 2025-06-16 起的變更")
        # a clean status takes every banner away
        fresh_page.locator(".st-drawer .sd-close").click()
        stub.status = status("clean")
        fresh_page.evaluate("() => window.pdSettlement.refresh()")
        expect(fresh_page.locator("[data-settle-banner]:not([hidden])")).to_have_count(0)


@pytest.mark.e2e
def test_monthly_rows_carry_their_marks_and_the_toggle_reads_the_year_closes(
    live_server: str, fresh_page: Page
) -> None:
    stub = SettleStub(status("clean"))
    stub.periods = [period("2026", source="live"), period("2025", restated=True)]
    snaps = SnapStub([
        _month("2026-06", notes={"pending_positions": [
            {"account_id": "tw_broker", "symbol": "2330", "kind": "oversold"}]}),
        _month("2026-05", provisional=True),
        _month("2026-04", stale=True),
        _month("2026-03", restated={"cutover": False, "rows": [restatement(1)]}),
        _month("2026-02", restated={"cutover": True, "rows": [restatement(
            2, period="2026-02", cutover=True, source="portfolio_snapshots",
            reason="上線重述：月底定格值改為月底結算值")]}),
        _month("2026-01", xirr=None, xirr_reason="期間不足 90 天，XIRR 不計",
               period_twr=None, period_twr_reason="待結算：期間跨越更新前版本",
               notes={"missing_prices": ["2603"], "dividends_receivable": [
                   {"account_id": "tw_broker", "symbol": "2884", "ex_date": "2026-01-20",
                    "pay_date": "2026-02-15", "ccy": "TWD", "net": "1800"}]}),
    ])
    with stubbed(fresh_page, stub, [(_SNAPSHOTS, snaps.handle)]):
        _dashboard(fresh_page, live_server)
        panel = fresh_page.locator("#snapshots-panel")
        head = panel.locator("thead th")
        expect(head).to_have_text(["月份", "總市值", "累計總報酬", "累計報酬率", "累計 XIRR",
                                   "當期報酬（TWR）"])
        expect(panel.locator(".panel-sub")).to_contain_text("過去月份為月底結算值")
        rows = panel.locator("tbody tr")
        expect(rows).to_have_count(6)
        expect(rows.nth(1).locator(".snap-mark-provisional")).to_have_text("暫定")
        expect(rows.nth(2).locator(".snap-mark-stale")).to_have_text("更新前版本")
        assert "snap-row-stale" in (rows.nth(2).get_attribute("class") or "")
        restated = rows.nth(3).locator(".snap-mark-restated")
        expect(restated).to_have_text("已重述")
        title = restated.get_attribute("title") or ""
        assert "總市值" in title and "→" in title, title
        assert "2025-06-16 交易更正後重新結算" in title, title     # the zh reason
        assert "transactions:" not in title, title                  # never the raw source
        cut = rows.nth(4).locator(".snap-mark-cutover")
        expect(cut).to_have_text("上線重述")
        assert "portfolio_snapshots" not in (cut.get_attribute("title") or "")
        warn = rows.nth(5).locator(".snap-mark-note")
        assert "缺價格：2603" in (warn.get_attribute("title") or "")
        assert "已除息、股利未入帳" in (warn.get_attribute("title") or "")
        cells = rows.nth(5).locator("td")
        assert cells.nth(4).get_attribute("title") == "期間不足 90 天，XIRR 不計"
        assert cells.nth(5).get_attribute("title") == "待結算：期間跨越更新前版本"
        # today's book carries a 待釐清 position: one note under the table
        expect(panel.locator("#snapshots-note")).to_contain_text("過去月份的數字可能隨之更新")
        for mark in ("snap-mark-provisional", "snap-mark-stale", "snap-mark-note"):
            assert panel.locator("." + mark).first.get_attribute("style") is None
        # 年: the year closes from /api/settlement/periods?kind=Y
        panel.locator("#snapshots-kind .range-btn[data-kind='Y']").click()
        expect(head.first).to_have_text("年份")
        expect(rows).to_have_count(2)
        expect(rows.nth(0)).to_contain_text("2026")
        expect(rows.nth(1).locator(".snap-mark-restated")).to_have_text("已重述")
        assert rows.nth(0).locator("td").nth(4).get_attribute("title") == "期間不足一年，XIRR 不計"
        assert any("/settlement/periods?" in u and "kind=Y" in u for u in stub.gets)
        expect(panel.locator(".panel-sub")).to_contain_text("過去年份為年底結算值")
        # and back
        panel.locator("#snapshots-kind .range-btn[data-kind='M']").click()
        expect(rows).to_have_count(6)


@pytest.mark.e2e
def test_an_empty_monthly_panel_no_longer_promises_a_2350_snapshot(
    live_server: str, fresh_page: Page
) -> None:
    stub = SettleStub(status("clean"))
    snaps = SnapStub([])
    with stubbed(fresh_page, stub, [(_SNAPSHOTS, snaps.handle)]):
        _dashboard(fresh_page, live_server)
        body = fresh_page.locator("#snapshots-body")
        expect(body).to_contain_text("尚無月度成績")
        expect(body).not_to_contain_text("23:50")
        expect(fresh_page.locator("#snapshots-note")).to_be_hidden()
        # 年 before package G mounts /api/settlement/periods: a 404 reads as an empty state in
        # words, never the generic 「找不到指定的資料或頁面」 (review F #10)
        stub.periods = None
        fresh_page.locator("#snapshots-kind .range-btn[data-kind='Y']").click()
        expect(body).to_contain_text("年度成績尚未提供")
        expect(body).not_to_contain_text("找不到指定的資料或頁面")


@pytest.mark.e2e
def test_a_swap_refreshes_the_history_in_place_without_a_reload(
    live_server: str, fresh_page: Page
) -> None:
    stub = SettleStub(status("running", generation=41, run=progress(412)))
    snaps = SnapStub([_month("2026-06")])
    dashboards: list[str] = []

    def _count_dashboard(route: Route) -> None:
        dashboards.append(route.request.url)
        route.continue_()

    with stubbed(fresh_page, stub, [(_SNAPSHOTS, snaps.handle),
                                    ("**/api/dashboard", _count_dashboard)]):
        _dashboard(fresh_page, live_server)
        fresh_page.evaluate("() => { window.__noReload = 1; }")
        fresh_page.wait_for_function("() => window.pdSettlement && window.pdSettlement.status()")
        snaps_before, dash_before = snaps.hits, len(dashboards)
        # the run publishes generation 42: the chip's next poll sees the swap
        stub.status = status("clean", generation=42)
        fresh_page.evaluate("() => window.pdSettlement.refresh()")
        fresh_page.wait_for_function(
            "() => document.querySelector('#settle-chip').textContent.indexOf('已結算') >= 0")
        expect(fresh_page.locator("#trend-chart canvas").first).to_be_visible()
        fresh_page.wait_for_timeout(500)
        assert snaps.hits > snaps_before, "月度成績 re-read its rows"
        assert len(dashboards) > dash_before, "the trend re-read the history"
        assert fresh_page.evaluate("() => window.__noReload") == 1, "no page reload"
        assert re.search(r"/api/dashboard$", dashboards[-1])
