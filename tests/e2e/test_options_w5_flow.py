"""E2E options W5 (spec §7.1, §8, §6.2): the 選擇權 section, option-mode entry, the 轉倉 line,
the contract drawer and the refused-close banner — on a seeded option ledger — and the
golden (stock-only) dashboard carrying no trace of any of it (invariant 1).

The expiry-inbox routes are not wired into the app in this wave (they land with W6/W7b), so
every page here stubs ``/api/option-expiry/*`` (the sidebar badge reads its count on every
page); the inbox panel's own flows are in ``test_options_w5_expiry_inbox_flow.py``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from playwright.sync_api import Page, Route
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.data_ingestion.register import register_option_contract
from portfolio_dash.data_ingestion.store import insert_transaction
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.options import TREND_EXCLUDES_OPTIONS_NOTE
from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory
from tests.e2e.options_seed import (
    CLOSED_PUT,
    EXPIRED_CALL,
    OPEN_CALL,
    OPEN_PUT,
    PAST_PUT,
    seed_option_ledger,
)


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def stub_expiry(page: Page) -> None:
    """``/api/option-expiry/*`` answered empty — the routes are wired in W7b."""

    def handle(route: Route) -> None:
        url = route.request.url
        body: dict[str, Any] = {"count": 0} if "/count" in url else {"items": []}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route("**/api/option-expiry**", handle)


def _errors(page: Page) -> list[str]:
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    def _console(m: Any) -> None:
        if getattr(m, "type", None) == "error":
            errors.append(getattr(m, "text", ""))

    page.on("console", _console)
    return errors


