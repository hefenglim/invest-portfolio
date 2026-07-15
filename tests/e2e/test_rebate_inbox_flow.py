"""E2E: 收件匣 two-panel render + 折讓款 confirm flow (Wave B / FE-D1).

Uses an ISOLATED flow server (the confirm mutates the DB) seeded with the golden scenario
PLUS a prior-month fee-bearing TW trade, so the rebate panel carries exactly one pending
month. Drives the REAL page: both panels render, the sidebar badge counts the pending
rebate, and 確認入帳 → editable-amount prompt → POST /api/rebates/confirm (200) → the month
self-heals out of the inbox. ZERO console + ZERO page errors throughout.
"""

from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal

import pytest
from playwright.sync_api import Page
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.data_ingestion.store import insert_transaction
from portfolio_dash.shared.models.enums import Side
from tests.conftest import _seed_golden


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    """Re-enable loopback sockets per test (pytest-socket re-bans before each test).

    Required by every flow-server e2e file (mirrors test_whatsnew_flow.py /
    test_flows_e1_e10.py); without it the file passes in isolation but fails under
    the full-suite ordering with SocketBlockedError at _free_port().
    """
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _seed_rebate(conn: object) -> None:
    """Golden scenario + a prior-calendar-month tw_broker trade WITH a real fee.

    The trade month (5th of last month, relative to the real clock the flow server runs
    under) is always < the current month -> its rebate is PENDING; fee 142 -> estimate
    floor(142 × 0.77) = 109.
    """
    _seed_golden(conn)  # type: ignore[arg-type]
    prior_month_last = date.today().replace(day=1) - timedelta(days=1)
    trade_date = prior_month_last.replace(day=5)
    insert_transaction(
        conn,  # type: ignore[arg-type]
        account_id="tw_broker", symbol="2330", side=Side.BUY,
        quantity=Decimal("1000"), price=Decimal("500"), fees=Decimal("142"),
        tax=Decimal("0"), trade_date=trade_date)


@pytest.mark.e2e
def test_rebate_inbox_two_panel_and_confirm(
    flow_server: object, fresh_page: Page
) -> None:
    base = flow_server(_seed_rebate)  # type: ignore[operator]
    page = fresh_page

    console_errors: list[str] = []
    page_errors: list[str] = []

    def _on_console(msg: object) -> None:
        if getattr(msg, "type", None) == "error":
            console_errors.append(getattr(msg, "text", repr(msg)))

    def _on_pageerror(exc: object) -> None:
        page_errors.append(str(exc))

    page.on("console", _on_console)
    page.on("pageerror", _on_pageerror)
    try:
        page.goto(str(base) + "/dividend-inbox.html", wait_until="load")
        # BOTH panels render: the dividend inbox (empty-state note, no events) + the rebate
        # panel (one pending month from the prior-month fee trade).
        page.wait_for_selector("#inbox-section")
        page.wait_for_selector("#rebate-section")
        page.wait_for_selector("#inbox-list .inbox-note")
        page.wait_for_selector("#rebate-list .inbox-item")
        # sidebar badge counts the pending rebate (1 rebate + 0 dividends) on every page.
        page.wait_for_selector(".sb-badge-alert")

        # 確認入帳 -> a small prompt with the estimate PREFILLED into an editable amount input.
        page.click("#rebate-list .inbox-item .btn-primary")
        page.wait_for_selector(".modal-backdrop .modal input")
        assert page.input_value(".modal-backdrop .modal input") == "109"

        # Confirm -> POST /api/rebates/confirm returns 200; the month self-heals out.
        with page.expect_response("**/api/rebates/confirm") as resp_info:
            page.click(".modal-backdrop .modal-foot .btn-primary")
        assert resp_info.value.status == 200, (
            f"confirm status {resp_info.value.status}"
        )
        page.wait_for_selector("#rebate-list .inbox-note")  # back to the empty state
    finally:
        page.remove_listener("console", _on_console)
        page.remove_listener("pageerror", _on_pageerror)

    assert not console_errors and not page_errors, (
        f"rebate inbox flow: console errors={console_errors!r}; page errors={page_errors!r}"
    )
