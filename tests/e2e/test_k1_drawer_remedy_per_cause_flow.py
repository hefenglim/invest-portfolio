"""E2E (K1 / K3, owner rulings 2026-10-05): the drawer offers the RIGHT remedy for each cause of a
red 交易明細 footer, and names a refused dividend on a FLAT carrier.

Door 2 offered 補登公司行動 beside every ⚠ 對帳不一致. A DRIP / 配股 the book REFUSED still adds
its shares on the ledger's share path, so the footer is red — but a corporate action is not the
repair:

* an orphan dividend (no position by its date) → 補登該日（含）之前的買進或期初庫存，或刪除該筆股利;
* a dividend on a ticker an EXCHANGE vacated (E24) → record the payment as a cash movement and
  delete the dividend;
* an unapplied corporate action → 補登公司行動, as before.

Mixed causes show each one. And (K3) the position carrying a refused dividend's 待釐清 flag can be
flat — sold out — with no 部位摘要 and no chip: its drawer now names the row under 配息史.
"""

import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest
from playwright.sync_api import Locator, Page, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.data_ingestion.store import (
    insert_corporate_action,
    insert_dividend,
    insert_transaction,
    upsert_instrument,
)
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory

D = Decimal


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    """Re-enable loopback sockets PER TEST (pytest-socket re-bans before every test)."""
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _stock(conn: sqlite3.Connection, symbol: str, day: date, shares: str) -> None:
    insert_dividend(conn, account_id="tw_broker", symbol=symbol, div_date=day,
                    div_type="STOCK", gross=D("0"), withholding=D("0"), net=D("0"),
                    reinvest_shares=D(shares))


def _seed(conn: sqlite3.Connection) -> None:
    """Golden + four tw_broker shapes:

    * 2317 — ONLY an orphan 配股 of 50 (never held): red footer, one dividend cause.
    * 2454 → 2603 — 2454 bought, exchanged 1:1 into 2603, 2603 sold out, then a 配股 of 20 on
      2454 (refused, E24): 2454's footer is red by those 20; 2603 is the FLAT carrier.
    * 2881 — a refused EXCHANGE into it from 2882 (never held) + an orphan 配股 of 30: two
      causes, one corporate and one dividend — both remedies.
    """
    _seed_golden(conn)
    for sym in ("2317", "2454", "2603", "2881", "2882"):
        upsert_instrument(conn, Instrument(symbol=sym, market=Market.TW,
                                           quote_ccy=Currency.TWD, sector="Electronics",
                                           name=sym, board="TWSE"))
    _stock(conn, "2317", date(2026, 4, 1), "50")
    insert_transaction(conn, account_id="tw_broker", symbol="2454", side=Side.BUY,
                       quantity=D("1000"), price=D("500"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 1, 12))
    insert_corporate_action(conn, account_id="tw_broker", action_date=date(2026, 2, 2),
                            kind=CorporateActionKind.EXCHANGE, from_symbol="2454",
                            to_symbol="2603", ratio_to=D("1"), ratio_from=D("1"))
    insert_transaction(conn, account_id="tw_broker", symbol="2603", side=Side.SELL,
                       quantity=D("1000"), price=D("520"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 3, 2))
    _stock(conn, "2454", date(2026, 4, 1), "20")
    insert_corporate_action(conn, account_id="tw_broker", action_date=date(2026, 3, 2),
                            kind=CorporateActionKind.EXCHANGE, from_symbol="2882",
                            to_symbol="2881", ratio_to=D("1"), ratio_from=D("1"))
    _stock(conn, "2881", date(2026, 4, 1), "30")
    conn.commit()


def _open(page: Page, symbol: str) -> Locator:
    with page.expect_response(f"**/api/symbol/{symbol}/detail") as resp:
        page.evaluate(f"() => window.pdOpenSymbol('{symbol}')")
    assert resp.value.status == 200, f"{symbol} detail status {resp.value.status}"
    foot = page.locator(".sd-tx-section .sd-tx-reconcile")
    expect(foot).to_be_visible()
    return foot


