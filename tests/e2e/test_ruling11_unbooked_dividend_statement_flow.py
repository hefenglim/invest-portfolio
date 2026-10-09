"""E2E (⑪, owner ruling 2026-10-05): the 現金收支明細 lists a refused payout without moving
the balance — and SAYS so.

A dividend the replay refuses (here: 2317 paid into tw_broker, which never held it) counts in
no cash figure since ⑪. The statement still LISTS it — a payout must not vanish from the list
the owner reconciles against the broker's — so the page must show why the balance beside it did
not move: the server's 「未入帳（待釐清）」 beside the description, the amount struck through
(a class, never a style — DEF-077; asserted with getComputedStyle), the running balance equal
to the row below it, and a header that counts the row, as the printed report's title does. The
pool card above reads the same balance as the control ledger without the row.

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


def _card(page: Page, account_zh: str) -> Locator:
    return page.locator(".cash-card").filter(
        has=page.locator(".acct", has_text=account_zh))


@pytest.mark.e2e
def test_the_statement_marks_a_refused_payout_and_keeps_the_balance(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed)
    page = fresh_page
    errors = _errors(page)
    page.goto(base + "/cash.html", wait_until="load")
    line = _card(page, "台灣券商").locator(".cash-line").filter(
        has=page.locator(".ccy", has_text="TWD"))
    expect(line).to_have_count(1)
    # The golden pool (−500,000 buy + 5,000 booked dividend) — the 300 counts nowhere.
    card_balance = line.locator(".amt").inner_text()
    assert "495,000" in card_balance, card_balance
    line.click()
    page.wait_for_selector("#cash-stmt-body tr td.num")
    expect(page.locator("#cash-stmt-sub")).to_have_text(
        f"台灣券商・TWD　目前餘額 {card_balance} TWD（另有 1 筆股利未入帳，不計入餘額）")

    marked = page.locator("#cash-stmt-body tr.stmt-unbooked")
    expect(marked).to_have_count(1)
    expect(marked).to_be_visible()
    tag = marked.locator(".stmt-unbooked-tag")
    expect(tag).to_have_text(_NOTE)
    expect(tag).to_be_visible()
    assert tag.get_attribute("title"), "the reason rides on the badge"
    cells = marked.locator("td")
    expect(cells.nth(2)).to_contain_text("配息 鴻海（2317）")
    amount = cells.nth(3)
    expect(amount).to_have_text("+300")
    expect(amount).to_have_class("num stmt-unbooked-amt")
    decoration = amount.evaluate("el => getComputedStyle(el).textDecorationLine")
    assert "line-through" in decoration, decoration
    # The running balance is the row below's — the payout moved nothing.
    rows = page.locator("#cash-stmt-body tr")
    i = next(k for k in range(rows.count())
             if "stmt-unbooked" in (rows.nth(k).get_attribute("class") or ""))
    below = rows.nth(i + 1).locator("td").last.inner_text()
    assert cells.last.inner_text() == below, (cells.last.inner_text(), below)
    # Every other row is unmarked.
    expect(page.locator("#cash-stmt-body .stmt-unbooked-tag")).to_have_count(1)
    expect(page.locator("#cash-stmt-body .stmt-unbooked-amt")).to_have_count(1)
    assert not errors, errors
