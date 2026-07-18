"""E2E flow (Playwright, real server + real frontend) — deletion tiers (FU-D32; over FU-D18).

Drives the REAL stack (uvicorn subprocess + on-disk SQLite + StaticFiles web/) against a
GUEST DB seeded with a held symbol (2330), a never-traded watch-only symbol (WATCH), and a
closed-with-history symbol (CLSD). The 移除 dialog now offers two destructive tiers — 移除（隱藏）
(FU-D18 soft delete, unchanged) and 永久移除 (hard purge). Verified entirely through the UI:

  * HELD 2330: 永久移除 is DISABLED (has_history); 移除（隱藏）is refused (422 held) → the 無法移除
    info dialog; the row stays,
  * CLOSED-with-history CLSD: 永久移除 is DISABLED with the owner's explanation; 移除（隱藏）
    soft-deletes it (row hides, still registered),
  * NEVER-TRADED WATCH: 永久移除 is gated by a type-confirm (disabled until the EXACT symbol is
    typed) then hard-purges it (row gone entirely — not merely archived),
  * archived rows hide by default and reappear (dimmed, 已移除) via 顯示已移除／封存,
  * ZERO console errors (bar the held soft-delete's expected 422) + ZERO uncaught page errors.

Purge does no network I/O (pure row deletes), so this flow stays deterministic + offline.
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


def _open_delete_dialog(page: Page, symbol: str) -> Locator:
    """Click a row's 移除 → return the freshly-opened three-tier dialog backdrop."""
    _row(page, symbol).get_by_role("button", name="移除").click()
    dialog = page.locator(".modal-backdrop").last
    expect(dialog.get_by_role("button", name="永久移除", exact=True)).to_be_visible()
    return dialog


def test_delete_tiers_flow(
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

    # 1) HELD 2330: 永久移除 DISABLED (has_history) → 移除（隱藏）refused (422 held) → info dialog.
    dialog = _open_delete_dialog(page, "2330")
    expect(dialog.get_by_role("button", name="永久移除", exact=True)).to_be_disabled()
    dialog.get_by_role("button", name="移除（隱藏）", exact=True).click()
    ack = page.get_by_role("button", name="我知道了")
    expect(ack).to_be_visible()
    ack.click()
    expect(_row(page, "2330")).to_have_count(1)

    # 2) CLOSED CLSD: 永久移除 DISABLED (has_history); 移除（隱藏）soft-deletes it (row hides).
    dialog = _open_delete_dialog(page, "CLSD")
    expect(dialog.get_by_role("button", name="永久移除", exact=True)).to_be_disabled()
    dialog.get_by_role("button", name="移除（隱藏）", exact=True).click()
    expect(_row(page, "CLSD")).to_have_count(0)

    # 3) NEVER-TRADED WATCH: 永久移除 type-confirm gate → real purge (row gone entirely).
    dialog = _open_delete_dialog(page, "WATCH")
    purge_btn = dialog.get_by_role("button", name="永久移除", exact=True)
    confirm_in = dialog.locator("input.purge-confirm")
    expect(purge_btn).to_be_disabled()          # gate closed before any typing
    confirm_in.fill("WRONG")
    expect(purge_btn).to_be_disabled()          # a non-matching value keeps it closed
    confirm_in.fill("WATCH")
    expect(purge_btn).to_be_enabled()           # exact match opens the gate
    purge_btn.click()
    expect(_row(page, "WATCH")).to_have_count(0)  # hard-purged (not merely archived)

    # 4) Only CLSD is archived now → toggle surfaces (1); reveal → CLSD reappears dimmed, 已移除.
    toggle = page.locator("#toggle-archived")
    expect(toggle).to_be_visible()
    expect(toggle).to_contain_text("顯示已移除／封存")
    toggle.click()
    expect(_row(page, "CLSD")).to_have_count(1)
    expect(_row(page, "WATCH")).to_have_count(0)  # purge is permanent — no archived row
    expect(page.locator("#inst-body tr.inst-archived .status-tag.archived")).to_have_count(1)

    page.remove_listener("console", _on_console)
    page.remove_listener("pageerror", _on_pageerror)
    # The held soft-delete deliberately 422s; Chromium logs that as a network-level "Failed to
    # load resource … status of 422" console entry (an EXPECTED response, not a JS fault — same
    # class E6 documents for its intentional 401). The CLSD hide + WATCH purge are 200 (no
    # console noise). Tolerate ONLY that exact status; any other console error or ANY uncaught
    # page error still fails the flow.
    real_console = [e for e in console_errors if "status of 422" not in e]
    assert not real_console and not page_errors, (
        f"console errors={real_console!r}; page errors={page_errors!r}"
    )
