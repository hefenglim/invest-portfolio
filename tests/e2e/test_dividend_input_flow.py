"""E2E: the 股利 (dividend) input pane — every model + every branch, driven against the
REAL stack (FU-D21).

Drives the four dividend models through the actual trades.html form + the one-row-CSV import
seam, then verifies the DOWNSTREAM numbers via /api/dashboard + the cash statement, asserting
EXACT Decimal-strings computed by hand against domain-ledger.md:

  * TW CASH   → adjusted cost DROPS by the net; original untouched; a cash-statement line.
  * TW STOCK  → shares increase (配股), NO cash line, cost totals unchanged (exercises the
                現金股利/配股 segmented switch — the id-contract fix, see below).
  * DRIP (US) → 30% withholding surfaces; net reinvested as $0-cost shares; adjusted NOT
                reduced.
  * MY NET    → adjusted cost drops by the net received.

Also exercises model-block switching (account change reveals the tw/drip/net block) and the
現金股利⇄配股 toggle in BOTH directions, all with ZERO console / page errors.

Defect fixed in-wave (FU-D21): the tw 現金股利/配股 segmented buttons + the Gross-label / Net-
field were bound in input.js by ids (#d-type-stock / #d-tw-gross-label / #d-tw-net-field) that
never existed in trades.html — so clicking either button threw a TypeError and 配股 could never
be submitted. The ids were added to the markup; this flow is the regression guard.
"""

import json
import urllib.request
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from playwright.sync_api import Page
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


