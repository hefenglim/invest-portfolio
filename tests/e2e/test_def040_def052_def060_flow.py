"""E2E (Playwright, real server + real frontend) for the R4 corporate-action items the verifier
re-tests black-box in a browser:

* **DEF-052** — the 公司行動 ledger row shows its linked reorganisation fee (「重組費用 50 TWD」).
* **DEF-040 R4** — a SPINOFF whose child already has a price on the action day: the form says
  BEFORE saving that the typed 起始價 will not be written, the save's toast says it again, and
  the child's price is the provider's, untouched.
* **DEF-060** — editing a SPINOFF's date moves its seed, and the edit's toast says where to.

Each claim is about rendered state, which a payload assertion cannot see.
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
    insert_cash_movement,
    insert_corporate_action,
    upsert_instrument,
)
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from tests.conftest import GOLDEN_NOW
from tests.e2e.conftest import FlowServerFactory
from tests.e2e.test_band_and_spinoff_flow import (
    ACTION_DAY,
    _ack_checkbox,
    _date_box,
    _kind_button,
    _open_form_from_door3,
    _ratio_from,
    _ratio_to,
    _save_button,
    _seed_banded_position,
    _symbol_box,
    _to_symbol_box,
)

NEW_DAY = date(2026, 3, 20)


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


def _last(base_url: str, symbol: str) -> str | None:
    for row in _get_json(base_url, "/api/instruments")["list"]:
        if row["symbol"] == symbol:
            last: str | None = row["last"]
            return last
    raise AssertionError(f"{symbol} not in /api/instruments")


def _open_action_ledger(page: Page, base: str) -> None:
    page.goto(base + "/trades.html", wait_until="load")
    page.click("#tab-laction")
    page.wait_for_selector("#pane-laction.active")


# ============================================================================ DEF-052


def _seed_split_with_fee(conn: sqlite3.Connection) -> None:
    _seed_banded_position(conn)
    insert_cash_movement(conn, account_id="tw_broker", move_date=date(2026, 1, 1),
                         kind="DEPOSIT", ccy=Currency.TWD, amount=Decimal("1000000"))
    action = insert_corporate_action(
        conn, account_id="tw_broker", action_date=ACTION_DAY,
        kind=CorporateActionKind.SPLIT, from_symbol="2330", to_symbol="2330",
        ratio_to=Decimal("2"), ratio_from=Decimal("1"))
    insert_cash_movement(conn, account_id="tw_broker", move_date=ACTION_DAY,
                         kind="WITHDRAW", ccy=Currency.TWD, amount=Decimal("50"),
                         note="重組費用", corporate_action_id=action)
    conn.commit()


@pytest.mark.e2e
def test_def052_the_action_row_shows_its_reorganisation_fee(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_split_with_fee)
    page = fresh_page
    _open_action_ledger(page, base)
    row = page.locator("#action-body tr")
    expect(row).to_have_count(1)
    badge = row.locator(".ledger-reorg-fee")
    expect(badge).to_have_text("重組費用 50 TWD")


# ============================================================================ DEF-040 R4


def _seed_quoted_child(conn: sqlite3.Connection) -> None:
    _seed_banded_position(conn)
    upsert_instrument(conn, Instrument(symbol="SPINCO", market=Market.TW,
                                       quote_ccy=Currency.TWD, sector="Semiconductors",
                                       name="SpinCo", board="TWSE"))
    upsert_prices(conn, [PriceRow(instrument="SPINCO", market=Market.TW, as_of=ACTION_DAY,
                                  close=Decimal("120"), source="yfinance")],
                  fetched_at=GOLDEN_NOW)
    conn.commit()


@pytest.mark.e2e
def test_def040_a_quoted_child_day_is_announced_before_saving_and_never_overwritten(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_quoted_child)
    page = fresh_page
    modal = _open_form_from_door3(page, base)
    _kind_button(modal, 2).click()
    modal.locator(".ca-grid select").select_option("tw_broker")
    _symbol_box(modal).fill("2330")
    _date_box(modal).fill(ACTION_DAY.isoformat())
    _ratio_from(modal).fill("2")
    _ratio_to(modal).fill("1")
    modal.locator(".field").filter(has_text="成本分攤比例").locator("input").fill("0.3")
    modal.locator(".field").filter(has_text="子公司起始價").locator("input").fill("123.5")
    with page.expect_response("**/api/ledgers/corporate-actions/preview") as prev:
        _to_symbol_box(modal).fill("SPINCO")
    assert prev.value.status == 200, prev.value.text()

    notice = modal.locator(".ca-seed-skip")
    expect(notice).to_be_visible()
    expect(notice).to_contain_text("SPINCO 在 2026-03-16 已有正式報價 120（來源 yfinance）")
    expect(notice).to_contain_text("起始價未寫入")

    ack = _ack_checkbox(modal)
    if ack.count():
        ack.check()
    with page.expect_response("**/api/ledgers/corporate-actions") as saved:
        _save_button(modal).click()
    assert saved.value.status == 201, saved.value.text()
    assert saved.value.json()["child_priced"] is None
    expect(page.locator(".toast-warn").filter(has_text="子公司起始價未寫入")).to_contain_text(
        "已有正式報價 120")
    assert _last(base, "SPINCO") == "120"          # the provider's quote, untouched


# ============================================================================ DEF-060


def _post(base_url: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
    req = urllib.request.Request(base_url + path, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 (loopback)
        data: dict[str, Any] = json.loads(r.read().decode("utf-8"))
        return data


@pytest.mark.e2e
def test_def060_editing_the_date_moves_the_seed_and_the_toast_says_where(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_banded_position)
    saved = _post(base, "/api/ledgers/corporate-actions", {
        "account_id": "tw_broker", "date": ACTION_DAY.isoformat(), "kind": "SPINOFF",
        "from_symbol": "2330", "to_symbol": "SPINCO", "ratio_to": "1", "ratio_from": "2",
        "cost_carry": "0.3", "to_symbol_price": "123.5", "ack_warnings": True})
    assert saved["child_priced"] == "SPINCO", saved

    page = fresh_page
    _open_action_ledger(page, base)
    row = page.locator("#action-body tr")
    expect(row).to_have_count(1)
    row.locator("button", has_text="編輯").click()
    modal = page.locator(".modal")
    expect(modal).to_be_visible()
    modal.locator("input[type=date]").fill(NEW_DAY.isoformat())
    with page.expect_response("**/api/ledgers/corporate-actions/*") as put:
        modal.locator(".modal-foot .btn-primary").click()
    assert put.value.status == 200, put.value.text()
    expect(page.locator(".toast-ok").filter(has_text="子公司起始價已更新")).to_contain_text(
        "已由 SPINCO 在 2026-03-16 搬到 SPINCO 在 2026-03-20")
    listed = _get_json(base, "/api/ledgers/corporate-actions")["rows"][0]
    assert listed["child_price_restore"]["date"] == NEW_DAY.isoformat()


# ============================================================================ DEF-061


def _seed_tw_dividend(conn: sqlite3.Connection) -> None:
    from portfolio_dash.data_ingestion.store import insert_dividend

    _seed_banded_position(conn)
    insert_dividend(conn, account_id="tw_broker", symbol="2330", div_date=date(2026, 4, 1),
                    div_type="CASH", gross=Decimal("1000"), withholding=Decimal("0"),
                    net=Decimal("1000"))
    conn.commit()


@pytest.mark.e2e
def test_def061_the_dividend_edit_dialog_shows_the_entry_doors_findings_and_gates_save(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """A TW (cash_cost_reduction) row edited to DRIP: the entry door calls that a type the
    account's model does not book (soft) and a DRIP with no share count (hard). The dialog
    shows both live; 儲存 stays disabled until the warning is ticked; the PUT then refuses
    the hard one and the row is unchanged."""
    base = flow_server(_seed_tw_dividend)
    page = fresh_page
    page.goto(base + "/trades.html", wait_until="load")
    page.click("#tab-ldiv")
    row = page.locator("#div-body tr")
    expect(row).to_have_count(1)
    row.locator("button", has_text="編輯").click()
    modal = page.locator(".modal")
    expect(modal).to_be_visible()
    with page.expect_response("**/api/ledgers/dividends/preview") as prev:
        modal.locator("select").nth(1).select_option("drip")
    assert prev.value.status == 200, prev.value.text()
    box = modal.locator(".div-edit-issues")
    expect(box).to_contain_text("股利類型與該市場模型不符")
    expect(box).to_contain_text("DRIP 股利必須有股數")
    save = modal.locator(".modal-foot .btn-primary")
    expect(save).to_be_disabled()
    box.locator("input[type=checkbox]").check()
    expect(save).to_be_enabled()
    with page.expect_response("**/api/ledgers/dividends/*") as put:
        save.click()
    assert put.value.status == 400, put.value.text()
    rows = _get_json(base, "/api/ledgers/dividends")["rows"]
    assert rows[0]["type"] == "cash", rows[0]


# ============================================================================ DEF-056 addendum


def _seed_future_action(conn: sqlite3.Connection) -> None:
    """A SPLIT dated far ahead of the flow server's REAL clock, beside a past one."""
    _seed_banded_position(conn)
    for on in (ACTION_DAY, date(2099, 1, 2)):
        insert_corporate_action(
            conn, account_id="tw_broker", action_date=on,
            kind=CorporateActionKind.SPLIT, from_symbol="2330", to_symbol="2330",
            ratio_to=Decimal("2"), ratio_from=Decimal("1"))
    conn.commit()


@pytest.mark.e2e
def test_def056_the_action_tab_flags_its_future_row_and_only_it(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_future_action)
    page = fresh_page
    _open_action_ledger(page, base)
    rows = page.locator("#action-body tr")
    expect(rows).to_have_count(2)
    future = rows.filter(has_text="2099")
    expect(future.locator(".ledger-future")).to_have_text("未來日期：2099-01-02 起計入")
    expect(future).to_have_count(1)
    past = rows.filter(has_text="2026-03-16")
    expect(past).to_have_count(1)              # the filter matched — not a vacuous zero
    expect(past.locator(".ledger-future")).to_have_count(0)
