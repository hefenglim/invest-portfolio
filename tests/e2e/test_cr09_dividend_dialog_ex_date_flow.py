"""E2E (Playwright, real server + real frontend) — CR-09: the dividend edit dialog owns the
ex-date.

The dialog could neither show nor change ``ex_date`` while the PUT carried the stored value
through every edit, so a row re-keyed to another symbol kept an ex-date that belonged to
somebody else's distribution — and a 配股 counts from it. In a browser: the field is there,
prefilled; moving the row to another symbol raises 「除息日…是原本 2330 那筆股利的日期」 and
儲存 waits for its tick; clearing the field answers the question; the save writes the
cleared value.
"""

import json
import sqlite3
import urllib.request
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from playwright.sync_api import Page, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_dividend,
    insert_transaction,
    upsert_instrument,
)
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.e2e.conftest import FlowServerFactory


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _get_json(base_url: str, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(base_url + path, timeout=10) as r:  # noqa: S310 (loopback)
        data: dict[str, Any] = json.loads(r.read().decode("utf-8"))
        return data


def _seed(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    for sym, name in (("2330", "TSMC"), ("2317", "鴻海")):
        upsert_instrument(conn, Instrument(symbol=sym, market=Market.TW, quote_ccy=Currency.TWD,
                                           sector="Semiconductors", name=name, board="TWSE"))
        insert_transaction(conn, account_id="tw_broker", symbol=sym, side=Side.BUY,
                           quantity=Decimal("1000"), price=Decimal("100"), fees=Decimal("0"),
                           tax=Decimal("0"), trade_date=date(2026, 1, 5))
    insert_dividend(conn, account_id="tw_broker", symbol="2330", div_date=date(2026, 4, 5),
                    div_type="CASH", gross=Decimal("2500"), withholding=Decimal("0"),
                    net=Decimal("2500"), ex_date=date(2026, 3, 10))
    conn.commit()


@pytest.mark.e2e
def test_cr09_the_dialog_shows_the_ex_date_and_asks_when_a_rekey_carries_it(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed)
    page = fresh_page
    page.goto(base + "/trades.html", wait_until="load")
    page.click("#tab-ldiv")
    row = page.locator("#div-body tr")
    expect(row).to_have_count(1)
    with page.expect_response("**/api/ledgers/dividends/preview"):
        row.locator("button", has_text="編輯").click()
    modal = page.locator(".modal", has_text="編輯股利")
    expect(modal).to_be_visible()
    ex_box = modal.locator(".field", has_text="除息日").locator("input[type=date]")
    expect(ex_box).to_have_value("2026-03-10")             # shown, prefilled
    save = modal.locator(".modal-foot .btn-primary")
    expect(save).to_be_enabled()                            # nothing to ask on open

    sym = modal.locator(".field", has_text="代號").locator("input")
    with page.expect_response("**/api/ledgers/dividends/preview") as prev:
        sym.fill("2317")
    assert prev.value.status == 200, prev.value.text()
    box = modal.locator(".div-edit-issues")
    expect(box).to_contain_text("除息日 2026-03-10 是原本 2330 那筆股利的日期")
    expect(save).to_be_disabled()                           # 儲存 waits for the tick

    with page.expect_response("**/api/ledgers/dividends/preview") as prev2:
        ex_box.fill("")
        ex_box.dispatch_event("change")
    assert prev2.value.status == 200, prev2.value.text()
    expect(box).not_to_contain_text("除息日 2026-03-10")      # clearing answers it
    expect(save).to_be_enabled()

    with page.expect_response("**/api/ledgers/dividends/*") as put:
        save.click()
    assert put.value.status == 200, put.value.text()
    stored = _get_json(base, "/api/ledgers/dividends")["rows"][0]
    assert (stored["symbol"], stored["ex_date"]) == ("2317", None)
