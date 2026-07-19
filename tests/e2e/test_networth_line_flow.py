"""E2E: the 總淨值（含現金）line + header sub-stat on the dashboard 市值 trend card (FU-D29).

Seeds a small TWD portfolio WITH a cash deposit so net worth (= market value + cash) is
computable, drives the real dashboard, and asserts the third trend series + legend entry
exist and the 「含現金 …」 header sub-stat renders — all with ZERO console errors and ZERO
uncaught page errors (the Decimal-string-through-ECharts money-passthrough invariant).
"""

import sqlite3
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import Page
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_cash_movement,
    insert_transaction,
    upsert_instrument,
)
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.e2e.conftest import FlowServerFactory

_FETCHED = datetime(2026, 6, 4, tzinfo=ZoneInfo("Asia/Taipei"))


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    """Re-enable loopback sockets per test (matches the other flow modules)."""
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _seed_networth(conn: sqlite3.Connection) -> None:
    """A TWD holding priced daily + a TWD cash deposit -> a cash-complete net worth."""
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="BBB", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Semis", name="BBB Corp", board="TWSE"))
    insert_cash_movement(conn, account_id="tw_broker", move_date=date(2026, 6, 1),
                         kind="DEPOSIT", ccy=Currency.TWD, amount=Decimal("100000"))
    insert_transaction(conn, account_id="tw_broker", symbol="BBB", side=Side.BUY,
                       quantity=Decimal("100"), price=Decimal("500"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 6, 1))
    upsert_prices(conn, [
        PriceRow(instrument="BBB", market=Market.TW, as_of=date(2026, 6, d),
                 close=Decimal(str(500 + d)), source="test")  # 501, 502, 503
        for d in (1, 2, 3)
    ], fetched_at=_FETCHED)


@pytest.mark.e2e
def test_networth_line_and_header_render(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base_url = flow_server(_seed_networth)
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
        page.goto(base_url + "/index.html", wait_until="load")
        page.wait_for_selector(".kpi-card")  # dashboard async render landed
        page.wait_for_function("() => document.querySelector('#trend-chart canvas') !== null")
        # The 市值 chart carries a third series + legend entry for 總淨值（含現金）.
        page.wait_for_function(
            "() => { const el = document.getElementById('trend-chart');"
            " const inst = window.echarts && window.echarts.getInstanceByDom(el);"
            " if (!inst) return false;"
            " const opt = inst.getOption();"
            " const names = (opt.series || []).map(s => s.name);"
            " const legend = ((opt.legend || [])[0] || {}).data || [];"
            " return names.indexOf('總淨值（含現金）') !== -1"
            "   && legend.indexOf('總淨值（含現金）') !== -1; }"
        )
        # The header sub-stat shows the current net worth (含現金 …), not hidden.
        page.wait_for_function(
            "() => { const el = document.getElementById('trend-networth');"
            " return el && !el.hidden && el.textContent.indexOf('含現金') !== -1; }"
        )
    finally:
        page.remove_listener("console", _on_console)
        page.remove_listener("pageerror", _on_pageerror)

    assert not console_errors and not page_errors, (
        f"net worth line: console errors={console_errors!r}; page errors={page_errors!r}"
    )