def _seed_dividends(conn: Any) -> None:
    """Golden scenario + two extra holdings so every dividend model has a target:

    * 2330 / tw_broker  — TW CASH (from golden; already carries a 5,000 net CASH dividend).
    * 2454 / tw_broker  — TW STOCK 配股 (fresh, never-dividended; 500 sh @ 800 = 400,000).
    * AAPL / schwab     — DRIP (from golden; 10 sh @ 100 = 1,000).
    * 1155 / moomoo_my — MY NET (fresh; 1,000 sh @ 9 MYR = 9,000).
    """
    _seed_golden(conn)
    upsert_instrument(conn, Instrument(symbol="2454", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Semiconductors", name="MediaTek", board="TWSE"))
    insert_transaction(conn, account_id="tw_broker", symbol="2454", side=Side.BUY,
                       quantity=Decimal("500"), price=Decimal("800"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 6))
    upsert_instrument(conn, Instrument(symbol="1155", market=Market.MY, quote_ccy=Currency.MYR,
                                       sector="Banks", name="Maybank", board=".KL"))
    insert_transaction(conn, account_id="moomoo_my", symbol="1155", side=Side.BUY,
                       quantity=Decimal("1000"), price=Decimal("9"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 7))
    conn.commit()


def _get_json(base_url: str, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(base_url + path, timeout=5) as r:  # noqa: S310 (loopback)
        data: dict[str, Any] = json.loads(r.read().decode("utf-8"))
        return data


def _holding(base: str, symbol: str, account_id: str) -> dict[str, Any] | None:
    rows: list[dict[str, Any]] = _get_json(base, "/api/dashboard")["holdings"]
    for h in rows:
        if h["symbol"] == symbol and h["account_id"] == account_id:
            return h
    return None


def _dividend_line_count(base: str, account: str, ccy: str) -> int:
    stmt = _get_json(base, f"/api/cash/statement?account={account}&ccy={ccy}")
    return sum(1 for r in stmt["rows"] if r.get("kind") == "dividend")


def _sink(page: Page) -> tuple[list[str], list[str]]:
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(getattr(m, "text", ""))
            if getattr(m, "type", None) == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    return console_errors, page_errors


def _open_div_tab(page: Page, base: str) -> None:
    page.goto(base + "/trades.html", wait_until="load")
    # boot done once initDiv() has populated the dividend account dropdown.
    page.wait_for_selector("#d-account option", state="attached")
    page.click("#tab-div")
    page.wait_for_selector("#d-symbol", state="visible")


def _commit_dividend(page: Page) -> None:
    """Click 確認寫入 and wait for the one-row import commit to land (200)."""
    with page.expect_response("**/api/import/commit") as cm:
        page.click("#d-confirm")
    assert cm.value.status == 200, f"dividend commit status {cm.value.status}"
    page.wait_for_selector(".toast-ok")


@pytest.mark.e2e
def test_dividend_all_models_downstream_numbers(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_dividends)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    # --- baselines (hand-computed against domain-ledger.md) --------------------------------
    tw = _holding(base, "2330", "tw_broker")
    assert tw is not None
    assert Decimal(tw["adjusted_cost_total"]) == Decimal("495000")   # 500000 − 5000 golden div
    assert Decimal(tw["original_cost_total"]) == Decimal("500000")
    aapl = _holding(base, "AAPL", "schwab")
    assert aapl is not None and Decimal(aapl["shares"]) == Decimal("10")
    assert Decimal(aapl["adjusted_cost_total"]) == Decimal("1000")

    _open_div_tab(page, base)

    # ============ (1) TW CASH — adjusted cost drops by the net; a cash line appears =========
    tw_div_before = _dividend_line_count(base, "tw_broker", "TWD")
    page.select_option("#d-account", "tw_broker")
    page.wait_for_selector("#d-tw", state="visible")           # model-block: tw
    page.fill("#d-symbol", "2330")
    page.fill("#d-date", "2026-07-10")
    page.fill("#d-tw-gross", "3000")
    page.fill("#d-tw-net", "3000")
    _commit_dividend(page)

    tw_after = _holding(base, "2330", "tw_broker")
    assert tw_after is not None
    assert Decimal(tw_after["adjusted_cost_total"]) == Decimal("492000")  # 495000 − 3000 net
    assert Decimal(tw_after["original_cost_total"]) == Decimal("500000")  # original untouched
    assert Decimal(tw_after["shares"]) == Decimal("1000")
    assert _dividend_line_count(base, "tw_broker", "TWD") == tw_div_before + 1

    # ============ (2) TW STOCK 配股 — shares up, NO cash line, cost totals unchanged =========
    # Clicking 配股 used to throw (missing ids) — the id-contract fix makes this branch work.
    page.select_option("#d-account", "tw_broker")
    page.fill("#d-symbol", "2454")
    page.click("#d-type-stock")
    page.wait_for_function(
        "() => document.querySelector('#d-tw-gross-label').textContent === '配股股數'"
    )
    page.wait_for_selector("#d-tw-net-field", state="hidden")   # Net hidden in 配股 mode
    page.fill("#d-tw-gross", "100")                             # 配股股數 (reuses the Gross input)
    _commit_dividend(page)

    stock = _holding(base, "2454", "tw_broker")
    assert stock is not None
    assert Decimal(stock["shares"]) == Decimal("600")                    # 500 + 100 配股
    assert Decimal(stock["original_cost_total"]) == Decimal("400000")    # cost unchanged
    assert Decimal(stock["adjusted_cost_total"]) == Decimal("400000")
    # 配股 adds NO cash line (STOCK is a share event, not a cash dividend).
    assert _dividend_line_count(base, "tw_broker", "TWD") == tw_div_before + 1

    # toggle BACK to 現金股利 — the segmented switch works both ways (defect regression).
    page.click("#d-type-cash")
    page.wait_for_function(
        "() => document.querySelector('#d-tw-gross-label').textContent === 'Gross（總額）'"
    )
    page.wait_for_selector("#d-tw-net-field", state="visible")

    # ============ (3) DRIP — 30% withholding; net reinvested as $0-cost shares =============
    page.select_option("#d-account", "schwab")
    page.wait_for_selector("#d-drip", state="visible")         # model-block: drip
    page.wait_for_selector("#d-tw", state="hidden")
    page.fill("#d-symbol", "AAPL")
    page.fill("#d-drip-gross", "100")
    # the input-side estimate fills the readonly withholding/net previews (30% of 100).
    page.wait_for_function("() => document.querySelector('#d-drip-wh').value === '30.00'")
    page.wait_for_function("() => document.querySelector('#d-drip-net').value === '70.00'")
    page.fill("#d-drip-shares", "0.5")
    page.fill("#d-drip-price", "140")
    _commit_dividend(page)

    drip = _holding(base, "AAPL", "schwab")
    assert drip is not None
    assert Decimal(drip["shares"]) == Decimal("10.5")                    # 10 + 0.5 $0-cost
    assert Decimal(drip["adjusted_cost_total"]) == Decimal("1000")       # NOT reduced (DRIP)
    assert Decimal(drip["original_cost_total"]) == Decimal("1000")
    # the 30% withholding is recorded on the ledger row (server-computed, not from the UI).
    divs = _get_json(base, "/api/ledgers/dividends?account_id=schwab&symbol=AAPL")["rows"]
    drip_rows = [d for d in divs if d["type"] == "drip"]
    assert len(drip_rows) == 1
    assert Decimal(drip_rows[0]["withhold"]) == Decimal("30")            # 100 × 30%
    assert Decimal(drip_rows[0]["net"]) == Decimal("70")
    assert Decimal(drip_rows[0]["reinvest_shares"]) == Decimal("0.5")

    # ============ (4) MY NET — adjusted cost drops by the net received ======================
    # Batch B (Moomoo merge): moomoo_my is now a DUAL-market account, so the dividend model
    # follows the ENTERED SYMBOL's market (F01), not an account scalar. #d-symbol still holds
    # the prior step's US "AAPL" here, which resolves to the US→DRIP block — so the MY symbol
    # must be entered FIRST for the form to switch to the MY→NET block (the pre-merge premise
    # of "select moomoo_my ⇒ NET" no longer holds).
    my_div_before = _dividend_line_count(base, "moomoo_my", "MYR")
    page.select_option("#d-account", "moomoo_my")
    page.fill("#d-symbol", "1155")                             # MY symbol picks the NET model
    page.wait_for_selector("#d-net", state="visible")          # model-block: net
    page.wait_for_selector("#d-drip", state="hidden")
    page.fill("#d-net-amt", "200")
    _commit_dividend(page)

    my = _holding(base, "1155", "moomoo_my")
    assert my is not None
    assert Decimal(my["adjusted_cost_total"]) == Decimal("8800")         # 9000 − 200 net
    assert Decimal(my["original_cost_total"]) == Decimal("9000")         # original untouched
    assert Decimal(my["shares"]) == Decimal("1000")
    assert _dividend_line_count(base, "moomoo_my", "MYR") == my_div_before + 1

    assert not console_errors and not page_errors, (
        f"dividend input flow: console={console_errors!r} page={page_errors!r}"
    )
