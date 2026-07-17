"""E2E: the 績效比較 (TWR overlay, FU-D27) mode toggle on the dashboard trend card.

Drives the REAL app against an isolated flow server seeded with a small TWD portfolio +
a 0050 benchmark series written directly into ``prices``. Navigates the dashboard, toggles
the trend card from 市值 to 績效比較, and asserts the lazily-initialised overlay chart
renders (a canvas mounts) with ZERO console errors + ZERO uncaught page errors — the
Decimal-string-through-ECharts path the money-passthrough invariant guards.
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
from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.e2e.conftest import FlowServerFactory

_FETCHED = datetime(2026, 6, 4, tzinfo=ZoneInfo("Asia/Taipei"))


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    """Re-enable loopback sockets PER TEST. pytest-socket's --disable-socket re-bans
    sockets before every test; the session-scoped _e2e_loopback_socket only lifts the ban
    once. flow_server's free-port probe + readiness poll open fresh Python sockets per test,
    so each needs the loopback exception re-applied here (matches the other flow modules)."""
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _seed_twr(conn: sqlite3.Connection) -> None:
    """A TWD holding priced daily + the 0050 benchmark series (both TWD, no FX)."""
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="BBB", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Semis", name="BBB Corp", board="TWSE"))
    insert_transaction(conn, account_id="tw_broker", symbol="BBB", side=Side.BUY,
                       quantity=Decimal("100"), price=Decimal("10"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 6, 1))
    upsert_prices(conn, [
        PriceRow(instrument="BBB", market=Market.TW, as_of=date(2026, 6, d),
                 close=Decimal(str(9 + d)), source="test")  # 10, 11, 12
        for d in (1, 2, 3)
    ], fetched_at=_FETCHED)
    upsert_prices(conn, [
        PriceRow(instrument="0050", market=Market.TW, as_of=date(2026, 6, d),
                 close=Decimal(str(100 + d)), source="test")  # 101, 102, 103
        for d in (1, 2, 3)
    ], fetched_at=_FETCHED)


@pytest.mark.e2e
def test_twr_overlay_mode_renders(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base_url = flow_server(_seed_twr)
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
        # The value chart is the default; the overlay chart is hidden + uninitialised.
        assert page.query_selector("#twr-chart canvas") is None
        # Toggle to 績效比較 -> lazily fetch /api/performance/twr + init the overlay chart.
        with page.expect_response("**/api/performance/twr**") as resp_info:
            page.click('#trend-mode .range-btn[data-mode="twr"]')
        assert resp_info.value.status == 200, f"twr status {resp_info.value.status}"
        page.wait_for_function(
            "() => { const c = document.querySelector('#twr-chart');"
            " return c && !c.hidden && c.querySelector('canvas') !== null; }"
        )
        # The basis-notes caption is shown in TWR mode.
        page.wait_for_function(
            "() => { const c = document.querySelector('#twr-caption');"
            " return c && !c.hidden && c.textContent.indexOf('時間加權') !== -1; }"
        )
    finally:
        page.remove_listener("console", _on_console)
        page.remove_listener("pageerror", _on_pageerror)

    assert not console_errors and not page_errors, (
        f"TWR overlay: console errors={console_errors!r}; page errors={page_errors!r}"
    )
