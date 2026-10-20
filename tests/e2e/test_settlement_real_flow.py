"""PERF spec 2026-10-18 §14.1–§14.4 on the REAL backend (plan §6.4 step 4): the chip, the
prompt, the menu's 重新結算, the live panel, a finished run's report, cancel and a reload —
against E's service and routes with runs in real child processes (``flow_server(settle=True)``,
golden ledger, the real clock), nothing stubbed but one sleep.

Each server boots with an empty store, so its boot run settles the golden ledger first; the
flows wait for 「已結算」 before they change anything. A run that must still be running when
the panel is read gets ``tests/settlement/worker_stubs.py``'s ``slow_valuate`` (the real worker,
one sleep before its valuation, only while a marker file exists — so the boot run is never
slowed). Changes are made the way the owner makes them — the page's own ``pdApi`` (so
``pd-mutated`` fires) — except a price, which no page edits: it is written to the database
file, as a quote job would.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import Page, expect

from portfolio_dash.data_ingestion.store import insert_transaction
from portfolio_dash.shared.models.enums import Side
from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory

_TPE = ZoneInfo("Asia/Taipei")

_SETTLE_TIMEOUT = 120_000   # a child run on this machine: spawn + imports + ~130 days


def _seed_and_remember(where: list[str]) -> Callable[[sqlite3.Connection], None]:
    """The golden seed, remembering the database file (a price is written to it later)."""
    def seed(conn: sqlite3.Connection) -> None:
        _seed_golden(conn)
        where.append(str(conn.execute("PRAGMA database_list").fetchone()[2]))
    return seed


def _chip(page: Page) -> Any:
    return page.locator("#settle-chip")


def _settled(page: Page) -> None:
    """Wait for the chip to read 已結算 (the boot run, or the run just started, finished) —
    and STAY clean: a run that ends can chain an automatic follow-up (changes the boot itself
    committed), and a change made in the instant between the two is claimed by the follow-up
    (system-handled: no prompt), which is not the state these flows start from."""
    page.wait_for_function(
        "() => { const c = document.querySelector('#settle-chip');"
        " return c && !c.hidden && c.classList.contains('st-chip-clean'); }",
        timeout=_SETTLE_TIMEOUT)
    page.wait_for_function(
        """async () => {
             const a = await window.pdSettlement.refresh();
             await new Promise((r) => setTimeout(r, 1500));
             const b = await window.pdSettlement.refresh();
             return a && b && a.state === 'clean' && b.state === 'clean'
               && !a.run && !b.run && a.generation === b.generation; }""",
        timeout=_SETTLE_TIMEOUT, polling=500)


def _status(page: Page) -> Any:
    return page.evaluate("() => window.pdSettlement.refresh()")


def _deposit(page: Page, day: str, amount: str = "1000") -> None:
    """A back-dated 入金 through the page's own fetch layer (pd-mutated fires)."""
    page.evaluate(
        "(b) => window.pdApi.post('/api/cash/movements', b)",
        {"account_id": "tw_broker", "date": day, "kind": "deposit", "ccy": "TWD",
         "amount": amount})


def _prompt(page: Page) -> Any:
    """The re-settle prompt, once it is up — with the status in the message when it is not."""
    prompt = page.locator(".modal.st-prompt")
    try:
        prompt.wait_for(state="visible", timeout=30_000)
    except Exception as exc:   # noqa: BLE001 (re-raised with the evidence)
        raise AssertionError(f"no prompt; status = {_status(page)!r}") from exc
    return prompt


def _open(page: Page, base: str, path: str = "/instruments.html") -> None:
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(base + path, wait_until="load")
    expect(_chip(page)).to_be_visible(timeout=_SETTLE_TIMEOUT)


def _posts(page: Page) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    page.on("request", lambda r: out.append((r.url, r.post_data or ""))
            if r.method == "POST" and "/api/settlement/" in r.url else None)
    return out


