"""E2E (R3, repair K): the four ledger / draft rulings of 2026-09-24, driven in a real browser.

* **DEF-042** — the 交易帳本 「編輯」 modal runs the entry door's findings over "the ledger
  without this row + the edited row", and asks for the SAME per-warning acknowledgement: a
  date moved before the position's opening build date warns, and 儲存 waits for the tick.
* **DEF-013** — 當沖 + 放空 together is allowed: the ledger row shows both badges, the edit
  modal carries both flags, and the manual form explains what the pair means.
* **DEF-040** — deleting a SPINOFF says, in the confirm, that the child's seed price goes
  with it, and the toast reports that it did.
* **DEF-048** — a back-dated manual draft shows the position ON THE TRADE DATE, and says so.

Against the REAL stack (uvicorn + SQLite + the shipped static frontend): every one of these
is a page reading a server answer, which a contract test on the endpoint cannot see.
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

from portfolio_dash.data_ingestion.store import (
    insert_transaction,
    upsert_instrument,
    upsert_opening,
)
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory

D = Decimal
_PREVIEW = "**/api/input/manual/preview"


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


def _post_json(base_url: str, path: str, body: dict[str, Any]) -> tuple[int, Any]:
    req = urllib.request.Request(  # noqa: S310 (loopback)
        base_url + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 (loopback)
        return r.status, json.loads(r.read().decode("utf-8"))


def _sink(page: Page) -> tuple[list[str], list[str]]:
    console_errors: list[str] = []
    page_errors: list[str] = []

    def _console(m: Any) -> None:
        if getattr(m, "type", None) != "error":
            return
        text = getattr(m, "text", "")
        if "Failed to load resource" in text and ("400" in text or "422" in text):
            return
        console_errors.append(text)

    page.on("console", _console)
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    return console_errors, page_errors


# --- DEF-042 -----------------------------------------------------------------------------

def _seed_with_opening(conn: sqlite3.Connection) -> None:
    _seed_golden(conn)
    upsert_opening(conn, account_id="tw_broker", symbol="2330", shares=D("200"),
                   original_cost_total=D("90000"), build_date=date(2025, 12, 1))
    conn.commit()


@pytest.mark.e2e
def test_the_edit_modal_asks_for_the_entry_doors_acknowledgement(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_with_opening)
    page = fresh_page
    console_errors, page_errors = _sink(page)
    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#tx-body tr.expandable")
    row = page.locator("#tx-body tr.expandable").filter(has_text="2330")
    with page.expect_response(_PREVIEW) as opened:
        row.locator(".wl-actions .btn").first.click()
    body = opened.value.request.post_data_json or {}
    assert body.get("replaces_txn_id") is not None, body   # the EDIT question, not the entry one
    save = page.locator(".modal-foot .btn-primary")
    expect(save).to_be_enabled()
    # The preview on OPEN must not write the engine's fee over the stored one (the golden
    # row's fee is 0; the engine's for 1,000 x 500 is not) — a supplied fee is money that left.
    assert page.locator(".modal-body .field:nth-child(8) input").input_value() == "0"

    with page.expect_response(_PREVIEW):
        page.fill(".modal-body .field:nth-child(1) input", "2025-11-03")
    warn = page.locator(".modal-body .issue-warn").filter(has_text="期初庫存建檔日")
    expect(warn).to_have_count(1)
    # A core field moved, so the page's own figure IS replaced (audit M6) — through
    # pdField.autoFill (DEF-007), which lets it because the page put the stored 0 there.
    expect(page.locator(".modal-body .field:nth-child(8) input")).not_to_have_value("0")
    expect(save).to_be_disabled()                      # the entry door's tick, per warning
    warn.locator("input[type=checkbox]").check()
    expect(save).to_be_enabled()

    with page.expect_response("**/api/ledgers/transactions/**") as saved:
        save.click()
    assert saved.value.status == 200, saved.value.text()
    assert "trade_before_opening" in [i["code"] for i in saved.value.json()["issues"]]
    page.wait_for_selector(".toast-ok:has-text('編輯完成')")
    assert not console_errors and not page_errors, (console_errors, page_errors)


# --- DEF-013 -----------------------------------------------------------------------------

def _seed_daytrade_short(conn: sqlite3.Connection) -> None:
    _seed_golden(conn)
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.SELL,
                       quantity=D("1500"), price=D("600"), fees=D("1282"), tax=D("1350"),
                       trade_date=date(2026, 6, 10), daytrade=True, short_sale=True)
    conn.commit()


@pytest.mark.e2e
def test_daytrade_and_short_are_visible_on_the_row_the_modal_and_the_form(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_daytrade_short)
    page = fresh_page
    console_errors, page_errors = _sink(page)
    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#tx-body tr.expandable")
    row = page.locator("#tx-body tr.expandable").filter(has_text="1,500")
    expect(row.locator(".tx-flag-daytrade")).to_have_text("當沖")
    expect(row.locator(".tx-flag-short")).to_have_text("放空")
    # the golden buy carries neither
    plain = page.locator("#tx-body tr.expandable").filter(has_text="1,000")
    expect(plain.locator(".tx-flag-daytrade, .tx-flag-short")).to_have_count(0)

    with page.expect_response(_PREVIEW):
        row.locator(".wl-actions .btn").first.click()
    expect(page.locator("#edit-daytrade")).to_be_checked()
    expect(page.locator("#edit-short")).to_be_checked()
    expect(page.locator(".edit-combo-note")).to_be_visible()
    expect(page.locator(".edit-combo-note")).to_contain_text("0.15%")
    # FU-D7's 還原自動 still works through pdField.autoFill (DEF-007): a typed fee is the
    # owner's until they hand it back, then the engine's figure replaces it.
    fee = page.locator(".modal-body .field:nth-child(8) input")
    fee.fill("999")
    with page.expect_response(_PREVIEW):
        page.locator(".modal-body .field:nth-child(8) .edit-revert").click()
    expect(fee).not_to_have_value("999")
    page.locator(".modal-close").click()

    # the manual form: the note appears only with BOTH ticked, on a sell
    page.click("#tab-manual")
    page.click("#m-side-sell")
    page.check("#m-daytrade")
    expect(page.locator("#m-combo-note")).to_be_hidden()
    page.check("#m-short")
    expect(page.locator("#m-combo-note")).to_be_visible()
    expect(page.locator("#m-combo-note")).to_contain_text("當沖優先於 ETF")
    page.click("#m-side-buy")
    expect(page.locator("#m-combo-note")).to_be_hidden()
    assert not console_errors and not page_errors, (console_errors, page_errors)


# --- DEF-040 -----------------------------------------------------------------------------

def _seed_parent(conn: sqlite3.Connection) -> None:
    _seed_golden(conn)
    for symbol, name in (("PARN", "Parent"), ("CHLD", "Child")):
        upsert_instrument(conn, Instrument(symbol=symbol, market=Market.US,
                                           quote_ccy=Currency.USD, sector="Tech", name=name))
    insert_transaction(conn, account_id="schwab", symbol="PARN", side=Side.BUY,
                       quantity=D("100"), price=D("100"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 1, 12))
    conn.commit()


@pytest.mark.e2e
def test_deleting_a_spinoff_says_it_takes_the_seed_price_and_does(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_parent)
    status, _ = _post_json(base, "/api/ledgers/corporate-actions", {
        "account_id": "schwab", "date": "2026-03-16", "kind": "SPINOFF",
        "from_symbol": "PARN", "to_symbol": "CHLD", "ratio_to": "1", "ratio_from": "2",
        "cost_carry": "0.2", "to_symbol_price": "50", "ack_warnings": True})
    assert status == 201
    page = fresh_page
    console_errors, page_errors = _sink(page)
    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#m-account option", state="attached")
    page.click("#tab-laction")
    page.wait_for_selector("#action-body tr")
    page.locator("#action-body tr").first.locator(".btn-row-del").click()
    confirm = page.locator(".modal").filter(has_text="刪除公司行動")
    expect(confirm).to_contain_text("子公司起始價")
    expect(confirm).to_contain_text("會一併移除")
    with page.expect_response("**/api/ledgers/corporate-actions/**") as deleted:
        page.click(".modal-foot .btn-danger")
    assert deleted.value.json()["child_price_removed"] is True
    page.wait_for_selector(".toast-ok:has-text('已移除子公司起始價')")
    assert _get_json(base, "/api/ledgers/corporate-actions")["rows"] == []
    assert not console_errors and not page_errors, (console_errors, page_errors)


# --- DEF-048 -----------------------------------------------------------------------------

@pytest.mark.e2e
def test_a_backdated_draft_shows_the_trade_date_position_and_says_so(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_golden)
    page = fresh_page
    console_errors, page_errors = _sink(page)
    page.goto(base + "/input.html", wait_until="load")
    page.wait_for_selector("#m-account option", state="attached")
    page.select_option("#m-account", "tw_broker")
    page.click("#m-side-buy")
    page.fill("#m-symbol", "2330")
    page.fill("#m-date", "2025-01-02")
    page.fill("#m-shares", "100")
    with page.expect_response(_PREVIEW) as pv:
        page.fill("#m-price", "30")
    pp = pv.value.json()["position_preview"]
    assert pp["old_shares"] is None and pp["new_shares"] == "100", pp
    rows = page.locator("#m-pc-rows")
    expect(rows.locator(".pc-asof")).to_contain_text("以交易日 2025-01-02 當時的部位試算")
    held = rows.locator(".pc-row").filter(has_text="持股")
    expect(held.locator(".pc-old")).to_have_text("—")
    expect(held.locator(".pc-new")).to_have_text("100")
    # The sell-side fill hints read TODAY's holding (no date on their endpoint): on a
    # back-dated draft they say so, instead of posing as what the typed date could sell.
    page.click("#m-side-sell")
    expect(page.locator("#m-shares-hint button")).to_have_text("今日可賣 1,000 股")
    page.fill("#m-date", "")
    page.fill("#m-date", page.evaluate("() => document.querySelector('#m-date').max"))
    expect(page.locator("#m-shares-hint button")).to_have_text("可賣 1,000 股")
    assert not console_errors and not page_errors, (console_errors, page_errors)
