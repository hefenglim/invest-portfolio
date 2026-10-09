"""E2E options W7b — the 待確認到期 inbox against the REAL routes (spec §7.2, plan W7b).

W5 pinned the panel to the §7.2 contract with ``page.route`` stubs and W6 built the backend
alone; this file runs the two together after the router is wired in ``api/app.py``: the
detector lists the seeded past-expiry positions (a short put and a long call), 略過 / 取消略過
round-trips through ``option_expiry_skips``, 確認到期作廢 writes the §4.1 price-0 row for one
item, 全部確認 (in-page dialog) writes the rest, the sidebar badge counts the items, and a
stock-only ledger never shows the panel.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from playwright.sync_api import Page
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.data_ingestion.register import register_option_contract
from portfolio_dash.data_ingestion.store import insert_transaction
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.options import OptionTerms, option_symbol
from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory
from tests.e2e.options_seed import PAST_PUT, seed_option_ledger

_SWEEP_DAY = date(2099, 1, 2)   # the DEF-056 sweep's seeded day — never an expiry here
_PAST_CALL_EXPIRY = date.today() - timedelta(days=3)
assert _PAST_CALL_EXPIRY != _SWEEP_DAY
PAST_CALL = option_symbol(OptionTerms(underlying="AAPL", expiry=_PAST_CALL_EXPIRY,
                                      strike=Decimal("160"), right="C"))


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _seed(conn: sqlite3.Connection) -> None:
    """W5's option ledger (``PAST_PUT``: a 1-contract short past its expiry) + a 1-contract
    LONG call that expired three days ago — one item of each direction."""
    seed_option_ledger(conn)
    register_option_contract(conn, PAST_CALL)
    insert_transaction(conn, account_id="schwab", symbol=PAST_CALL, side=Side.BUY,
                       quantity=Decimal("100"), price=Decimal("1.10"), fees=Decimal("0.65"),
                       tax=Decimal("0"), trade_date=date.today() - timedelta(days=12))
    conn.commit()


def _errors(page: Page) -> list[str]:
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    def _console(m: Any) -> None:
        if getattr(m, "type", None) == "error":
            errors.append(getattr(m, "text", ""))

    page.on("console", _console)
    return errors


def _close_rows(page: Page, base: str) -> dict[str, dict[str, Any]]:
    rows = page.request.get(base + "/api/ledgers/transactions?limit=500").json()["rows"]
    return {r["symbol"]: r for r in rows if r.get("close_reason")}


@pytest.mark.e2e
def test_the_inbox_lists_skips_and_confirms_against_the_real_routes(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed)
    page = fresh_page
    errors = _errors(page)
    page.goto(base + "/dividend-inbox.html", wait_until="load")
    page.wait_for_selector("#expiry-section:not([hidden]) .xp-item")

    items = page.locator("#expiry-list .xp-item")
    assert items.count() == 2
    texts = [items.nth(i).inner_text() for i in range(2)]
    assert any("賣方 1 口" in t and "全數實現" in t for t in texts), texts
    assert any("買方 1 口" in t and "全數損失" in t for t in texts), texts
    assert all("{account:" not in t for t in texts)
    page.wait_for_function(
        "() => { const b = document.querySelector('.sb-badge-alert');"
        " return !!b && (b.title || '').includes('選擇權到期 2'); }")

    # 略過 the long call → it leaves the list and appears under 已略過; 取消略過 brings it back.
    call_item = page.locator("#expiry-list .xp-item", has_text="買方 1 口")
    call_item.locator(".xp-skip").click()
    page.wait_for_function(
        "() => document.querySelectorAll('#expiry-list .xp-item').length === 1")
    page.click("#expiry-skipped > summary")
    page.wait_for_selector("#expiry-skipped-list .xp-unskip")
    page.click("#expiry-skipped-list .xp-unskip")
    page.wait_for_function(
        "() => document.querySelectorAll('#expiry-list .xp-item').length === 2")

    # 確認到期作廢 on the short put → the §4.1 row: BUY, price 0, the whole position, expiry.
    put_item = page.locator("#expiry-list .xp-item", has_text="賣方 1 口")
    put_item.locator(".xp-confirm").click()
    page.wait_for_function(
        "() => document.querySelectorAll('#expiry-list .xp-item').length === 1")
    put_row = _close_rows(page, base)[PAST_PUT]
    assert (put_row["side"], put_row["close_reason"], put_row["contracts"]) == (
        "buy", "expired", "1")
    assert Decimal(put_row["price"]) == 0 and put_row["option_side"] == "到期作廢"

    # 全部確認 → the in-page confirm dialog → the long call's SELL at 0.
    page.click("#expiry-confirm-all")
    page.wait_for_selector(".modal-backdrop .modal-foot .btn-primary")
    page.click(".modal-backdrop .modal-foot .btn-primary")
    # every item succeeded: the panel reports by toast (the results list is for failures only)
    page.wait_for_function(
        "() => Array.from(document.querySelectorAll('.toast-ok .msg'))"
        ".some((n) => (n.textContent || '').includes('已登錄到期作廢 1 筆'))")
    call_row = _close_rows(page, base)[PAST_CALL]
    assert (call_row["side"], call_row["close_reason"], call_row["date"]) == (
        "sell", "expired", _PAST_CALL_EXPIRY.isoformat())
    assert page.request.get(base + "/api/option-expiry/count").json() == {"count": 0}
    assert not errors, errors


@pytest.mark.e2e
def test_a_stock_ledger_never_shows_the_panel(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_golden)
    page = fresh_page
    errors = _errors(page)
    with page.expect_response("**/api/option-expiry/skipped"):
        page.goto(base + "/dividend-inbox.html", wait_until="load")
    page.wait_for_selector("#rebate-list")
    assert page.locator("#expiry-section").is_hidden()
    assert page.request.get(base + "/api/option-expiry/count").json() == {"count": 0}
    assert not errors, errors
