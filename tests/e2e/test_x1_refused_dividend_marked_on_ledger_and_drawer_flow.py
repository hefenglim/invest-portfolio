"""E2E (X1, owner ruling 2026-10-05): the 交易帳本 股利 tab and the drawer's 配息史 mark a
dividend the replay refused — 「標示『未入帳（待釐清）』並排除合計」.

A payout into tw_broker on 2317, which that account never held, is in the ledger, so both
tables list it; but it is in no balance, return or total, so the row says so: the server's
「未入帳（待釐清）」 badge with the replay's own sentence as its hover, the net struck through
(a class, asserted with getComputedStyle — DEF-077), the row dimmed like an unapplied corporate
action. Every other row stays unmarked.

The flow server runs on the real clock; every row here is dated in the past.
"""

import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from playwright.sync_api import Locator, Page, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.data_ingestion.store import insert_dividend, upsert_instrument
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory

D = Decimal
_NOTE = "未入帳（待釐清）"


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _seed(conn: sqlite3.Connection) -> None:
    _seed_golden(conn)
    upsert_instrument(conn, Instrument(symbol="2317", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Electronics", name="鴻海", board="TWSE"))
    insert_dividend(conn, account_id="tw_broker", symbol="2317", div_date=date(2026, 3, 2),
                    div_type="CASH", gross=D("300"), withholding=D("0"), net=D("300"))
    conn.commit()


def _errors(page: Page) -> list[str]:
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    def _console(m: Any) -> None:
        if getattr(m, "type", None) == "error":
            errors.append(getattr(m, "text", ""))

    page.on("console", _console)
    return errors


def _reason(page: Page, base: str) -> str:
    resp = page.request.get(base + "/api/ledgers/dividends", params={"symbol": "2317"})
    assert resp.status == 200
    (row,) = resp.json()["rows"]
    assert row["booked"] is False
    reason: str = row["unbooked_reason"]
    assert reason
    return reason


def _assert_marked(row: Locator, net_cell: Locator) -> None:
    expect(row).to_be_visible()
    expect(row).to_have_class("row-stale ledger-unbooked-row")
    badge = row.locator(".ledger-unbooked")
    expect(badge).to_have_text(_NOTE)
    expect(badge).to_be_visible()
    expect(net_cell).to_have_text("300")
    decoration = net_cell.evaluate("el => getComputedStyle(el).textDecorationLine")
    assert "line-through" in decoration, decoration


@pytest.mark.e2e
def test_the_dividend_ledger_tab_marks_the_refused_row(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed)
    page = fresh_page
    errors = _errors(page)
    page.goto(base + "/trades.html", wait_until="load")
    page.click("#tab-ldiv")
    rows = page.locator("#div-body tr")
    expect(rows).to_have_count(2)            # the golden 2330 payout + the refused one
    marked = page.locator("#div-body tr", has_text="2317")
    # 日期 · 帳戶 · 代號 · 類型 · Gross · 預扣 · Net · 再投資 · actions
    _assert_marked(marked, marked.locator("td").nth(6))
    reason = _reason(page, base)
    # Hovering the badge or the row reads the replay's own sentence (account tokens resolved).
    assert marked.locator(".ledger-unbooked").get_attribute("title") == reason.replace(
        "{account:tw_broker}", "台灣券商")
    booked = page.locator("#div-body tr", has_text="2330")
    expect(booked.locator(".ledger-unbooked")).to_have_count(0)
    expect(booked).not_to_have_class("row-stale ledger-unbooked-row")
    assert not errors, errors


@pytest.mark.e2e
def test_the_drawer_dividend_history_marks_the_refused_row(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed)
    page = fresh_page
    errors = _errors(page)
    page.goto(base + "/index.html", wait_until="load")
    page.wait_for_selector(".kpi-card")
    with page.expect_response("**/api/symbol/2317/detail") as resp:
        page.evaluate("() => window.pdOpenSymbol('2317')")
    assert resp.value.status == 200
    history = page.locator(".sd-drawer .sd-section").filter(
        has=page.locator(".sd-sec-title", has_text="配息史"))
    row = history.locator("tbody tr")
    expect(row).to_have_count(1)
    # 日期 · 類型 · Gross · Net · 再投資
    _assert_marked(row, row.locator("td").nth(3))
    assert not errors, errors
