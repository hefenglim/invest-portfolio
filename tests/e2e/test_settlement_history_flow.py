"""PERF spec 2026-10-18 §11.4, §14.5–§14.6 (plan §6.3 / §8, package F): the history panels'
states the REAL backend cannot produce cheaply — 暫定 (it needs a quote job that did not
confirm a month-end close), a refused receivable payout, a 待釐清 position on today's book, a
restated year close, the 404 of a server without the year closes, and the banner's 績效比較
half on a stale range of any length.

The same panels on the real backend — the month rows and their ⚠ notes, 更新前版本 while a
back-dated change waits, 已重述 / 上線重述 with the server's reasons, the real year closes, the
empty ledger, the swap refreshing the charts — are ``test_settlement_monthly_real_flow.py``.
Here ``/api/settlement/*`` and ``/api/snapshots`` are stubbed on ``fresh_page`` only (removed at
the end) in E's and G's real shapes (``tests/e2e/settlement_stubs.py``, pinned to the routes by
``tests/contract/test_settlement_stub_shapes.py``).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e.settlement_stubs import (
    SettleStub,
    period,
    restatement,
    snapshot_row,
    status,
    stubbed,
)

_SNAPSHOTS = "**/api/snapshots*"


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
    snaps = SnapStub([snapshot_row("2026-06"), snapshot_row("2026-05", stale=True)])
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
def test_monthly_marks_the_real_backend_cannot_produce_cheaply(
    live_server: str, fresh_page: Page
) -> None:
    stub = SettleStub(status("clean"))
    stub.periods = [period("2026", source="live", current=True),
                    period("2025", restated=True, restatements=[restatement(
                        3, kind="Y", period="2025")])]
    snaps = SnapStub([
        snapshot_row("2026-06", current=True, notes={"pending_positions": [
            {"account_id": "tw_broker", "symbol": "2330", "kind": "oversold"}]}),
        snapshot_row("2026-05", provisional="月底收盤價尚未確定：台股報價工作未完成"),
        snapshot_row("2026-01", xirr=None, xirr_reason="期間不足 90 天，XIRR 不計",
                     period_twr=None, period_twr_reason="待結算：期間跨越更新前版本",
                     notes={"missing_prices": ["2603"], "dividends_receivable": [
                         {"account_id": "tw_broker", "symbol": "2884", "ex_date": "2026-01-20",
                          "pay_date": "2026-02-15", "ccy": "TWD", "net": "1800"},
                         {"account_id": "tw_broker", "symbol": "2603", "ex_date": "2026-01-21",
                          "pay_date": "2026-02-16", "ccy": "TWD", "net": "500",
                          "booked": False, "unbooked_note": "未入帳（待釐清）",
                          "unbooked_reason": "此帳戶在發放日前沒有 2603 的部位"}]}),
    ])
    with stubbed(fresh_page, stub, [(_SNAPSHOTS, snaps.handle)]):
        _dashboard(fresh_page, live_server)
        panel = fresh_page.locator("#snapshots-panel")
        rows = panel.locator("tbody tr")
        expect(rows).to_have_count(3)
        # the live current month reads 至今 (its 當期報酬 is to date); a closed month never
        expect(rows.nth(0).locator(".snap-mark-current")).to_have_text("至今")
        expect(rows.nth(1).locator(".snap-mark-current")).to_have_count(0)
        prov = rows.nth(1).locator(".snap-mark-provisional")
        expect(prov).to_have_text("暫定")
        assert prov.get_attribute("title") == "月底收盤價尚未確定：台股報價工作未完成"
        warn = rows.nth(2).locator(".snap-mark-note")
        title = warn.get_attribute("title") or ""
        assert "缺價格：2603" in title and "已除息、股利未入帳" in title, title
        # X1: a refused payout is listed marked, with the replay's own reason
        assert ("2603（2026-01-21 除息，2026-02-16 發放；未入帳（待釐清）："
                "此帳戶在發放日前沒有 2603 的部位）") in title, title
        assert "2884（2026-01-20 除息，2026-02-15 發放）" in title, title
        # today's book carries a 待釐清 position: one note under the table
        expect(panel.locator("#snapshots-note")).to_contain_text("過去月份的數字可能隨之更新")
        for cls in ("snap-mark-provisional", "snap-mark-note"):
            assert panel.locator("." + cls).first.get_attribute("style") is None
        # a restated YEAR close, its tooltip from the period's own restatements
        panel.locator("#snapshots-kind .range-btn[data-kind='Y']").click()
        expect(rows).to_have_count(2)
        expect(rows.nth(0).locator(".snap-mark-current")).to_have_text("至今")
        mark = rows.nth(1).locator(".snap-mark-restated")
        expect(mark).to_have_text("已重述")
        title = mark.get_attribute("title") or ""
        assert "2025-06-16 交易更正後重新結算" in title and "transactions:" not in title, title


_HINT_TITLE = "觀察期不足一年，年化 XIRR 波動較大，僅供參考。"


@pytest.mark.e2e
def test_a_first_year_xirr_carries_the_short_window_hint(
    live_server: str, fresh_page: Page
) -> None:
    """Accountant MINOR 7b (owner ruling 2026-10-22): the 30-day gate stays, and a 累計 XIRR
    over a window shorter than a year carries the KPI card's own hint 「觀察期 N 天・短窗參考」 —
    on a month row (``xirr_window_days`` of ``GET /api/snapshots``) and on a year row (the
    close's ``kpi``) alike; a window of a year or more, or a blank XIRR, carries none."""
    stub = SettleStub(status("clean"))
    stub.periods = [period("2026", source="live", current=True,
                           kpi={"xirr": "0.21", "xirr_window_days": 157}),
                    period("2025", kpi={"xirr": "0.18", "xirr_window_days": 400})]
    snaps = SnapStub([
        snapshot_row("2026-06", xirr="0.2345", xirr_window_days=157),
        snapshot_row("2026-05", xirr="0.1234", xirr_window_days=365),
        snapshot_row("2026-01", xirr=None, xirr_window_days=26,
                     xirr_reason="觀察期 26 天・不足以年化"),
    ])
    with stubbed(fresh_page, stub, [(_SNAPSHOTS, snaps.handle)]):
        _dashboard(fresh_page, live_server)
        # the KPI card reads the same helper: its hint follows the dashboard's own figures
        k = fresh_page.evaluate("() => window.pdDashboard.then((d) => d.kpis)")
        card_hint = fresh_page.locator("#kpi-band .badge-window-mini")
        if k["xirr"] is not None and k["xirr_window_days"] is not None \
                and k["xirr_window_days"] < 365:
            expect(card_hint).to_have_text(f"觀察期 {k['xirr_window_days']} 天・短窗參考")
        else:
            expect(card_hint).to_have_count(0)
        rows = fresh_page.locator("#snapshots-body tr")
        expect(rows).to_have_count(3)
        first_year = rows.nth(0).locator("td").nth(4)
        hint = first_year.locator(".snap-window .badge-window-mini")
        expect(hint).to_have_text("觀察期 157 天・短窗參考")
        assert hint.get_attribute("title") == _HINT_TITLE
        expect(first_year).to_contain_text("+23.45%")
        assert hint.get_attribute("style") is None   # DEF-077: a class, never inline
        # a year's window (365 days) reads no hint; a blank XIRR keeps its reason, no hint
        expect(rows.nth(1).locator(".badge-window-mini")).to_have_count(0)
        blank = rows.nth(2).locator("td").nth(4)
        expect(blank.locator(".badge-window-mini")).to_have_count(0)
        assert blank.get_attribute("title") == "觀察期 26 天・不足以年化"
        # the year rows: the same hint from the close's own kpi
        fresh_page.locator("#snapshots-kind .range-btn[data-kind='Y']").click()
        expect(rows).to_have_count(2)
        expect(rows.nth(0).locator("td").nth(4).locator(".badge-window-mini")).to_have_text(
            "觀察期 157 天・短窗參考")
        expect(rows.nth(1).locator(".badge-window-mini")).to_have_count(0)


@pytest.mark.e2e
def test_a_server_without_the_year_closes_reads_an_empty_state(
    live_server: str, fresh_page: Page
) -> None:
    """Review F #10: before G mounted /api/settlement/periods a 404 printed the generic
    「找不到指定的資料或頁面」; it is an empty state in words (a server can still answer 404)."""
    stub = SettleStub(status("clean"))
    stub.periods = None
    snaps = SnapStub([snapshot_row("2026-06")])
    with stubbed(fresh_page, stub, [(_SNAPSHOTS, snaps.handle)]):
        _dashboard(fresh_page, live_server)
        body = fresh_page.locator("#snapshots-body")
        expect(body.locator("tr")).to_have_count(1)
        fresh_page.locator("#snapshots-kind .range-btn[data-kind='Y']").click()
        expect(body).to_contain_text("年度成績尚未提供")
        expect(body).not_to_contain_text("找不到指定的資料或頁面")
