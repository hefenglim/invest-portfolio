"""E2E: the 股利 (dividend) 代號 picker (FU-D35, owner 需求六), driven against the REAL stack.

After an account is chosen, activating 代號 lists that account's CURRENTLY-HELD symbols for
point-and-click; a 「顯示已清倉標的」 toggle additionally lists symbols the account historically
held but has since closed (owner 假設 2). Manual typing stays possible as a fallback.

This flow verifies, with ZERO console / page errors:
  * pick account -> the picker lists the HELD symbol (2330), NOT the closed one while the
    toggle is off;
  * checking 「顯示已清倉標的」 reveals the CLOSED symbol (2454) with a 已清倉 tag;
  * clicking a held row FILLS #d-symbol;
  * a normal dividend commit still works after selecting via the picker.
"""

import json
import urllib.request
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from playwright.sync_api import Page, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    """Re-enable loopback sockets PER TEST (pytest-socket re-bans before every test); each flow
    spawns a fresh isolated uvicorn (free-port probe + readiness poll need loopback TCP)."""
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _seed_picker(conn: Any) -> None:
    """Golden scenario (2330 held in tw_broker) + a CLOSED 2454 in the same account.

    2454 (MediaTek): bought 500 @ 800 then fully sold 500 @ 900 -> current shares 0 ->
    classified 'closed' for tw_broker (historically held). This is the toggle target.
    """
    _seed_golden(conn)
    upsert_instrument(conn, Instrument(symbol="2454", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Semiconductors", name="MediaTek", board="TWSE"))
    insert_transaction(conn, account_id="tw_broker", symbol="2454", side=Side.BUY,
                       quantity=Decimal("500"), price=Decimal("800"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 6))
    insert_transaction(conn, account_id="tw_broker", symbol="2454", side=Side.SELL,
                       quantity=Decimal("500"), price=Decimal("900"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 2, 6))
    conn.commit()


def _get_json(base_url: str, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(base_url + path, timeout=5) as r:  # noqa: S310 (loopback)
        data: dict[str, Any] = json.loads(r.read().decode("utf-8"))
        return data


def _sink(page: Page) -> tuple[list[str], list[str]]:
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(getattr(m, "text", ""))
            if getattr(m, "type", None) == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    return console_errors, page_errors


@pytest.mark.e2e
def test_dividend_symbol_picker_held_closed_and_commit(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_picker)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    # sanity: the endpoint classifies 2330 held + 2454 closed for tw_broker.
    holdings = _get_json(base, "/api/input/holdings?account=tw_broker")
    assert {h["symbol"] for h in holdings["held"]} == {"2330"}
    assert {h["symbol"] for h in holdings["closed"]} == {"2454"}

    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#d-account option", state="attached")
    page.click("#tab-div")
    page.wait_for_selector("#d-symbol", state="visible")

    # --- pick the account; open the picker by activating 代號 ---------------------------
    page.select_option("#d-account", "tw_broker")
    page.wait_for_selector("#d-tw", state="visible")
    page.click("#d-symbol")
    page.wait_for_selector("#d-sym-picker", state="visible")
    # held symbol listed; closed footer offered (there IS closed history)…
    page.wait_for_selector("#d-sym-list button:has-text('2330')")
    # Wave C: the dividend picker's held rows now ALSO carry the 股數 + 均價 annotation
    # (parity with the manual picker via the shared component).
    expect(page.locator("#d-sym-list button:has-text('2330')")).to_contain_text("均價")
    page.wait_for_selector("#d-sym-foot", state="visible")
    # …but the closed symbol is NOT shown while the toggle is off.
    assert page.locator("#d-sym-list button:has-text('2454')").count() == 0

    # --- toggle 顯示已清倉標的 -> the closed symbol appears with a 已清倉 tag --------------
    page.check("#d-sym-closed-toggle")
    page.wait_for_selector("#d-sym-list button:has-text('2454')")
    assert page.locator("#d-sym-list button:has-text('已清倉')").count() >= 1

    # --- click the HELD row -> #d-symbol is filled; the picker closes -------------------
    page.click("#d-sym-list button:has-text('2330')")
    assert page.input_value("#d-symbol") == "2330"
    page.wait_for_selector("#d-sym-picker", state="hidden")

    # --- a normal dividend commit still works after selecting via the picker ------------
    page.fill("#d-date", "2026-07-10")
    page.fill("#d-tw-gross", "3000")
    page.fill("#d-tw-net", "3000")
    with page.expect_response("**/api/import/commit") as cm:
        page.click("#d-confirm")
    assert cm.value.status == 200, f"dividend commit status {cm.value.status}"
    page.wait_for_selector(".toast-ok")

    assert not console_errors and not page_errors, (
        f"dividend picker flow: console={console_errors!r} page={page_errors!r}"
    )
