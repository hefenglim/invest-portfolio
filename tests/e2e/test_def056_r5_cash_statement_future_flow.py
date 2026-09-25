"""E2E (DEF-056 R5, verifier bounce 2026-09-25): the cash page's 現金收支明細 flags future rows.

R4 cut every valuation by the row's own date and badged the ledger lists, the cash page's two
lists and the printed report — but the ON-SCREEN statement (帳戶現金 › 台灣券商) rendered its
rows from ``f.date(r.date)`` and never read the ``future`` flag the API already sent. Its
header said 「目前餘額 6,067,595 TWD」 while the first row, a broker fee dated next month, read
「餘額 6,067,495」 with nothing saying it was a projection.

This drives the shipped page in a real browser against the real stack. The expected values
come from the API the page renders (as_of, future_count, each row's counts_from), so the
test asserts what the SCREEN does with them: the printed report's cut line verbatim above the
first future row, a 「未來日期」 badge on every future row and on no other, each future balance
marked as a projection, and a header whose 目前餘額 is the pool card's balance (the first
row that has happened carries that same balance). Both the one-pool view and the account's
all-currency view.

The flow server runs on the REAL clock, so the future rows are dated far ahead (2099).
"""

import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from playwright.sync_api import Locator, Page, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.data_ingestion.store import (
    insert_cash_movement,
    insert_dividend,
    insert_transaction,
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


def _seed(conn: sqlite3.Connection) -> None:
    _seed_golden(conn)
    insert_cash_movement(conn, account_id="tw_broker", move_date=date(2026, 1, 2),
                         kind="DEPOSIT", ccy=Currency.TWD, amount=D("600000"))
    # The verifier's row: a 券商費用 dated after today …
    insert_cash_movement(conn, account_id="tw_broker", move_date=_AHEAD,
                         kind="BROKER_FEE", ccy=Currency.TWD, amount=D("100"))
    # … and a second kind in the same pool (a trade settles through the pool too).
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=D("10"), price=D("600"), fees=D("20"), tax=D("0"),
                       trade_date=_AHEAD)
    # The all-currency view: a future row in schwab's USD pool beside its TWD pool.
    insert_cash_movement(conn, account_id="schwab", move_date=_AHEAD, kind="DEPOSIT",
                         ccy=Currency.USD, amount=D("100"))
    conn.commit()


def _errors(page: Page) -> list[str]:
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    def _console(m: Any) -> None:
        if getattr(m, "type", None) == "error":
            errors.append(getattr(m, "text", ""))

    page.on("console", _console)
    return errors


def _statement(page: Page, base: str, account: str, ccy: str | None) -> dict[str, Any]:
    params: dict[str, str | float | bool] = {"account": account, "limit": "50", "offset": "0"}
    if ccy is not None:
        params["ccy"] = ccy
    resp = page.request.get(base + "/api/cash/statement", params=params)
    assert resp.status == 200
    body: dict[str, Any] = resp.json()
    return body


def _card(page: Page, account_zh: str) -> Locator:
    return page.locator(".cash-card").filter(
        has=page.locator(".acct", has_text=account_zh))


def _assert_statement_flags_future_rows(page: Page, body: dict[str, Any]) -> None:
    rows = page.locator("#cash-stmt-body tr")
    future = [r for r in body["rows"] if r["future"]]
    assert future, "the fixture needs at least one future row in this scope"
    assert body["future_count"] == len(future)
    # 1. the printed report's cut line, verbatim, directly above the first future row
    #    (newest-first puts the future rows at the top).
    cut = rows.nth(0)
    expect(cut).to_have_class("stmt-cut")
    expect(cut).to_have_text(
        f"以下 {len(future)} 筆為未來日期（{body['as_of']} 之後），不計入目前餘額；其餘額欄為投影")
    # 2. every future row carries its own server-decided badge; no other row carries one.
    badges = page.locator("#cash-stmt-body .ledger-future")
    expect(badges).to_have_count(len(future))
    for i, r in enumerate(future):
        row = rows.nth(1 + i)
        expect(row).to_have_class("stmt-future" if i < len(future) - 1
                                  else "stmt-future stmt-future-last")
        expect(row.locator(".ledger-future")).to_have_text(
            f"未來日期：{r['counts_from']} 起計入")
        # 3. its running balance is marked as a projection.
        expect(row.locator("td").last.locator(".stmt-proj-tag")).to_have_text("投影")
    expect(page.locator("#cash-stmt-body .stmt-proj-tag")).to_have_count(len(future))
    # The rows that have happened carry neither mark.
    past = rows.nth(1 + len(future))
    expect(past.locator(".ledger-future")).to_have_count(0)
    expect(past.locator(".stmt-proj-tag")).to_have_count(0)


