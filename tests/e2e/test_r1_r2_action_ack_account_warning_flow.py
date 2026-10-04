"""E2E (Playwright, real server + real frontend) — R1 and R2 of the final CR-07 review.

**R1.** A corporate-action warning that NAMES AN ACCOUNT could never be acknowledged. The
server's sentence carries ``{account:tw_broker}``; ``web/api.js`` resolves it to 「台灣券商」 in
every response; CR-07 compared the text the browser sent back against the server's, so the two
never matched and every save answered ``warnings_changed`` — the owner looped forever, on the
create form and on the ledger edit dialog. The binding is now the server's own opaque
``ack_key`` per issue, which the resolver cannot touch. CR-07's own e2e used D44's sentence,
which names no account, so it could not see this; the warning here is D3's (an opening built
ON the action date): 「台灣券商 在 2026-03-16 有一筆 2330 的期初庫存…」.

**R2.** In the ledger edit dialog the re-send after 「我了解，仍要儲存」 carried an error handler
that knew only the replay's ACK_CODES, so a ``warnings_changed`` on the re-send was merely
toasted. It now reopens the warnings dialog with the new list. Driven for real: the opening's
share count (which D3's sentence quotes) is changed while the dialog is open.
"""

import json
import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from playwright.sync_api import Locator, Page, Request, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_corporate_action,
    upsert_instrument,
    upsert_opening,
)
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from tests.conftest import GOLDEN_NOW
from tests.e2e.conftest import FlowServerFactory
from tests.e2e.test_band_and_spinoff_flow import (
    _ack_checkbox,
    _date_box,
    _kind_button,
    _open_form_from_door3,
    _ratio_from,
    _ratio_to,
    _save_button,
    _symbol_box,
)

ACTION_DAY = date(2026, 3, 16)
_D3 = "台灣券商 在 2026-03-16 有一筆 2330 的期初庫存"


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _seed(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Semiconductors", name="TSMC", board="TWSE"))
    upsert_opening(conn, account_id="tw_broker", symbol="2330", shares=Decimal("1000"),
                   original_cost_total=Decimal("500000"), build_date=ACTION_DAY)
    upsert_prices(conn, [PriceRow(instrument="2330", market=Market.TW, as_of=date(2026, 6, 9),
                                  close=Decimal("600"), source="test")],
                  fetched_at=GOLDEN_NOW)
    conn.commit()


def _seed_with_split(conn: sqlite3.Connection) -> None:
    _seed(conn)
    insert_corporate_action(conn, account_id="tw_broker", action_date=ACTION_DAY,
                            kind=CorporateActionKind.SPLIT, from_symbol="2330",
                            to_symbol="2330", ratio_to=Decimal("2"), ratio_from=Decimal("1"))
    conn.commit()


def _is_action_put(r: Any) -> bool:
    req = r if isinstance(r, Request) else r.request
    return req.method == "PUT" and "/api/ledgers/corporate-actions/" in req.url


def _rows(page: Page) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = page.evaluate(
        "() => window.pdApi.get('/api/ledgers/corporate-actions', {limit: 500})")["rows"]
    return rows


@pytest.mark.e2e
def test_r1_the_create_form_saves_an_acknowledged_account_naming_warning(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed)
    page = fresh_page
    modal = _open_form_from_door3(page, base)
    _kind_button(modal, 0).click()
    modal.locator(".ca-grid select").select_option("tw_broker")
    _symbol_box(modal).fill("2330")
    _date_box(modal).fill(ACTION_DAY.isoformat())
    _ratio_from(modal).fill("1")
    with page.expect_response("**/api/ledgers/corporate-actions/preview") as prev:
        _ratio_to(modal).fill("2")
    assert prev.value.status == 200, prev.value.text()
    expect(modal.locator(".ca-issue").first).to_contain_text(_D3)   # resolved, as read
    expect(modal).not_to_contain_text("{account:")
    _ack_checkbox(modal).check()
    with page.expect_request("**/api/ledgers/corporate-actions") as sent, \
            page.expect_response("**/api/ledgers/corporate-actions") as saved:
        _save_button(modal).click()
    # pre-fix: 422 warnings_changed, on every attempt
    assert saved.value.status == 201, saved.value.text()
    posted = json.loads(sent.value.post_data or "{}")
    assert posted["acked_keys"] == [i["ack_key"] for i in prev.value.json()["issues"]
                                    if i["sev"] != "error"]
    expect(page.locator(".ca-modal")).to_have_count(0)
    assert [r["ratio_to"] for r in _rows(page)] == ["2"]


def _open_edit(page: Page, base: str) -> Locator:
    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_function("() => !!window.pdApi && !!window.pdAckConfirm")
    page.click("#tab-laction")
    row = page.locator("#action-body tr", has_text="2330")
    expect(row).to_have_count(1)
    row.locator("button", has_text="編輯").click()
    modal = page.locator(".modal-backdrop .modal", has_text="編輯公司行動")
    modal.locator(".field", has_text="變成／換得（股）").locator("input").fill("3")
    return modal


@pytest.mark.e2e
def test_r1_the_edit_dialog_saves_an_acknowledged_account_naming_warning(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_with_split)
    page = fresh_page
    modal = _open_edit(page, base)
    with page.expect_response(_is_action_put) as first:
        modal.locator("button", has_text="儲存").click()
    assert first.value.status == 422
    assert first.value.json()["error"]["code"] == "warnings_unacknowledged"
    warn = page.locator(".modal-backdrop .modal", has_text="公司行動警告確認")
    expect(warn).to_contain_text(_D3)
    expect(warn).not_to_contain_text("{account:")
    with page.expect_response(_is_action_put) as second:
        warn.locator("button", has_text="我了解，仍要儲存").click()
    assert second.value.status == 200, second.value.text()   # pre-fix: 422 warnings_changed
    page.wait_for_function(
        "async () => (await window.pdApi.get('/api/ledgers/corporate-actions'))"
        ".rows[0].ratio_to === '3'")


@pytest.mark.e2e
def test_r2_a_re_send_refused_with_new_warnings_reopens_the_warnings_dialog(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_with_split)
    page = fresh_page
    modal = _open_edit(page, base)
    with page.expect_response(_is_action_put):
        modal.locator("button", has_text="儲存").click()
    warn = page.locator(".modal-backdrop .modal", has_text="公司行動警告確認")
    expect(warn).to_contain_text("1000 股會依比例換算")

    # While the dialog is open the ledger moves: D3's sentence quotes the opening's shares.
    moved = page.evaluate(
        "() => window.pdApi.put('/api/ledgers/openings/tw_broker/2330',"
        " {shares: '1500', date: '2026-03-16'})")
    assert moved.get("ok") is True, moved

    with page.expect_response(_is_action_put) as resent:
        warn.locator("button", has_text="我了解，仍要儲存").click()
    assert resent.value.status == 422
    assert resent.value.json()["error"]["code"] == "warnings_changed"
    # pre-fix: only a fail toast — no way to read or acknowledge the new warning
    again = page.locator(".modal-backdrop .modal", has_text="公司行動警告確認")
    expect(again).to_contain_text("警告內容已經改變，請重新閱讀")
    expect(again).to_contain_text("1500 股會依比例換算")
    assert [r["ratio_to"] for r in _rows(page)] == ["2"]       # nothing written yet

    with page.expect_response(_is_action_put) as last:
        again.locator("button", has_text="我了解，仍要儲存").click()
    assert last.value.status == 200, last.value.text()
    page.wait_for_function(
        "async () => (await window.pdApi.get('/api/ledgers/corporate-actions'))"
        ".rows[0].ratio_to === '3'")
