"""E2E (Wave A4): the manual-tab grouped 代號 picker (#8) + the ledger commit funnel (#10),
driven against the REAL stack.

#8 — the manual pane's free-text datalist is replaced by a grouped dropdown modeled on the
dividend picker: symbols are grouped 已持有 / 未持有, MARKET-FILTERED by the selected account
(a US symbol never shows on a TW-only account; a MERGED Moomoo account unions its markets and
tags each 未持有 row). Selecting a row still drives the SAME preview pipeline; a footer
「＋新增標的」 opens the shared quick-add dialog and auto-selects the newly-registered symbol.

#10 — after ANY successful commit the funnel (afterCommitRefresh → highlightCommitted)
auto-switches the lower 帳本記錄 tab to the committed kind and soft-pulses the new top row
(.ledger-added-row, wn-flash-pulse ~8×). A dividend commit flashes the 股利 table (proving the
flash is no longer manual/tx-only); an opening commit flashes the 期初庫存 table. Under
prefers-reduced-motion the pulse is disabled. #9 — the draft-preview label reads 交易後現金.

All with ZERO console / page errors.
"""

import json
from collections.abc import Iterator
from typing import Any

import pytest
from playwright.sync_api import Page, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.data_ingestion.store import upsert_instrument
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
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
    """Golden scenario + a SECOND TW instrument (2317) that is registered but NOT held, so
    tw_broker shows BOTH groups: 已持有 [2330] and 未持有 [2317]. AAPL (US, held in schwab)
    stays the market-filter probe — it must be hidden on the TW account and shown (tagged US)
    on the merged moomoo_my account."""
    _seed_golden(conn)
    upsert_instrument(conn, Instrument(symbol="2317", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Electronics", name="Hon Hai", board="TWSE"))
    conn.commit()


def _sink(page: Page) -> tuple[list[str], list[str]]:
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(getattr(m, "text", ""))
            if getattr(m, "type", None) == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    return console_errors, page_errors


@pytest.mark.e2e
def test_manual_grouped_picker_groups_and_market_filter(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_picker)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#m-account option", state="attached")

    # ---- TW-only account: 已持有 [2330] + 未持有 [2317]; US AAPL is market-filtered out ----
    page.select_option("#m-account", "tw_broker")
    page.click("#m-symbol")
    page.wait_for_selector("#m-sym-picker", state="visible")
    page.wait_for_selector("#m-sym-list:has-text('已持有')")
    page.wait_for_selector("#m-sym-list:has-text('未持有')")
    # held row carries the 股數 + 均價 annotation (server Decimal strings via fmt).
    expect(page.locator("#m-sym-list button:has-text('2330')")).to_contain_text("均價")
    page.wait_for_selector("#m-sym-list button:has-text('2317')")   # un-held, same TW market
    # AAPL (US) is market-filtered OFF a TW-only account.
    assert page.locator("#m-sym-list button:has-text('AAPL')").count() == 0

    # selecting a row FILLS #m-symbol + drives the existing preview pipeline (hint resolves).
    page.click("#m-sym-list button:has-text('2330')")
    assert page.input_value("#m-symbol") == "2330"
    page.wait_for_selector("#m-sym-picker", state="hidden")
    page.wait_for_selector("#m-sym-hint:has-text('TSMC')")

    # ---- merged (US+MY) account: AAPL shows in 未持有 WITH a market tag; TW symbols hidden ----
    page.select_option("#m-account", "moomoo_my")   # onManualAccountChange closes the picker
    page.wait_for_selector("#m-sym-picker", state="hidden")
    # clear the leftover 2330 filter (the symbol persists across an account switch, matching the
    # dividend picker); the empty-query fill re-focuses and reopens the picker for this account.
    page.fill("#m-symbol", "")
    page.wait_for_selector("#m-sym-picker", state="visible")
    page.wait_for_selector("#m-sym-list button:has-text('AAPL')")
    # the 未持有 row carries a market tag on a merged account.
    expect(page.locator("#m-sym-list button:has-text('AAPL')")).to_contain_text("US")
    # TW symbols are hidden on the US+MY merged account.
    assert page.locator("#m-sym-list button:has-text('2330')").count() == 0
    assert page.locator("#m-sym-list button:has-text('2317')").count() == 0

    assert not console_errors and not page_errors, (
        f"grouped picker flow: console={console_errors!r} page={page_errors!r}"
    )


def _lookup_found() -> str:
    return json.dumps({
        "found": True, "registered": False, "archived": False,
        "name": "New Co", "sector": "", "board": "", "is_etf": False,
    })


@pytest.mark.e2e
def test_manual_picker_add_new_registers_and_selects(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """「＋新增標的」 opens the shared quick-add dialog; confirming force-registers the symbol
    (REAL POST /api/instruments), the context reloads, and the new symbol lands in 未持有 and
    is AUTO-SELECTED into #m-symbol — ready to trade with zero re-entry."""
    base = flow_server(_seed_picker)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    # canned lookup so the dialog's 確認 enables (the register POST hits the REAL endpoint).
    page.route("**/api/instruments/lookup**",
               lambda route: route.fulfill(status=200, content_type="application/json",
                                           body=_lookup_found()))

    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#m-account option", state="attached")
    page.select_option("#m-account", "schwab")   # USD account -> market inferred US
    page.click("#m-symbol")
    page.wait_for_selector("#m-sym-picker", state="visible")

    # the footer 「＋新增標的」 opens the shared dialog (symbol editable, market from account).
    page.click("#m-sym-addnew")
    dialog = page.locator(".modal-backdrop").last
    sym_input = dialog.locator("input.qa-symbol")
    sym_input.wait_for(state="visible")
    sym_input.fill("NEWCO")
    confirm = dialog.get_by_role("button", name="確認", exact=True)
    expect(confirm).to_be_enabled()
    confirm.click()

    # the REAL register POST (force-registers even quote-less) can stall on a network-restricted
    # runner's provider probe -> generous ceiling. After it, the new symbol is auto-selected.
    expect(page.locator("#m-symbol")).to_have_value("NEWCO", timeout=30000)
    # …and it now resolves in 未持有 (reopen the picker to prove the reload landed).
    page.click("#m-symbol")
    page.wait_for_selector("#m-sym-picker", state="visible")
    page.wait_for_selector("#m-sym-list button:has-text('NEWCO')")

    assert not console_errors and not page_errors, (
        f"add-new flow: console={console_errors!r} page={page_errors!r}"
    )


@pytest.mark.e2e
def test_commit_flash_and_autoswitch_per_kind(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """#10 — a DIVIDEND commit auto-switches the lower ledger to 股利 (pane-ldiv active) and
    flashes its newest row (.ledger-added-row) — proving the flash is no longer tx-only; an
    OPENING commit does the same for 期初庫存 (pane-lopen). #9 — a buy draft's cash line reads
    交易後現金."""
    base = flow_server(_seed_picker)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#m-account option", state="attached")

    # #9: the buy-draft cash line label reads 交易後現金 (renamed from 扣款後現金).
    page.select_option("#m-account", "tw_broker")
    page.fill("#m-symbol", "2330")
    page.fill("#m-shares", "100")
    with page.expect_response("**/api/input/manual/preview"):
        page.fill("#m-price", "600")
    page.wait_for_selector("#m-pc-rows .pc-row:has-text('交易後現金（TWD）')")

    # ---- dividend commit -> lower ledger auto-switches to 股利 + top row flashes -------------
    page.click("#tab-div")
    page.wait_for_selector("#d-symbol", state="visible")
    page.select_option("#d-account", "tw_broker")
    page.wait_for_selector("#d-tw", state="visible")
    page.fill("#d-symbol", "2330")
    page.fill("#d-date", "2026-07-10")
    page.fill("#d-tw-gross", "3000")
    page.fill("#d-tw-net", "3000")
    with page.expect_response("**/api/import/commit") as dm:
        page.click("#d-confirm")
    assert dm.value.status == 200, f"dividend commit status {dm.value.status}"
    page.wait_for_selector("#pane-ldiv.active")                 # auto-switched to the 股利 tab
    page.wait_for_selector("#div-body tr.ledger-added-row")     # newest 股利 row pulses

    # ---- opening commit -> lower ledger auto-switches to 期初庫存 + top row flashes ----------
    page.click("#tab-fxopen")
    page.wait_for_selector("#o-account", state="visible")
    page.select_option("#o-account", "tw_broker")
    page.fill("#o-symbol", "2317")
    page.fill("#o-shares", "100")
    page.fill("#o-total", "50000")
    page.fill("#o-date", "2026-01-02")
    with page.expect_response("**/api/import/commit") as om:
        page.click("#o-confirm")
    assert om.value.status == 200, f"opening commit status {om.value.status}"
    page.wait_for_selector("#pane-lopen.active")                # auto-switched to the 期初庫存 tab
    page.wait_for_selector("#open-body tr.ledger-added-row")    # newest 期初 row pulses

    assert not console_errors and not page_errors, (
        f"flash/auto-switch flow: console={console_errors!r} page={page_errors!r}"
    )


@pytest.mark.e2e
def test_reduced_motion_disables_flash_pulse(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """Under prefers-reduced-motion:reduce the .ledger-added-row rule resolves to
    animation:none — the highlight is still applied (so nothing else breaks) but does not
    animate. Discriminator: getComputedStyle(row).animationName === 'none'."""
    base = flow_server(_seed_picker)
    page = fresh_page
    page.emulate_media(reduced_motion="reduce")
    console_errors, page_errors = _sink(page)

    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#d-account option", state="attached")
    page.click("#tab-div")
    page.wait_for_selector("#d-symbol", state="visible")
    page.select_option("#d-account", "tw_broker")
    page.wait_for_selector("#d-tw", state="visible")
    page.fill("#d-symbol", "2330")
    page.fill("#d-date", "2026-07-11")
    page.fill("#d-tw-gross", "1500")
    page.fill("#d-tw-net", "1500")
    with page.expect_response("**/api/import/commit") as dm:
        page.click("#d-confirm")
    assert dm.value.status == 200, f"dividend commit status {dm.value.status}"

    row = page.wait_for_selector("#div-body tr.ledger-added-row")   # class still applied…
    anim = page.evaluate(
        "() => getComputedStyle(document.querySelector('#div-body tr.ledger-added-row'))"
        ".animationName"
    )
    assert anim == "none", f"reduced-motion should disable the pulse, got animationName={anim!r}"
    assert row is not None

    assert not console_errors and not page_errors, (
        f"reduced-motion flow: console={console_errors!r} page={page_errors!r}"
    )
