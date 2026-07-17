"""E2E flow (Playwright, real server + real frontend) — accumulative watchlist (FU-D18).

Drives the REAL stack (uvicorn subprocess + on-disk SQLite + StaticFiles web/) against a
GUEST DB seeded with a held symbol (2330), a never-traded watch-only symbol (WATCH), and a
closed-with-history symbol (CLSD). Verifies, entirely through the UI, the FU-D18 soft-delete
behavior (supersedes FU-D13's three tiers):
  * 移除 on the never-traded WATCH SOFT-deletes it (ONE confirm; row hides — no hard delete),
  * 移除 on the closed-with-history CLSD ALSO soft-deletes with ONE confirm — the former
    has_history 封存 dialog is gone,
  * 移除 on the held 2330 is refused with the 無法移除 info dialog (422 held),
  * archived rows hide by default and reappear (dimmed, 已移除) via 顯示已移除／封存,
  * ZERO console errors (bar the held delete's expected 422) + ZERO uncaught page errors.

Restore + background gap backfill are covered in the contract / unit suites (with the fetch
mocked); they are deliberately NOT exercised here so the e2e flow stays network-free.
"""

import sqlite3
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import ConsoleMessage, Locator, Page, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.e2e.conftest import FlowServerFactory

_TAIPEI = ZoneInfo("Asia/Taipei")


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    """Re-enable loopback sockets PER TEST. pytest-socket's --disable-socket re-bans
    sockets before every test; the session-scoped _e2e_loopback_socket only lifts the
    ban once. These flows create fresh Python sockets per test (flow_server's free-port
    probe + readiness poll), so each needs the loopback exception re-applied here."""
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _seed_delete_flow(conn: sqlite3.Connection) -> None:
    """One held symbol, one never-traded watch-only symbol, one closed-with-history symbol."""
    seed_accounts(conn)
    fetched = datetime(2026, 6, 9, 15, 0, tzinfo=_TAIPEI)
    # held 2330 (so a 持有 row exists alongside the deletable ones)
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Semi", name="TSMC", board="TWSE"))
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=Decimal("1000"), price=Decimal("500"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 1, 5))
    # never-traded watch-only WATCH (+ a price row so its cleanup is exercised)
    upsert_instrument(conn, Instrument(symbol="WATCH", market=Market.US, quote_ccy=Currency.USD,
                                       sector="Tech", name="Watchy"))
    upsert_prices(conn, [PriceRow(instrument="WATCH", market=Market.US, as_of=date(2026, 6, 9),
                                  close=Decimal("50"), source="test")], fetched_at=fetched)
    # closed-with-history CLSD (buy then full sell → net 0)
    upsert_instrument(conn, Instrument(symbol="CLSD", market=Market.US, quote_ccy=Currency.USD,
                                       sector="Tech", name="Closed"))
    insert_transaction(conn, account_id="schwab", symbol="CLSD", side=Side.BUY,
                       quantity=Decimal("10"), price=Decimal("100"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 1, 10))
    insert_transaction(conn, account_id="schwab", symbol="CLSD", side=Side.SELL,
                       quantity=Decimal("10"), price=Decimal("120"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 2, 10))
    conn.commit()


def _row(page: Page, symbol: str) -> Locator:
    """The instrument table row whose code cell is exactly *symbol*."""
    return page.locator("#inst-body tr").filter(
        has=page.locator(".sym-code", has_text=symbol))


def test_delete_and_archive_flow(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base_url = flow_server(_seed_delete_flow)  # guest mode (no users)
    page = fresh_page

    console_errors: list[str] = []
    page_errors: list[str] = []

    def _on_console(msg: ConsoleMessage) -> None:
        if getattr(msg, "type", None) == "error":
            console_errors.append(getattr(msg, "text", repr(msg)))

    def _on_pageerror(exc: object) -> None:
        page_errors.append(str(exc))

    page.on("console", _on_console)
    page.on("pageerror", _on_pageerror)

    page.goto(base_url + "/instruments.html", wait_until="load")
    page.wait_for_selector("#inst-body tr")
    expect(_row(page, "WATCH")).to_have_count(1)

    # 1) 移除 the never-traded WATCH → ONE confirm → row hides (SOFT delete, not hard).
    _row(page, "WATCH").get_by_role("button", name="移除").click()
    page.locator(".modal-backdrop").last.get_by_role("button", name="移除").click()
    expect(_row(page, "WATCH")).to_have_count(0)

    # 2) 移除 the closed-with-history CLSD → ONE confirm → row hides too (no has_history dialog).
    _row(page, "CLSD").get_by_role("button", name="移除").click()
    page.locator(".modal-backdrop").last.get_by_role("button", name="移除").click()
    expect(_row(page, "CLSD")).to_have_count(0)

    # 3) 移除 the HELD 2330 → confirm → backend 422 held → the 無法移除 info dialog; row stays.
    _row(page, "2330").get_by_role("button", name="移除").click()
    page.locator(".modal-backdrop").last.get_by_role("button", name="移除").click()
    ack = page.get_by_role("button", name="我知道了")
    expect(ack).to_be_visible()
    ack.click()
    expect(_row(page, "2330")).to_have_count(1)

    # 4) The 顯示已移除／封存 toggle surfaces (2 archived); reveal → both reappear dimmed, 已移除.
    toggle = page.locator("#toggle-archived")
    expect(toggle).to_be_visible()
    expect(toggle).to_contain_text("顯示已移除／封存")
    toggle.click()
    expect(_row(page, "WATCH")).to_have_count(1)
    expect(_row(page, "CLSD")).to_have_count(1)
    expect(page.locator("#inst-body tr.inst-archived .status-tag.archived")).to_have_count(2)

    page.remove_listener("console", _on_console)
    page.remove_listener("pageerror", _on_pageerror)
    # The held DELETE deliberately 422s; Chromium logs that as a network-level "Failed to
    # load resource … status of 422" console entry (an EXPECTED response, not a JS fault —
    # same class E6 documents for its intentional 401). WATCH/CLSD soft-deletes are 200 (no
    # console noise). Tolerate ONLY that exact status; any other console error or ANY uncaught
    # page error still fails the flow.
    real_console = [e for e in console_errors if "status of 422" not in e]
    assert not real_console and not page_errors, (
        f"console errors={real_console!r}; page errors={page_errors!r}"
    )