@pytest.mark.e2e
def test_the_chip_and_the_prompt_follow_the_real_store(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """pending → running → clean as the store produces them; no prompt for a change inside
    the live window, the prompt for an old one — asked once per seq; a second change on the
    same day after a settle is asked about again (review F #1)."""
    base = flow_server(_seed_golden, settle=True)
    page = fresh_page
    posts = _posts(page)
    _open(page, base)
    _settled(page)
    seq0 = _status(page)["seq"]

    # a change dated yesterday is already reflected (the live window): never a prompt
    _deposit(page, (datetime.now(_TPE).date() - timedelta(days=1)).isoformat())
    page.wait_for_timeout(2500)              # the 800 ms debounce and its status read
    assert page.locator(".modal.st-prompt").count() == 0
    st = _status(page)
    assert st["needs_prompt"] is False and st["seq"] > seq0, st

    _deposit(page, "2026-06-05")
    prompt = _prompt(page)
    expect(prompt).to_contain_text("要重新結算歷史資料嗎？")
    expect(prompt).to_contain_text("這筆變更影響 2026-06-05 起的歷史結算")
    expect(prompt).to_contain_text("待結算：現金收支 2 筆")
    expect(_chip(page)).to_have_class(re.compile(r"\bst-chip-pending\b"))
    expect(_chip(page)).to_contain_text("待結算 · 2026-06-05 起")
    prompt.get_by_role("button", name="立即重新結算").click()
    page.wait_for_function(
        "() => document.querySelector('#settle-chip').classList.contains('st-chip-run')",
        timeout=_SETTLE_TIMEOUT)
    _settled(page)
    assert sum(1 for u, b in posts if u.endswith("/api/settlement/run")
               and '"dirty"' in b) == 1
    st = _status(page)
    assert st["state"] == "clean" and st["seq"] > seq0

    # the SAME day and the same amount again, after the settle: a new seq, asked again
    _deposit(page, "2026-06-05")
    prompt = _prompt(page)
    expect(prompt).to_contain_text("2026-06-05 起")
    prompt.get_by_role("button", name="稍後").click()
    expect(_chip(page)).to_contain_text("待結算 · 2026-06-05 起")
    page.wait_for_timeout(500)
    assert sum(1 for u, _b in posts if u.endswith("/api/settlement/run")) == 1, "稍後 runs nothing"
    # … and this browser is not asked again about the same seq after a reload
    page.reload(wait_until="load")
    expect(_chip(page)).to_contain_text("待結算")
    page.wait_for_timeout(1500)
    assert page.locator(".modal.st-prompt").count() == 0


@pytest.mark.e2e
def test_the_menu_full_run_and_its_report(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """The menu's 重新結算（驗證並重建歷史） starts a full run; its report has the steps, the
    change trail with the ledger record AND the price counter (review F #2), and the restated
    closes with their zh reasons — never a raw source. A reload keeps the report."""
    db: list[str] = []
    base = flow_server(_seed_and_remember(db), settle=True)
    page = fresh_page
    posts = _posts(page)
    _open(page, base, "/index.html")     # the one page with the 重新整理 menu
    _settled(page)
    _deposit(page, "2026-06-05")
    _prompt(page).get_by_role("button", name="稍後").click()
    conn = sqlite3.connect(db[0], timeout=10)
    try:   # a quote job re-states a close: the triggers record a price counter
        conn.execute("UPDATE prices SET close = '612.5', close_raw = '612.5' "
                     "WHERE instrument = '2330' AND as_of_date = '2026-06-09'")
        conn.commit()
    finally:
        conn.close()

    page.locator(".refresh-wrap .btn-refresh").click()
    page.get_by_role("button", name=re.compile("重新結算（驗證並重建歷史）")).click()
    expect(page.locator(".toast")).to_contain_text("重新結算已開始（驗證並重建歷史）")
    assert any(u.endswith("/api/settlement/run") and '"full"' in b for u, b in posts)
    _settled(page)

    for _ in range(2):        # …and the same report after a reload
        _chip(page).click()
        card = page.locator(".st-card").first
        expect(card.locator(".st-card-title")).to_contain_text("最近一次結算 #")
        expect(card).to_contain_text("啟動（重新結算：驗證並重建歷史）")
        expect(card).to_contain_text("你 於 ")
        expect(card).to_contain_text("現金收支 1 筆，2026-06-05")
        expect(card).to_contain_text("價格 1 檔 1 筆，2026-06-09")
        trail = card.locator(".st-sec[data-sec='rep-changes'] .st-table")
        expect(trail).to_contain_text("現金收支")
        expect(trail).to_contain_text("新增")
        expect(trail.locator("tr.st-row-lead")).to_contain_text("2330")
        expect(trail.locator("tr.st-row-lead")).to_contain_text("1 筆")
        assert card.locator(".st-sec[data-sec='rep-steps'] .st-step-done").count() >= 7
        closes = card.locator(".st-sec[data-sec='rep-closes']")
        expect(closes).to_contain_text("→")
        expect(closes).to_contain_text("更正後重新結算")
        text = closes.inner_text()
        assert not re.search(r"\b(transactions|cash_movements|prices):\d+|portfolio_snapshots",
                             text), text
        expect(card).not_to_contain_text("{account:")
        page.reload(wait_until="load")
        _settled(page)


@pytest.mark.e2e
def test_the_live_panel_during_a_run_its_queued_change_and_cancel(
    flow_server: FlowServerFactory, fresh_page: Page, monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The live sections while a real child runs (held by one sleep), the change made during
    it as 「完成後接著處理」 (the status's `queued`, review F #3), 1 s polling, and 取消（保留目前
    版本） ending the run as cancelled with the server's consequence."""
    marker = tmp_path / "slow-once"
    monkeypatch.setenv("PD_TEST_MODE", "1")
    monkeypatch.setenv("PD_TEST_STUBS", "tests.settlement.worker_stubs")
    monkeypatch.setenv("PD_STUB_WORKER", "slow_valuate")
    monkeypatch.setenv("PD_STUB_ONCE", str(marker))
    monkeypatch.setenv("PD_STUB_SLEEP_S", "60")
    base = flow_server(_seed_golden, settle=True)
    page = fresh_page
    posts = _posts(page)
    statuses: list[str] = []
    page.on("request", lambda r: statuses.append(r.url)
            if "/api/settlement/status" in r.url else None)
    _open(page, base)
    _settled(page)
    marker.write_text("1", encoding="utf-8")       # the NEXT run sleeps before its valuation

    _deposit(page, "2026-06-05")
    _prompt(page).get_by_role("button", name="立即重新結算").click()
    page.wait_for_function(
        "() => document.querySelector('#settle-chip').classList.contains('st-chip-run')",
        timeout=_SETTLE_TIMEOUT)
    _chip(page).click()
    body = page.locator(".st-drawer .st-body")
    expect(body.locator(".st-live-title")).to_contain_text("重新結算：2026-06-05 至 ",
                                                           timeout=_SETTLE_TIMEOUT)
    expect(body).to_contain_text("你 於 ")
    expect(body).to_contain_text("啟動（確認提示：現金收支變更）")
    expect(body).to_contain_text("現金收支 1 筆，2026-06-05")
    expect(body.locator(".st-table").first).to_contain_text("新增")
    expect(body).to_contain_text("第 1 版 → 第 2 版（切換後生效）")
    expect(page.locator(".st-step-done .st-step-mark").first).to_have_text("✓",
                                                                          timeout=_SETTLE_TIMEOUT)
    expect(body).to_contain_text("取消的結果：")
    before = len(statuses)
    page.wait_for_timeout(2600)
    assert len(statuses) - before >= 2, "about one status read a second while the panel is open"

    _deposit(page, "2026-06-06", "2000")            # committed while the run computes
    expect(body).to_contain_text("完成後接著處理：現金收支 1 筆，2026-06-06",
                                 timeout=_SETTLE_TIMEOUT)

    page.get_by_role("button", name="取消（保留目前版本）").click()
    modal = page.locator(".modal")
    expect(modal).to_contain_text("取消重新結算？")
    modal.get_by_role("button", name="取消重新結算").click()
    expect(body.locator(".st-card").first).to_contain_text("已取消", timeout=_SETTLE_TIMEOUT)
    fail = body.locator(".st-sec.st-sec-fail").first
    expect(fail).to_contain_text("結果")
    assert any(u.endswith("/api/settlement/cancel") for u, _b in posts)
    expect(_chip(page)).to_have_class(re.compile(r"\bst-chip-pending\b"))


def _seed_with_an_oversell(conn: sqlite3.Connection) -> None:
    """Golden, plus a sale of more 2330 than the account holds, written past the doors' guard
    (as an import of an incomplete statement would): the tolerant replay keeps it 待釐清."""
    _seed_golden(conn)
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.SELL,
                       quantity=Decimal("50000"), price=Decimal("600"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 6, 8))


@pytest.mark.e2e
def test_a_rebuild_with_findings_warns_and_lists_them_and_stops_nothing(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """§14.4: 重新結算（驗證並重建歷史） lists every 待釐清 record of the tolerant replay — a
    warning toast for the run this page started and the panel's 帳本檢查 lines — and the run
    still publishes."""
    base = flow_server(_seed_with_an_oversell, settle=True)
    page = fresh_page
    _open(page, base, "/index.html")
    _settled(page)
    gen = _status(page)["generation"]
    page.locator(".refresh-wrap .btn-refresh").click()
    page.get_by_role("button", name=re.compile("重新結算（驗證並重建歷史）")).click()
    warn = page.locator(".toast.toast-warn").filter(has_text="待釐清")
    expect(warn).to_contain_text("重新結算：發現", timeout=_SETTLE_TIMEOUT)
    expect(warn).to_contain_text("2330")
    _settled(page)
    assert _status(page)["generation"] > gen, "a finding stops nothing: the run published"
    _chip(page).click()
    checks = page.locator(".st-card").first.locator(".st-sec[data-sec='rep-validation']")
    expect(checks.locator(".st-findings li").first).to_contain_text("2330")
    expect(checks).to_contain_text("待釐清")
    expect(checks).not_to_contain_text("{account:")
