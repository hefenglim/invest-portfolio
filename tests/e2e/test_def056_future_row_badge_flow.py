"""E2E (DEF-056, owner ruling 2026-09-24): a ledger row dated after TODAY says so.

The valuation cut leaves such a row out of the holdings, 總報酬, XIRR and the cash pools until
its own date, so a row the dashboard does not reflect must explain itself on every ledger
list: 「未來日期：YYYY-MM-DD 起計入」. The date is decided by the SERVER (``counts_from`` on the
wire, cut on the app clock) and only RENDERED here — this drives the shipped pages in a real
browser against the real stack, so it sees what a contract test on the endpoint cannot: that
the badge is on the future row, on no other row, in each of the five ledger tabs and on the
cash page's two lists.

The flow server runs on the REAL clock, so the future rows are dated far ahead (2099).
"""

import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from playwright.sync_api import Page, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.data_ingestion.store import (
    insert_cash_movement,
    insert_dividend,
    insert_fx_conversion,
    insert_transaction,
    upsert_opening,
)
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.enums import Side
from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory

D = Decimal
_AHEAD = date(2099, 1, 2)
_BADGE = "未來日期：2099-01-02 起計入"


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _seed_future_rows(conn: sqlite3.Connection) -> None:
    _seed_golden(conn)
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=D("1000"), price=D("600"), fees=D("855"), tax=D("0"),
                       trade_date=_AHEAD)
    insert_dividend(conn, account_id="tw_broker", symbol="2330", div_date=_AHEAD,
                    div_type="CASH", gross=D("5000"), withholding=D("0"), net=D("5000"))
    insert_fx_conversion(conn, account_id="schwab", date=_AHEAD, from_ccy=Currency.TWD,
                         from_amount=D("33500"), to_ccy=Currency.USD, to_amount=D("1000"))
    upsert_opening(conn, account_id="schwab", symbol="AAPL", shares=D("5"),
                   original_cost_total=D("500"), build_date=_AHEAD)
    insert_cash_movement(conn, account_id="schwab", move_date=_AHEAD, kind="DEPOSIT",
                         ccy=Currency.USD, amount=D("100"))
    insert_cash_movement(conn, account_id="tw_broker", move_date=date(2026, 1, 2),
                         kind="DEPOSIT", ccy=Currency.TWD, amount=D("600000"))
    conn.commit()


def _errors(page: Page) -> list[str]:
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    def _console(m: Any) -> None:
        if getattr(m, "type", None) == "error":
            errors.append(getattr(m, "text", ""))

    page.on("console", _console)
    return errors


def _assert_badge_only_on_the_future_row(page: Page, tbody: str) -> None:
    rows = page.locator(f"{tbody} tr:not(.detail-row)")
    expect(rows.first).to_be_visible()
    future = rows.filter(has_text="2099-01-02")
    expect(future).to_have_count(1)
    expect(future.locator(".ledger-future")).to_have_text(_BADGE)
    # Exactly one badge in the whole table: the past rows carry none.
    expect(page.locator(f"{tbody} .ledger-future")).to_have_count(1)
    assert rows.count() >= 2, f"{tbody}: the fixture needs a past row beside the future one"


@pytest.mark.e2e
def test_every_ledger_tab_marks_the_row_dated_after_today(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_future_rows)
    page = fresh_page
    errors = _errors(page)
    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#tx-body tr.expandable")
    _assert_badge_only_on_the_future_row(page, "#tx-body")
    for tab, tbody in (("#tab-ldiv", "#div-body"), ("#tab-lfx", "#fx-body"),
                       ("#tab-lopen", "#open-body"), ("#tab-lcash", "#cash-body")):
        page.click(tab)
        page.wait_for_selector(f"{tbody} tr")
        if tbody == "#open-body":
            # The golden subset has no opening, so the future one is the table's only row.
            expect(page.locator(f"{tbody} .ledger-future")).to_have_text(_BADGE)
            continue
        _assert_badge_only_on_the_future_row(page, tbody)
    assert not errors, errors


@pytest.mark.e2e
def test_the_cash_page_marks_the_future_movement_and_conversion(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_future_rows)
    page = fresh_page
    errors = _errors(page)
    page.goto(base + "/cash.html#flows", wait_until="load")
    page.wait_for_selector("#cm-body tr")
    _assert_badge_only_on_the_future_row(page, "#cm-body")
    page.goto(base + "/cash.html#fx", wait_until="load")
    page.wait_for_selector("#cfx-ledger-body tr td.num")
    _assert_badge_only_on_the_future_row(page, "#cfx-ledger-body")
    assert not errors, errors