@pytest.mark.e2e
def test_the_drawer_offers_each_causes_own_remedy(
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
    door = "button"

    # ===== an orphan 配股 only: its remedy, and NOT 補登公司行動 ==============================
    foot = _open(page, "2317")
    expect(foot).to_contain_text("⚠ 對帳不一致")
    expect(foot.locator(door, has_text="補登公司行動")).to_have_count(0)
    remedy = foot.locator('.sd-tx-remedy[data-cause="orphan"]')
    expect(remedy).to_have_count(1)
    expect(remedy).to_contain_text("補登該日（含）之前的買進或期初庫存，或刪除該筆股利")
    expect(remedy.locator("a", has_text="前往股利帳本")).to_have_attribute(
        "href", "trades.html?ledger=div")
    expect(foot.locator(".sd-tx-remedy")).to_have_count(1)
    # Guidance, not a state: the body text colour, from the stylesheet class (DEF-077).
    colour = remedy.evaluate("el => getComputedStyle(el).color")
    text2 = page.evaluate(
        "() => { const p = document.createElement('span');"
        " p.style.color = 'var(--text-2)'; document.body.appendChild(p);"
        " const c = getComputedStyle(p).color; p.remove(); return c; }")
    assert colour == text2, (colour, text2)

    # ===== a 配股 on a ticker an EXCHANGE vacated: the E24 remedy, its cause, no door =======
    foot = _open(page, "2454")
    expect(foot).to_contain_text("⚠ 對帳不一致")
    expect(foot).to_contain_text("差額 20 股")
    expect(foot.locator(door, has_text="補登公司行動")).to_have_count(0)
    cause = foot.locator(".sd-tx-issue", has_text="股利無法入帳")
    expect(cause).to_have_count(1)
    expect(cause).to_contain_text("換股為 2603")
    remedy = foot.locator('.sd-tx-remedy[data-cause="vacated"]')
    expect(remedy).to_have_count(1)
    expect(remedy).to_contain_text("改以現金收支登錄這筆配息，並刪除該筆股利")
    expect(remedy.locator("a", has_text="前往資金收支")).to_have_attribute("href", "cash.html")
    expect(remedy.locator("a", has_text="前往股利帳本")).to_have_attribute(
        "href", "trades.html?ledger=div")
    expect(page.locator(".sd-drawer .sd-ledger-issue", has_text="股利無法入帳")).to_have_count(1)

    # ===== K3: the FLAT carrier names the refused dividend it carries =====================
    foot = _open(page, "2603")
    expect(foot).to_contain_text("✓ 對帳一致")          # its own footer is fine…
    expect(foot.locator(".sd-tx-remedy")).to_have_count(0)
    carried = page.locator(".sd-drawer .sd-ledger-issue", has_text="股利待釐清")
    expect(carried).to_have_count(1)                     # …but the flag it carries is named
    expect(carried).to_contain_text("此部位由 2454 換股承接")
    expect(carried).to_contain_text("換股為 2603")

    # ===== two causes: each remedy, the corporate one included =============================
    foot = _open(page, "2881")
    expect(foot).to_contain_text("⚠ 對帳不一致")
    expect(foot.locator(door, has_text="補登公司行動")).to_have_count(1)
    expect(foot.locator('.sd-tx-remedy[data-cause="orphan"]')).to_have_count(1)
    expect(foot.locator(".sd-tx-issue", has_text="公司行動未套用")).to_have_count(1)

    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(150)
    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert overflow <= 0, f"drawer overflows by {overflow}px at 390 with the remedy lines"

    unexpected = [u for u in bad if not u.endswith("/api/whatif")]
    assert not unexpected and not page_errors, (
        f"drawer remedies: unexpected 5xx={unexpected!r} page={page_errors!r}")
