"""E2E: FU-D44 sell-entry hints + FU-D45 ledger live refresh, driven against the REAL stack.

FU-D44 — manual pane, side=sell + a registered symbol chosen: under 股數 a clickable
「可賣 {shares} 股」 and under 價格 a clickable 「持有均價 {adjusted_avg}」. Both values are
SERVER-computed (GET /api/input/holdings — shares via current_shares, adjusted_avg via the
verified build_book cost-basis replay) and click-fill the RAW Decimal wire string; the
golden book's 2330 value is DIVIDEND-ADJUSTED (495 = (500,000 − 5,000 div) / 1,000), so the
displayed number proves the real cost-basis path. Not held here -> muted 此帳戶無持股; buy
side -> hidden.

FU-D45 — after a successful manual commit the lower 帳本記錄 交易 tab gains the new row
IN PLACE (no reload — asserted via a window marker that a navigation would wipe), the
holdings-hint cache is invalidated (可賣 reflects the just-committed buy), and a dividend
commit refreshes the 股利 tab the same way.

All with ZERO console / page errors.
"""

import json
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest
from playwright.sync_api import Page
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

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
def test_sell_hints_show_fill_and_hide(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_golden)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    # sanity: the extended holdings read carries the FU-D44 fields for the golden book.
    holdings = _get_json(base, "/api/input/holdings?account=tw_broker")
    (h2330,) = [h for h in holdings["held"] if h["symbol"] == "2330"]
    assert h2330["shares"] == "1000"
    assert h2330["adjusted_avg"] == "495"   # (500,000 − 5,000 div) / 1,000 — dividend-adjusted

    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#m-account option", state="attached")
    page.select_option("#m-account", "tw_broker")
    page.fill("#m-symbol", "2330")

    # buy side (the default): hints stay hidden even with a held symbol chosen.
    page.wait_for_selector("#m-sym-hint:has-text('TSMC')")
    assert page.locator("#m-shares-hint").is_hidden()
    assert page.locator("#m-price-hint").is_hidden()

    # sell side: 可賣 + 持有均價 appear (server Decimal strings, formatted via window.fmt).
    page.click("#m-side-sell")
    page.wait_for_selector("#m-shares-hint button:has-text('可賣 1,000 股')")
    page.wait_for_selector("#m-price-hint button:has-text('持有均價 495.00')")

    # click-fill: the RAW wire values land in the fields (no thousands separators).
    page.click("#m-shares-hint button")
    assert page.input_value("#m-shares") == "1000"
    page.click("#m-price-hint button")
    assert page.input_value("#m-price") == "495"

    # registered but NOT held in this account -> muted note, no fill buttons.
    page.fill("#m-symbol", "AAPL")
    page.wait_for_selector("#m-shares-hint:has-text('此帳戶無持股')")
    assert page.locator("#m-shares-hint button").count() == 0
    assert page.locator("#m-price-hint").is_hidden()

    # account switch re-scopes the hints (per-account fetch): AAPL IS held in schwab.
    page.select_option("#m-account", "schwab")
    page.wait_for_selector("#m-shares-hint button:has-text('可賣 10 股')")
    page.wait_for_selector("#m-price-hint button:has-text('持有均價 100.00')")

    # back to buy -> everything hides.
    page.click("#m-side-buy")
    page.wait_for_selector("#m-shares-hint", state="hidden")
    page.wait_for_selector("#m-price-hint", state="hidden")

    assert not console_errors and not page_errors, (
        f"sell hints flow: console={console_errors!r} page={page_errors!r}"
    )


@pytest.mark.e2e
def test_ledger_live_refresh_after_commits(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_golden)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#m-account option", state="attached")
    page.wait_for_selector("#tx-body tr.expandable")
    tx_before = page.locator("#tx-body tr.expandable").count()

    # marker: a full reload/navigation would wipe it — proves the refresh is IN PLACE.
    page.evaluate("window.__pd_same_document = 1")

    # ---- manual buy commit -> the 交易 tab gains the row without a reload -------------
    page.select_option("#m-account", "tw_broker")
    page.fill("#m-symbol", "2330")
    page.fill("#m-shares", "100")
    page.fill("#m-price", "600")
    page.wait_for_function("() => !document.querySelector('#m-confirm').disabled")
    with page.expect_response("**/api/input/manual/commit") as cm:
        page.click("#m-confirm")
    assert cm.value.status == 201, f"manual commit status {cm.value.status}"
    page.wait_for_selector(".toast-ok")
    page.wait_for_function(
        f"() => document.querySelectorAll('#tx-body tr.expandable').length === {tx_before + 1}"
    )
    assert page.evaluate("window.__pd_same_document") == 1

    # ---- the holdings-hint cache was invalidated: 可賣 includes the just-bought 100 ----
    page.click("#m-side-sell")
    page.wait_for_selector("#m-shares-hint button:has-text('可賣 1,100 股')")

    # ---- dividend commit -> the 股利 tab refreshes in place too ------------------------
    page.click("#tab-ldiv")                       # lower ledger: activate the 股利 pane
    page.wait_for_selector("#div-body tr")
    div_before = page.locator("#div-body tr").count()
    page.click("#tab-div")                        # upper input: the dividend form
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
    page.wait_for_function(
        f"() => document.querySelectorAll('#div-body tr').length === {div_before + 1}"
    )
    assert page.evaluate("window.__pd_same_document") == 1

    assert not console_errors and not page_errors, (
        f"ledger live refresh flow: console={console_errors!r} page={page_errors!r}"
    )


@pytest.mark.e2e
def test_draft_preview_position_whatif_and_cash_line(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """R6-E: the 草稿預覽 card gains the drawer-parity 試算 what-if rows + a display-only
    account-cash line, both SERVER-computed (Decimal strings via window.fmt). A SELL draft on
    the seeded 2330 holding shows 調整成本移除 / 已實現損益 / 剩餘股數 and the 該帳戶現金 line;
    switching to BUY swaps in 新持股 / 新原始均價 / 新調整均價 — all with ZERO page errors."""
    base = flow_server(_seed_golden)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#m-account option", state="attached")
    page.select_option("#m-account", "tw_broker")
    page.fill("#m-symbol", "2330")

    # SELL draft → the position what-if rows + the account-cash line render in the card.
    page.click("#m-side-sell")
    page.fill("#m-shares", "500")
    page.fill("#m-price", "600")
    page.wait_for_selector("#m-pc-rows .pc-row:has-text('調整成本移除')")
    page.wait_for_selector("#m-pc-rows .pc-row:has-text('已實現損益')")
    page.wait_for_selector("#m-pc-rows .pc-row:has-text('剩餘股數')")
    page.wait_for_selector("#m-pc-rows .pc-row:has-text('該帳戶現金（TWD）')")

    # BUY draft → the what-if swaps to the buy rows; the cash line stays.
    page.click("#m-side-buy")
    page.wait_for_selector("#m-pc-rows .pc-row:has-text('新原始均價')")
    page.wait_for_selector("#m-pc-rows .pc-row:has-text('新調整均價')")
    page.wait_for_selector("#m-pc-rows .pc-row:has-text('該帳戶現金（TWD）')")

    assert not console_errors and not page_errors, (
        f"draft preview flow: console={console_errors!r} page={page_errors!r}"
    )