@pytest.mark.e2e
def test_the_options_section_renders_from_the_block(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(seed_option_ledger)
    page = fresh_page
    page.set_viewport_size({"width": 1440, "height": 1000})
    errors = _errors(page)
    stub_expiry(page)
    page.goto(base + "/index.html", wait_until="load")
    page.wait_for_selector("#options-panel #opt-open-body tr.opt-row")

    rows = page.locator("#opt-open-body tr.opt-row")
    assert rows.count() == 3
    # 未平倉 by 剩餘天數 ↑ (the server's order): the expired put, the call, the open put.
    assert [rows.nth(i).get_attribute("data-symbol") for i in range(3)] == [
        PAST_PUT, OPEN_CALL, OPEN_PUT]
    past = rows.nth(0).inner_text()
    assert "已過到期日・待登錄" in past and "已過 5 天" in past and "登錄到期作廢" in past
    assert "買方 1 口" in rows.nth(1).inner_text()
    put = rows.nth(2).inner_text()
    assert "賣方 2 口" in put and "22,000.00" in put and "70 天" in put
    assert "120.00" in put                                 # 標的現價 beside the strike
    assert "32,000.00 USD" in page.inner_text("#opt-obligation")   # 履約義務合計

    # the expander: 股當量 and 最後成交日 (spec §7.1)
    rows.nth(2).locator(".opt-c-days").click()
    detail = page.locator("#opt-open-body tr.opt-detail-row").nth(2)
    detail.wait_for(state="visible")
    assert "股當量" in detail.inner_text() and "-200" in detail.inner_text()

    # 已實現: 依合約 with reason chips, then the tabs
    rz = page.locator("#opt-rz-table tbody tr")
    assert rz.count() == 2
    chips = page.locator("#opt-rz-table .opt-reason").all_inner_texts()
    assert sorted(chips) == ["到期作廢", "買進平倉"]
    page.click(".opt-rz-tabs .range-btn[data-tab='year']")
    page.wait_for_function(
        "() => document.querySelector('#opt-rz-table thead th').textContent === '年度'")
    page.click(".opt-rz-tabs .range-btn[data-tab='underlying']")
    page.wait_for_function(
        "() => document.querySelector('#opt-rz-table tbody td').textContent === 'AAPL'")

    # the R3 note under the section and under the trend
    assert page.inner_text("#opt-note") == TREND_EXCLUDES_OPTIONS_NOTE
    page.wait_for_selector("#trend-opt-note")
    # KPI 「其中選擇權」 rows (總市值, 已實現, 未實現) and 本金匯率效果 「—」 with the reason
    assert page.locator(".kpi-opt-row").count() == 3
    band = page.inner_text("#kpi-band")
    assert "本金匯率效果" in band and "有未平倉選擇權時" in band
    # the 已實現損益 section's separate option line
    assert "選擇權已實現" in page.inner_text("#realized-opt-line")
    assert not errors, errors


@pytest.mark.e2e
def test_the_open_contracts_render_as_cards_on_a_phone(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """≤ 640 px (spec §7.1): one card per contract, no header row, no page-wide scroll."""
    base = flow_server(seed_option_ledger)
    page = fresh_page
    page.set_viewport_size({"width": 390, "height": 860})
    errors = _errors(page)
    stub_expiry(page)
    page.goto(base + "/index.html", wait_until="load")
    page.wait_for_selector("#opt-open-body tr.opt-row")
    assert page.locator("#opt-open-table thead").is_hidden()
    shape = page.evaluate("""() => {
      const row = document.querySelector('#opt-open-body tr.opt-row');
      const td = row.querySelector('td.opt-c-days');
      const doc = document.documentElement;
      return {row: getComputedStyle(row).display, td: getComputedStyle(td).display,
              label: getComputedStyle(td, '::before').content,
              overflow: doc.scrollWidth - doc.clientWidth};
    }""")
    assert shape["row"] == "block" and shape["td"] == "flex", shape
    assert "剩餘天數" in shape["label"], shape
    assert shape["overflow"] <= 1, shape
    assert not errors, errors


@pytest.mark.e2e
def test_a_stock_ledger_dashboard_carries_no_option_trace(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_golden)
    page = fresh_page
    errors = _errors(page)
    stub_expiry(page)
    page.goto(base + "/index.html", wait_until="load")
    page.wait_for_selector("#holdings-body tr")
    page.wait_for_selector(".kpi-card")
    for sel in ("#options-panel", ".kpi-opt-row", "#realized-opt-line", "#trend-opt-note",
                "#refused-close-banner"):
        assert page.locator(sel).count() == 0, sel
    assert not errors, errors


@pytest.mark.e2e
def test_register_expiry_from_the_section_lands_on_the_prefilled_form(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(seed_option_ledger)
    page = fresh_page
    errors = _errors(page)
    stub_expiry(page)
    page.goto(base + "/index.html", wait_until="load")
    page.wait_for_selector("#opt-open-body .opt-close-btn")
    with page.expect_navigation():
        page.click("#opt-open-body .opt-close-btn")
    page.wait_for_selector("#m-opt-closes button[data-close='expired'].active")
    expiry = (date.today() - timedelta(days=5)).isoformat()
    assert page.input_value("#m-symbol") == PAST_PUT
    assert page.input_value("#m-contracts") == "1"
    assert page.input_value("#m-date") == expiry
    page.wait_for_function(
        "() => document.querySelector('#m-pc-label').textContent.startsWith('到期作廢')")
    try:
        page.wait_for_function("() => !document.querySelector('#m-confirm').disabled",
                               timeout=15000)
    except Exception as exc:  # noqa: BLE001 (diagnostic)
        raise AssertionError(page.inner_text("#m-issues") + " || "
                             + page.inner_text("#m-pc-rows")) from exc
    assert "+149.35" in page.inner_text("#m-pc-rows")      # the premium the replay books
    page.click("#m-confirm")
    page.wait_for_selector(".toast-ok")
    page.wait_for_selector("#tx-body .tx-opt-side:text('到期作廢')")
    assert not errors, errors


@pytest.mark.e2e
def test_option_mode_sell_to_open_then_buy_to_close_offers_the_roll(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_golden)
    page = fresh_page
    errors = _errors(page)
    stub_expiry(page)
    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#m-account option[value='schwab']", state="attached")
    page.select_option("#m-account", "schwab")
    page.click("#m-symbol")
    page.wait_for_selector("#m-sym-addopt:visible")
    page.click("#m-sym-addopt")
    page.wait_for_selector("#m-opt-builder:visible")
    expiry = (date.today() + timedelta(days=60)).isoformat()
    assert expiry != "2099-01-02"
    page.fill("#m-ob-underlying", "AAPL")
    page.fill("#m-ob-expiry", expiry)
    page.fill("#m-ob-strike", "100")
    page.click("#m-ob-put")
    page.click("#m-ob-make")
    page.wait_for_selector("#m-opt-side-field:visible")
    symbol = page.input_value("#m-symbol")
    assert symbol.startswith("AAPL") and symbol.endswith("P00100000")
    # OPT-D18: no position → 賣出開倉
    page.wait_for_selector("#m-opt-sides button[data-intent='STO'].active")
    page.fill("#m-contracts", "1")
    page.fill("#m-price", "2.00")
    page.wait_for_function(
        "() => document.querySelector('#m-pc-label').textContent === '淨收權利金'")
    page.wait_for_function(
        "() => document.querySelector('#m-contracts-hint').textContent.includes('100 股當量')")
    assert "每口 0.65" in page.inner_text("#m-fee-rule")
    page.wait_for_function("() => !document.querySelector('#m-confirm').disabled")
    page.click("#m-confirm")
    page.wait_for_selector("#tx-body .tx-opt-side:text('賣出開倉')")

    # The short is held now: the default is 買進平倉 with the whole short as 口數.
    page.wait_for_selector("#m-opt-sides button[data-intent='BTC'].active")
    page.wait_for_function("() => document.querySelector('#m-contracts').value === '1'")
    page.fill("#m-price", "0.50")
    page.wait_for_function(
        "() => document.querySelector('#m-pc-label').textContent === '權利金總成本'")
    page.wait_for_function("() => !document.querySelector('#m-confirm').disabled")
    page.click("#m-confirm")
    page.wait_for_selector("#tx-body .tx-opt-side:text('買進平倉')")

    # 轉倉: the builder reopens on the same underlying and right, 到期日 to fill.
    page.wait_for_selector("#m-opt-roll-btn:visible")
    page.click("#m-opt-roll-btn")
    page.wait_for_selector("#m-opt-builder:visible")
    assert page.input_value("#m-ob-underlying") == "AAPL"
    assert "active" in (page.get_attribute("#m-ob-put", "class") or "")
    assert page.input_value("#m-ob-expiry") == ""
    assert not errors, errors


@pytest.mark.e2e
def test_an_open_contracts_drawer_reconciles_and_its_underlying_names_it(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(seed_option_ledger)
    page = fresh_page
    errors = _errors(page)
    stub_expiry(page)
    page.goto(base + "/index.html", wait_until="load")
    page.wait_for_selector(".kpi-card")
    with page.expect_response(f"**/api/symbol/{OPEN_PUT}/detail"):
        page.evaluate("(s) => window.pdOpenSymbol(s)", OPEN_PUT)
    page.wait_for_selector(".sd-drawer .sd-contract")
    head = page.inner_text(".sd-head .sym-code")
    assert head.startswith("AAPL ") and head.endswith(" 110 Put")
    body = page.inner_text(".sd-drawer")
    assert "選擇權不提供試算" in body and "配息史" not in body
    foot = page.inner_text(".sd-tx-reconcile")
    assert "✓ 對帳一致" in foot and "股當量" in foot
    assert "賣出開倉" in page.inner_text(".sd-tx-section")

    # A closed contract: its realized row says how it closed.
    with page.expect_response(f"**/api/symbol/{EXPIRED_CALL}/detail"):
        page.evaluate("(s) => window.pdOpenSymbol(s)", EXPIRED_CALL)
    page.wait_for_function(
        "() => (document.querySelector('.sd-drawer') || {}).textContent"
        "  && document.querySelector('.sd-drawer').textContent.includes('140 Call')")
    page.wait_for_selector(".sd-drawer .opt-reason")
    assert "到期作廢" in page.inner_text(".sd-drawer .opt-reason")

    # The underlying's drawer names the open contracts written on it.
    with page.expect_response("**/api/symbol/AAPL/detail"):
        page.evaluate("(s) => window.pdOpenSymbol(s)", "AAPL")
    page.wait_for_selector(".sd-drawer .sd-opt-line")
    line = page.inner_text(".sd-drawer .sd-opt-line")
    assert line.startswith("未平倉選擇權：") and "Put（賣方）" in line and "選擇權區" in line
    assert CLOSED_PUT not in line
    assert not errors, errors


def _seed_refused_close(conn: sqlite3.Connection) -> None:
    """The golden ledger + one 到期作廢 row on a contract nobody opened — the replay refuses
    it (§4.4), so XIRR is withheld and the row is 未入帳（待釐清）."""
    _seed_golden(conn)
    sym = OPEN_CALL
    register_option_contract(conn, sym)
    insert_transaction(conn, account_id="schwab", symbol=sym, side=Side.SELL,
                       quantity=Decimal("100"), price=Decimal("0"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date.today() - timedelta(days=3),
                       close_reason="expired")
    conn.commit()


@pytest.mark.e2e
def test_a_refused_close_is_named_on_the_dashboard_and_flashed_in_the_ledger(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_refused_close)
    page = fresh_page
    errors = _errors(page)
    stub_expiry(page)
    page.goto(base + "/index.html", wait_until="load")
    page.wait_for_selector("#refused-close-banner .unreg-link")
    assert "未入帳（待釐清）" in page.inner_text("#refused-close-banner")
    with page.expect_navigation():
        page.click("#refused-close-banner .unreg-link")
    page.wait_for_selector("#tx-body tr.ledger-added-row")
    flashed = page.inner_text("#tx-body tr.ledger-added-row")
    assert "未入帳（待釐清）" in flashed and "到期作廢" in flashed
    assert not errors, errors
