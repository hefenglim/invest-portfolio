"""PERF spec 2026-10-18 §11.4, §14.4–§14.6 on the REAL backend (plan §8 W4): 月度成績 and the
history panels against E's and G's routes, the store settled at boot in a child process
(``flow_server(settle=True)``, golden ledger, the real clock).

One flow follows a back-dated change through its whole life on the dashboard: the month rows
with their ⚠ notes and the go-live 上線重述, the real year closes behind 月／年, then a trade
written back in June (as a script would — the triggers record it) → the boot prompt → 稍後 →
待結算 with the 更新前版本 banner and the rows marked 更新前版本 → 立即重新結算 → progress → the
swap → the rows 已重述 with the server's reason (never its raw `source`), the banners gone and
the charts rebuilt in place. A second flow reads an empty ledger.

The states the real backend cannot produce cheaply (暫定, a refused receivable, a restated year)
stay stubbed in ``test_settlement_history_flow.py``.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from playwright.sync_api import Page, expect

from portfolio_dash.api import snapshots as snapshot_table
from portfolio_dash.data_ingestion.store import insert_transaction
from portfolio_dash.shared.models.enums import Side
from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory
from tests.e2e.test_settlement_real_flow import _SETTLE_TIMEOUT, _chip, _prompt, _settled

_JULY_FREEZE = ("2026-07", "2026-07-31T23:50:00+08:00", "TWD", "600000", "1000", "0.01", None,
                "{}")


def _seed_with_a_july_freeze(where: list[str]) -> Callable[[sqlite3.Connection], None]:
    """Golden, plus the retired 23:50 freeze of July (a month-end row that differs from the
    July close) — the go-live run restates it once, marked 上線重述."""
    def seed(conn: sqlite3.Connection) -> None:
        _seed_golden(conn)
        snapshot_table.ensure_table(conn)
        conn.execute("INSERT INTO portfolio_snapshots (month, as_of, reporting_ccy, total_value, "
                     "total_return, total_return_rate, xirr, by_currency) "
                     "VALUES (?,?,?,?,?,?,?,?)", _JULY_FREEZE)
        where.append(str(conn.execute("PRAGMA database_list").fetchone()[2]))
    return seed


def _row(page: Page, month: str) -> Any:
    return page.locator("#snapshots-body tr").filter(
        has=page.locator("td.snap-period", has_text=month))


def _title(locator: Any) -> str:
    return str(locator.get_attribute("title") or "")


@pytest.mark.e2e
def test_monthly_closes_through_a_backdated_change_on_the_real_store(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    db: list[str] = []
    base = flow_server(_seed_with_a_july_freeze(db), settle=True)
    page = fresh_page
    dashboards: list[str] = []
    page.on("request", lambda r: dashboards.append(r.url)
            if r.url.endswith("/api/dashboard") else None)
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(base + "/index.html", wait_until="load")
    expect(_chip(page)).to_be_visible(timeout=_SETTLE_TIMEOUT)
    _settled(page)
    page.reload(wait_until="load")          # read the rows the boot run settled
    page.wait_for_selector(".kpi-card")

    panel = page.locator("#snapshots-panel")
    expect(panel.locator("thead th")).to_have_text(
        ["月份", "總市值", "累計總報酬", "累計報酬率", "累計 XIRR", "當期報酬（TWR）"])
    june = _row(page, "2026-06")
    expect(june).to_have_count(1)
    assert "價格非當日：2330（2026-06-09）" in _title(june.locator(".snap-mark-note"))
    may = _row(page, "2026-05")
    assert "缺價格：2330、AAPL" in _title(may.locator(".snap-mark-note"))
    assert _title(may.locator("td").nth(4)).startswith("無法計算")      # the server's xirr_reason
    july = _row(page, "2026-07")
    cut = july.locator(".snap-mark-cutover")
    expect(cut).to_have_text("上線重述")
    assert "上線重述：月底定格值改為月底結算值" in _title(cut)
    assert "portfolio_snapshots" not in _title(cut)
    expect(panel.locator(".snap-mark-stale")).to_have_count(0)

    # 月／年: the real year closes (the current year is live)
    panel.locator("#snapshots-kind .range-btn[data-kind='Y']").click()
    expect(panel.locator("thead th").first).to_have_text("年份")
    expect(page.locator("#snapshots-body tr").first).to_contain_text("2026")
    expect(panel.locator(".panel-sub")).to_contain_text("過去年份為年底結算值")
    panel.locator("#snapshots-kind .range-btn[data-kind='M']").click()
    expect(_row(page, "2026-06")).to_have_count(1)

    # a trade written back in June, as a script would (the triggers record it)
    conn = sqlite3.connect(db[0], timeout=10)
    try:
        insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                           quantity=Decimal("1"), price=Decimal("500"), fees=Decimal("0"),
                           tax=Decimal("0"), trade_date=date(2026, 6, 5))
        conn.commit()
    finally:
        conn.close()
    page.reload(wait_until="load")
    _prompt(page).get_by_role("button", name="稍後").click()
    # 稍後 → 待結算, the banner on the history panels (never the KPI band), stale rows marked
    expect(_chip(page)).to_contain_text("待結算 · 2026-06-05 起")
    banner = panel.locator(".st-banner")
    expect(banner).to_contain_text("更新前版本 · 尚未包含 2026-06-05 起的變更")
    expect(page.locator(".trend-panel .st-banner")).to_be_visible()
    assert page.locator("#kpi-band .st-banner").count() == 0
    stale = _row(page, "2026-07").locator(".snap-mark-stale")
    expect(stale).to_have_text("更新前版本")
    expect(_row(page, "2026-06").locator(".snap-mark-stale")).to_have_text("更新前版本")

    # 立即重新結算 → progress → the swap → restated rows, no banner, the charts rebuilt
    page.evaluate("() => { window.__noReload = 1; }")
    before = len(dashboards)
    _chip(page).click()
    page.get_by_role("button", name="立即重新結算").click()
    page.wait_for_function(
        "() => document.querySelector('#settle-chip').classList.contains('st-chip-run')",
        timeout=_SETTLE_TIMEOUT)
    page.locator(".st-drawer .sd-close").click()
    _settled(page)
    restated = _row(page, "2026-06").locator(".snap-mark-restated")
    expect(restated).to_have_text("已重述", timeout=_SETTLE_TIMEOUT)
    title = _title(restated)
    assert "2026-06-05 交易更正後重新結算" in title, title
    assert not re.search(r"transactions:\d+", title), title
    expect(panel.locator(".snap-mark-stale")).to_have_count(0)
    expect(page.locator("[data-settle-banner]:not([hidden])")).to_have_count(0)
    for _ in range(40):
        if len(dashboards) > before:
            break
        page.wait_for_timeout(250)
    assert len(dashboards) > before, "the swap re-read the history for the charts"
    expect(page.locator("#trend-chart canvas").first).to_be_visible()
    assert page.evaluate("() => window.__noReload") == 1, "no page reload"


@pytest.mark.e2e
def test_an_empty_ledger_reads_the_empty_state_on_the_real_store(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(lambda conn: None, settle=True)
    page = fresh_page
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(base + "/index.html", wait_until="load")
    body = page.locator("#snapshots-body")
    expect(body).to_contain_text("尚無月度成績", timeout=_SETTLE_TIMEOUT)
    expect(body).not_to_contain_text("23:50")
    page.locator("#snapshots-kind .range-btn[data-kind='Y']").click()
    expect(body).to_contain_text("尚無年度成績")
