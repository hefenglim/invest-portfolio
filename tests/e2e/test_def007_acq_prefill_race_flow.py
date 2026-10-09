"""E2E DEF-007 / DEF-008 (functional test manual A-02 / A-05, 2026-09-23), in a real browser.

DEF-007 — the 取得成本 reference rate is a LATE write. The live site answers
``GET /api/cash/acq-rate`` in 5–30 s, so the owner routinely types the rate they actually
dealt at before the reference arrives; the reference then overwrote it (and forced the mode
back to 匯率), and 確認 booked the reference instead. The race is reproduced exactly: every
acq-rate request is HELD by a Playwright route until the owner has typed, then released.

DEF-008 — a refused withdrawal's toast shows the zh sentence (the account resolved from its
``{account:<id>}`` token by the fetch layer) and NOT the machine code: the code rides the
tooltip for diagnostics.

Golden FX: USD/TWD is stored for 2026-06-09 (33) and not for 2026-06-10 — so a movement dated
06-10 resolves to the 06-09 close, and the hint must say 06-09.
"""

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from playwright.sync_api import Page, Route
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.data_ingestion.store import insert_cash_movement
from portfolio_dash.shared.enums import Currency
from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _seed(conn: Any) -> None:
    _seed_golden(conn)
    # schwab USD funded on 06-01, so a withdrawal dated EARLIER passes the page's live
    # (today) ceiling and reaches the backend's date-aware guard.
    insert_cash_movement(conn, account_id="schwab", move_date=date(2026, 6, 1),
                         kind="DEPOSIT", ccy=Currency.USD, amount=Decimal("1000"))
    conn.commit()


def _open_foreign_deposit(page: Page, base: str) -> None:
    # about:blank first: a goto to the SAME url-with-fragment is a same-document navigation
    # (no reload), which would carry the previous scenario's form state into this one.
    page.goto("about:blank")
    page.goto(base + "/cash.html#flows", wait_until="load")
    page.wait_for_selector("#cm-account option", state="attached")
    page.select_option("#cm-account", "schwab")
    page.wait_for_function(
        "() => Array.from(document.querySelectorAll('#cm-ccy option'))"
        ".some((o) => o.value === 'USD')")
    page.select_option("#cm-ccy", "USD")
    page.fill("#cm-date", "2026-06-10")
    page.dispatch_event("#cm-date", "change")
    page.wait_for_function(
        "() => { const n = document.querySelector('#cm-acq-field'); return n && !n.hidden; }")


def _hint(page: Page) -> str:
    return str(page.evaluate("() => document.querySelector('#cm-acq-hint').textContent"))


@pytest.mark.e2e
def test_a_late_reference_rate_never_overwrites_what_the_owner_typed(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed)
    page = fresh_page
    page_errors: list[str] = []
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    held: list[Route] = []
    page.route("**/api/cash/acq-rate*", lambda route: held.append(route))
    try:
        # ---- 匯率 mode: type 31.5 while the lookup is in flight ------------------------
        _open_foreign_deposit(page, base)
        page.wait_for_function("() => true")          # let the held requests register
        assert held, "the acq-rate lookup never went out — the race is not reproduced"
        page.fill("#cm-acq", "31.5")
        for route in list(held):
            route.continue_()
        held.clear()
        page.wait_for_function(
            "() => document.querySelector('#cm-acq-hint').textContent.includes('參考值')")
        assert page.input_value("#cm-acq") == "31.5", "the late reference overwrote the owner"
        assert page.input_value("#cm-acq-mode") == "rate"
        hint = _hint(page)
        # The RATE's date (the 06-09 close), and the reason it is not the typed 06-10.
        assert "參考值：2026-06-09 收盤" in hint, hint
        assert "2026-06-10 尚無收盤匯率" in hint, hint
        assert "未覆寫你輸入的值" in hint, hint

        # …and 確認 books what the owner typed: 1,000 USD × 31.5 = 31,500 TWD.
        page.fill("#cm-amount", "1000")
        page.click("#cm-confirm")
        page.wait_for_function("() => document.querySelector('#cm-amount').value === ''")
        rows = page.evaluate(
            "async () => (await window.pdApi.get('/api/cash')).movements.rows")
        booked = next(r for r in rows if r["ccy"] == "USD" and r["amount"] == "1000"
                      and r["date"] == "2026-06-10")
        assert booked["acq_home_amount"] == "31500", booked

        # ---- 家幣金額 mode: the reference neither overwrites nor flips the mode ----------
        _open_foreign_deposit(page, base)
        page.select_option("#cm-acq-mode", "amount")
        page.fill("#cm-acq", "31500")
        page.wait_for_function("() => true")
        for route in list(held):
            route.continue_()
        held.clear()
        page.wait_for_function(
            "() => document.querySelector('#cm-acq-hint').textContent.includes('參考值')")
        assert page.input_value("#cm-acq") == "31500"
        assert page.input_value("#cm-acq-mode") == "amount"
    finally:
        page.unroute("**/api/cash/acq-rate*")
        for route in held:
            route.continue_()

    # ---- pristine field: the reference still fills it (the prefill is not switched off) ---
    _open_foreign_deposit(page, base)
    page.wait_for_function("() => document.querySelector('#cm-acq').value !== ''")
    ref = page.evaluate(
        "async () => (await window.pdApi.get('/api/cash/acq-rate',"
        " { account_id: 'schwab', ccy: 'USD', on: '2026-06-10' })).rate")
    assert page.input_value("#cm-acq") == ref
    assert page.input_value("#cm-acq-mode") == "rate"
    assert page_errors == []


@pytest.mark.e2e
def test_a_refused_withdrawal_toasts_the_sentence_not_the_code(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed)
    page = fresh_page
    page.goto(base + "/cash.html#flows", wait_until="load")
    page.wait_for_selector("#cm-account option", state="attached")
    page.click("#cm-kind-out")
    page.select_option("#cm-account", "schwab")
    page.wait_for_function(
        "() => Array.from(document.querySelectorAll('#cm-ccy option'))"
        ".some((o) => o.value === 'USD')")
    page.select_option("#cm-ccy", "USD")
    page.dispatch_event("#cm-ccy", "change")
    page.fill("#cm-date", "2026-05-01")              # before the 06-01 funding
    page.fill("#cm-amount", "500")
    page.dispatch_event("#cm-amount", "input")
    page.click("#cm-confirm")
    page.wait_for_selector(".toast-host .toast-fail")
    toast = page.evaluate("""() => {
        const t = Array.from(document.querySelectorAll('.toast-host .toast-fail')).pop();
        return { msg: t.querySelector('.msg').textContent,
                 sub: t.querySelector('.sub') ? t.querySelector('.sub').textContent : null,
                 title: t.title, text: t.textContent };
    }""")
    assert toast["msg"] == (
        "此筆出金會使 嘉信 Schwab 的 USD 現金於 2026-05-01 降至 −500.00（出金當日）"
        "— 出金不可透支，請先補登入金或換匯"), toast
    assert toast["sub"] is None, f"the toast still prints a sub-line: {toast}"
    assert "withdraw_insufficient_balance" not in toast["text"], toast
    assert toast["title"] == "錯誤碼：withdraw_insufficient_balance", toast
