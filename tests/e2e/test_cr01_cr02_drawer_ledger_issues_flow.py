"""E2E (CR-01 / CR-02, 2026-10-04): the drawer names the two ledger problems no holding row can.

* A 賣超 that a later buy of the same size netted back to EXACTLY zero shares has no holding
  row, so no 賣超 chip, and its 交易明細 footer reconciles 0 = 0 — the drawer read clean over a
  discarded basis and a sell with no realized row. The cause line now sits under the footer.
* A dividend the account never held the symbol for is listed in 配息史 (it IS a ledger row) but
  the book refused it — the line under 配息史 says so.

Both pages must load at all: before the fix the orphan dividend took ``GET /api/dashboard``
(and every drawer, built on it) down with a 500. Checked at 390px for overflow too — these are
long zh sentences, the class of addition that has overflowed this drawer before.
"""

import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest
from playwright.sync_api import Page, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.data_ingestion.store import (
    insert_dividend,
    insert_transaction,
    upsert_instrument,
)
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import GOLDEN_NOW, _seed_golden
from tests.e2e.conftest import FlowServerFactory


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    """Re-enable loopback sockets PER TEST (pytest-socket re-bans before every test)."""
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _seed(conn: sqlite3.Connection) -> None:
    """Golden + tw_broker 2454: sell 1,000 with nothing held, then buy 1,000 (flat 賣超) +
    tw_broker 2317: a cash dividend for a symbol the account never held (a stored orphan)."""
    _seed_golden(conn)
    for sym, name in (("2454", "MediaTek"), ("2317", "Hon Hai")):
        upsert_instrument(conn, Instrument(symbol=sym, market=Market.TW,
                                           quote_ccy=Currency.TWD, sector="Electronics",
                                           name=name, board="TWSE"))
    for side, qty, price, day in ((Side.SELL, "1000", "1000", date(2026, 2, 2)),
                                  (Side.BUY, "1000", "950", date(2026, 3, 2))):
        insert_transaction(conn, account_id="tw_broker", symbol="2454", side=side,
                           quantity=Decimal(qty), price=Decimal(price), fees=Decimal("0"),
                           tax=Decimal("0"), trade_date=day)
    insert_dividend(conn, account_id="tw_broker", symbol="2317", div_date=date(2026, 4, 1),
                    div_type="CASH", gross=Decimal("500"), withholding=Decimal("0"),
                    net=Decimal("500"))
    upsert_prices(conn, [
        PriceRow(instrument=s, market=Market.TW, as_of=date(2026, 6, 9),
                 close=Decimal("100"), source="test") for s in ("2454", "2317")
    ], fetched_at=GOLDEN_NOW)
    conn.commit()


@pytest.mark.e2e
def test_the_drawer_names_a_flat_oversell_and_an_orphan_dividend(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed)
    page = fresh_page
    page_errors: list[str] = []
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    bad: list[str] = []
    page.on("response", lambda r: bad.append(r.url) if r.status >= 500 else None)

    page.goto(base + "/index.html", wait_until="load")
    page.wait_for_selector(".kpi-card")

    # ===== the flat 賣超: a cause line under the (green) 交易明細 footer =====================
    with page.expect_response("**/api/symbol/2454/detail") as resp:
        page.evaluate("() => window.pdOpenSymbol('2454')")
    assert resp.value.status == 200, f"detail status {resp.value.status}"
    page.wait_for_selector(".sd-tx-section .sd-tx-reconcile")
    issue = page.locator(".sd-tx-section .sd-tx-issue", has_text="賣超待釐清")
    expect(issue).to_have_count(1)
    expect(issue).to_contain_text("2026-02-02 的 2454 賣出 1000 股，超過當日持股 0 股")
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(150)
    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert overflow <= 0, f"drawer overflows by {overflow}px at 390 with the 賣超 line"
    page.set_viewport_size({"width": 1280, "height": 900})

    # ===== the orphan dividend: listed in 配息史, refused by the book, and said so ==========
    with page.expect_response("**/api/symbol/2317/detail") as resp:
        page.evaluate("() => window.pdOpenSymbol('2317')")
    assert resp.value.status == 200, f"detail status {resp.value.status}"
    line = page.locator(".sd-ledger-issue")
    expect(line).to_have_count(1)
    expect(line).to_contain_text("股利無對應持倉")
    expect(line).to_contain_text("2317")
    colour = line.evaluate("el => getComputedStyle(el).color")
    up = page.evaluate(
        "() => { const p = document.createElement('span');"
        " p.style.color = 'var(--up)'; document.body.appendChild(p);"
        " const c = getComputedStyle(p).color; p.remove(); return c; }")
    assert colour == up, (colour, up)          # the error colour, from the stylesheet class

    # The drawer's 試算 posts on open and the strict replay may refuse a 賣超 ledger there —
    # a pre-existing exception the drawer suite documents; every OTHER 5xx fails this test.
    unexpected = [u for u in bad if not u.endswith("/api/whatif")]
    assert not unexpected and not page_errors, (
        f"drawer ledger issues: unexpected 5xx={unexpected!r} page={page_errors!r}")


def _seed_orphan_reinvest(conn: sqlite3.Connection) -> None:
    """Golden + tw_broker 2317: ONLY a 配股 row — 50 shares — for a symbol never held there."""
    _seed_golden(conn)
    upsert_instrument(conn, Instrument(symbol="2317", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Electronics", name="Hon Hai", board="TWSE"))
    insert_dividend(conn, account_id="tw_broker", symbol="2317", div_date=date(2026, 4, 1),
                    div_type="STOCK", gross=Decimal("0"), withholding=Decimal("0"),
                    net=Decimal("0"), reinvest_shares=Decimal("50"))
    conn.commit()


@pytest.mark.e2e
def test_a_symbol_whose_only_rows_are_refused_reinvest_dividends(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """R8 (round 2): 交易明細 lists a received 配股 row, so `activity` is not empty — and the
    drawer read that as 「帳本有交易紀錄，部位已結清」 over a position that never existed. The
    orphan-only state is decided on whether anything OPENED a position, and the red footer
    (the SQL share path counts the 50 shares, the book does not) carries its cause."""
    base = flow_server(_seed_orphan_reinvest)
    page = fresh_page
    page_errors: list[str] = []
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    page.goto(base + "/index.html", wait_until="load")
    page.wait_for_selector(".kpi-card")
    with page.expect_response("**/api/symbol/2317/detail") as resp:
        page.evaluate("() => window.pdOpenSymbol('2317')")
    assert resp.value.status == 200, f"detail status {resp.value.status}"
    page.wait_for_selector(".sd-drawer .sd-section")
    expect(page.locator(".sd-drawer")).not_to_contain_text("部位已結清")
    expect(page.locator(".sd-drawer .sd-empty",
                        has_text="帳本只有無法入帳的股利紀錄")).to_have_count(1)
    expect(page.locator(".sd-ledger-issue")).to_have_count(1)       # under 配息史
    page.wait_for_selector(".sd-tx-section .sd-tx-reconcile")
    expect(page.locator(".sd-tx-section .sd-tx-reconcile")).to_contain_text("⚠ 對帳不一致")
    cause = page.locator(".sd-tx-section .sd-tx-issue", has_text="股利無對應持倉")
    expect(cause).to_have_count(1)                                  # never a red footer alone
    assert not page_errors, page_errors