@pytest.mark.e2e
def test_the_one_pool_statement_marks_future_rows_and_keeps_todays_balance(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed)
    page = fresh_page
    errors = _errors(page)
    page.goto(base + "/cash.html", wait_until="load")
    card = _card(page, "台灣券商")
    line = card.locator(".cash-line").filter(has=page.locator(".ccy", has_text="TWD"))
    expect(line).to_have_count(1)
    card_balance = line.locator(".amt").inner_text()
    line.click()
    page.wait_for_selector("#cash-stmt-body tr td.num")  # loaded; the marks are asserted below
    body = _statement(page, base, "tw_broker", "TWD")
    _assert_statement_flags_future_rows(page, body)
    # 4. the header's 目前餘額 is today's balance — the pool card's figure, NOT the projected
    #    balance of the top row — and says how many rows it leaves out, as the report does.
    future_n = body["future_count"]
    expect(page.locator("#cash-stmt-sub")).to_have_text(
        f"台灣券商・TWD　目前餘額 {card_balance} TWD"
        f"（截至 {body['as_of']}，不含 {future_n} 筆未來日期）")
    top_future_balance = page.locator("#cash-stmt-body tr.stmt-future").first.locator(
        "td").last.inner_text()
    assert card_balance not in top_future_balance, (card_balance, top_future_balance)
    # The first row that has happened carries today's balance (newest-first, end of day on top).
    first_past = page.locator("#cash-stmt-body tr").nth(1 + future_n)
    expect(first_past.locator("td").last).to_have_text(card_balance)
    assert not errors, errors


@pytest.mark.e2e
def test_the_all_currency_statement_marks_future_rows_too(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed)
    page = fresh_page
    errors = _errors(page)
    page.goto(base + "/cash.html", wait_until="load")
    _card(page, "嘉信").locator(".acct").click()
    page.wait_for_selector("#cash-stmt-body tr td.num")  # loaded; the marks are asserted below
    body = _statement(page, base, "schwab", None)
    assert {r["ccy"] for r in body["rows"]} >= {"USD", "TWD"}, "needs a two-pool account"
    _assert_statement_flags_future_rows(page, body)
    # The cut line spans the extra 幣別 column of the combined view.
    assert page.locator("#cash-stmt-body tr.stmt-cut td").get_attribute("colspan") == "6"
    expect(page.locator("#cash-stmt-sub")).to_contain_text(
        f"（截至 {body['as_of']}，不含 {body['future_count']} 筆未來日期）")
    assert not errors, errors


def _seed_future_dividend(conn: sqlite3.Connection) -> None:
    _seed_golden(conn)
    insert_dividend(conn, account_id="tw_broker", symbol="2330", div_date=_AHEAD,
                    div_type="CASH", gross=D("5000"), withholding=D("0"), net=D("5000"))
    conn.commit()


@pytest.mark.e2e
def test_the_drawer_dividend_history_marks_the_dividend_that_does_not_count_yet(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """Class scan (DEF-056 R5): the drawer's 配息史 lists EVERY ledger dividend — owner ③
    kept future rows out of the drawer's 交易明細, not out of this list — so a dividend paying
    in 2099 sat there beside the paid ones with nothing saying the position leaves it out."""
    base = flow_server(_seed_future_dividend)
    page = fresh_page
    errors = _errors(page)
    page.goto(base + "/index.html", wait_until="load")
    page.wait_for_selector(".kpi-card")
    with page.expect_response("**/api/symbol/2330/detail") as resp:
        page.evaluate("() => window.pdOpenSymbol('2330')")
    assert resp.value.status == 200
    page.wait_for_selector(".sd-drawer .sd-signals")
    history = page.locator(".sd-drawer .sd-section").filter(
        has=page.locator(".sd-sec-title", has_text="配息史"))
    rows = history.locator("tbody tr")
    expect(rows.filter(has_text="2099-01-02")).to_have_count(1)
    expect(rows.filter(has_text="2099-01-02").locator(".ledger-future")).to_have_text(_BADGE)
    expect(history.locator(".ledger-future")).to_have_count(1)
    assert rows.count() >= 2, "the golden 2330 dividend must be listed beside the future one"
    assert not errors, errors
