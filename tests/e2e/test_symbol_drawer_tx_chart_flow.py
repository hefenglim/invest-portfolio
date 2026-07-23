"""E2E (Playwright, real server + real frontend): Wave A2 symbol-drawer additions.

Drives the REAL stack (uvicorn subprocess + SQLite + served web/) opening the symbol-detail
drawer and asserting the two Wave-A2 behaviours that a canvas smoke test cannot see:

  1. 交易明細 (transaction detail) — a NEW paginated section (10/page) fed by
     GET /api/ledgers/transactions?symbol=...&limit=10&offset=n. A symbol with 12 buys must
     show 10 rows on page 1, a visible pager「共 12 筆」, and 2 rows after paging to page 2
     (offset=10) — all with ZERO console/page errors. The pager utility (pager.js, which
     index.html does NOT include) must be lazy-loaded by detail.js on demand.

  2. Chart cost-line labels — the EQUAL-average edge case (原始均價 == 調整均價, the owner's
     原始=調整 screenshot) renders ONE combined「均價」markLine; when the two averages differ a
     dividend adjustment) it renders TWO (原始均價 / 調整均價). Trade markPoints are symbol-only:
     their persistent value label is suppressed (label.show === false), the detail moving to
     the hover tooltip. Read via the live ECharts option (getInstanceByDom) since markLine /
     markPoint labels are canvas-drawn, not DOM.
"""

import sqlite3
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from playwright.sync_api import Page, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_dividend,
    insert_fx_conversion,
    insert_transaction,
    upsert_instrument,
)
from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.pricing.store import upsert_fx, upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import GOLDEN_NOW
from tests.e2e.conftest import FlowServerFactory


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    """Re-enable loopback sockets PER TEST (pytest-socket re-bans before every test); each
    flow spawns a fresh isolated uvicorn (free-port probe + readiness poll need loopback)."""
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _collect_errors(page: Page) -> tuple[list[str], list[str]]:
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(getattr(m, "text", ""))
            if getattr(m, "type", None) == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    return console_errors, page_errors


# --- seeds -------------------------------------------------------------------------

def _base_fx(conn: sqlite3.Connection) -> None:
    """FX rows covering any reporting currency the live server may default to."""
    upsert_fx(conn, [
        FxRow(base=Currency.USD, quote=Currency.TWD, as_of=date(2026, 5, 28),
              rate=Decimal("32"), source="test"),
        FxRow(base=Currency.USD, quote=Currency.TWD, as_of=date(2026, 6, 9),
              rate=Decimal("33"), source="test"),
        FxRow(base=Currency.MYR, quote=Currency.TWD, as_of=date(2026, 6, 9),
              rate=Decimal("7"), source="test"),
        FxRow(base=Currency.USD, quote=Currency.MYR, as_of=date(2026, 6, 9),
              rate=Decimal("4.4"), source="test"),
    ], fetched_at=GOLDEN_NOW)


def _seed_many_tx(conn: sqlite3.Connection) -> None:
    """12 buys of 2330 in one account → the drawer 交易明細 must paginate 10/page (2 pages)."""
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Semiconductors", name="TSMC", board="TWSE"))
    for i in range(12):
        insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                           quantity=Decimal("100"), price=Decimal("500"),
                           fees=Decimal("0"), tax=Decimal("0"),
                           trade_date=date(2026, 1, 1) + timedelta(days=i))
    upsert_prices(conn, [
        PriceRow(instrument="2330", market=Market.TW, as_of=date(2026, 6, 9),
                 close=Decimal("600"), source="test"),
    ], fetched_at=GOLDEN_NOW)
    _base_fx(conn)
    conn.commit()


def _seed_chart(conn: sqlite3.Connection) -> None:
    """Two held symbols whose price history covers the trade date (so trade markPoints render):
      * AAPL — a buy with NO dividend → original_avg == adjusted_avg (COMBINED-label case).
      * 2330 — a buy + a cash dividend → adjusted_avg != original_avg (TWO-label case)."""
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Semiconductors", name="TSMC", board="TWSE"))
    upsert_instrument(conn, Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
                                       sector="Tech", name="Apple"))
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=Decimal("1000"), price=Decimal("500"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 6, 1))
    insert_dividend(conn, account_id="tw_broker", symbol="2330", div_date=date(2026, 6, 3),
                    div_type="CASH", gross=Decimal("5000"), withholding=Decimal("0"),
                    net=Decimal("5000"))
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("10"), price=Decimal("100"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 6, 1))
    insert_fx_conversion(conn, account_id="schwab", date=date(2026, 5, 28),
                         from_ccy=Currency.TWD, from_amount=Decimal("32000"),
                         to_ccy=Currency.USD, to_amount=Decimal("1000"))
    upsert_prices(conn, [
        PriceRow(instrument="2330", market=Market.TW, as_of=date(2026, 6, 1),
                 close=Decimal("500"), source="test"),
        PriceRow(instrument="2330", market=Market.TW, as_of=date(2026, 6, 9),
                 close=Decimal("600"), source="test"),
        PriceRow(instrument="AAPL", market=Market.US, as_of=date(2026, 6, 1),
                 close=Decimal("100"), source="test"),
        PriceRow(instrument="AAPL", market=Market.US, as_of=date(2026, 6, 9),
                 close=Decimal("120"), source="test"),
    ], fetched_at=GOLDEN_NOW)
    _base_fx(conn)
    conn.commit()


def _seed_multi_account(conn: sqlite3.Connection) -> None:
    """AAPL genuinely held in TWO accounts (schwab 30 @100 + moomoo_my 10 @110): the drawer
    部位摘要 must show the AGGREGATE (40 sh) + a per-account breakdown, the head 「2 個帳戶」, and
    交易明細 an account filter (round-8.1 Wave A owner #2c / #2b)."""
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
                                       sector="Tech", name="Apple"))
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("30"), price=Decimal("100"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 10))
    insert_transaction(conn, account_id="moomoo_my", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("10"), price=Decimal("110"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 12))
    upsert_prices(conn, [
        PriceRow(instrument="AAPL", market=Market.US, as_of=date(2026, 6, 9),
                 close=Decimal("120"), source="test"),
    ], fetched_at=GOLDEN_NOW)
    _base_fx(conn)
    conn.commit()


# --- 交易明細 pagination -------------------------------------------------------------

@pytest.mark.e2e
def test_symbol_drawer_tx_pagination(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """交易明細 (round-8.1 Wave A): the UNIFIED activity list from /api/symbol/{sym}/detail,
    paginated 10/page CLIENT-SIDE (no per-page network). 12 buys → 10 rows on page 1, a
    「共 12 筆」pager, 2 rows on page 2, and a reconciliation footer proving
    買 1,200 ＝ 部位摘要."""
    base = flow_server(_seed_many_tx)
    page = fresh_page
    console_errors, page_errors = _collect_errors(page)

    page.goto(base + "/index.html", wait_until="load")
    page.wait_for_selector(".kpi-card")  # dashboard async render landed
    # the drawer self-fetches the per-symbol detail (which now carries the activity list).
    with page.expect_response("**/api/symbol/2330/detail") as resp_info:
        page.evaluate("() => window.pdOpenSymbol('2330')")
    assert resp_info.value.status == 200, f"detail status {resp_info.value.status}"

    sec = page.locator(".sd-tx-section")
    page.wait_for_selector(".sd-tx-section table.data tbody tr")
    rows = sec.locator("table.data tbody tr")
    expect(rows).to_have_count(10)  # page 1 of 2 (12 activity rows, 10/page)

    # section head + count, columns, and the reused neutral 買/賣 direction chip.
    expect(sec.locator(".sd-sec-title")).to_have_text("交易明細")
    expect(sec.locator(".pd-pager .pg-label")).to_contain_text("共 12 筆")
    expect(sec.locator("thead th")).to_have_count(8)  # 日期/帳戶/事件/股數/價格/費用/稅/合計
    expect(sec.locator("tbody .dir-chip").first).to_be_visible()

    # reconciliation footer: 12 buys × 100 sh = 1,200 shares = 部位摘要, balanced.
    foot = sec.locator(".sd-tx-reconcile")
    expect(foot).to_contain_text("＋買 1,200")
    expect(foot).to_contain_text("部位摘要 1,200 股")
    expect(foot).to_contain_text("對帳一致")

    # page 2 → CLIENT-SIDE re-slice (NO network) → the 2 remaining rows.
    sec.locator(".pd-pager .pg-btn").filter(has_text="2").click()
    expect(rows).to_have_count(2)

    assert not console_errors and not page_errors, (
        f"tx pagination: console={console_errors!r} page={page_errors!r}"
    )


@pytest.mark.e2e
def test_symbol_drawer_tx_section_omitted_when_no_history(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """A watchlist symbol with ZERO activity → the 交易明細 section omits itself entirely."""
    base = flow_server(_seed_chart)
    page = fresh_page
    console_errors, page_errors = _collect_errors(page)

    page.goto(base + "/index.html", wait_until="load")
    page.wait_for_selector(".kpi-card")
    # MSFT is not seeded → no instrument, no activity → detail.activity == [] → section omitted.
    with page.expect_response("**/api/symbol/MSFT/detail") as resp_info:
        page.evaluate("() => window.pdOpenSymbol('MSFT')")
    assert resp_info.value.status == 200
    page.wait_for_selector(".sd-drawer .sd-signals")  # drawer fully rendered
    expect(page.locator(".sd-tx-section")).to_have_count(0)

    assert not console_errors and not page_errors, (
        f"tx omit: console={console_errors!r} page={page_errors!r}"
    )


# --- chart cost-line labels + symbol-only trade markers -----------------------------

def _read_chart(page: Page, expected_markline_len: int) -> dict[str, Any]:
    """Wait until the live ECharts option carries exactly `expected_markline_len` cost lines,
    then return the markLine names + the buy/sell markPoint's symbol / colour / label (the
    round-8.1 Wave A redesign: coloured labelled triangle — canvas, so read via the option)."""
    page.wait_for_selector(".sd-drawer #sd-chart")
    page.wait_for_function(
        "n => { const b = document.getElementById('sd-chart');"
        " const i = window.echarts && window.echarts.getInstanceByDom(b);"
        " if (!i) return false; const s = i.getOption().series[0];"
        " return !!(s && s.markLine && s.markLine.data"
        " && s.markLine.data.length === n); }",
        arg=expected_markline_len,
    )
    result: dict[str, Any] = page.evaluate(
        "() => { const i = window.echarts.getInstanceByDom("
        " document.getElementById('sd-chart'));"
        " const s = i.getOption().series[0];"
        " const mp = (s.markPoint && s.markPoint.data) || [];"
        " const t = mp.find(d => d.name === '買進' || d.name === '賣出');"
        " return { names: (s.markLine.data || []).map(d => d.name),"
        "   tradeShow: t ? (t.label ? !!t.label.show : null) : 'no-trade',"
        "   tradeLabel: t && t.label ? String(t.label.formatter || '') : '',"
        "   tradeSymbol: t ? String(t.symbol || '') : '' }; }"
    )
    return result


@pytest.mark.e2e
def test_symbol_drawer_cost_line_labels(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """Equal averages → ONE「均價」line; distinct → TWO lines. Buy/sell markers are the
    redesigned coloured labelled triangles (owner #6): a path:// symbol + an always-on
    「買N」/「賣N」 label when trades are sparse, plus a ▲買/▼賣 legend."""
    base = flow_server(_seed_chart)
    page = fresh_page
    console_errors, page_errors = _collect_errors(page)

    page.goto(base + "/index.html", wait_until="load")
    page.wait_for_selector(".kpi-card")

    # AAPL: no dividend → original_avg == adjusted_avg → ONE combined「均價」line.
    page.evaluate("() => window.pdOpenSymbol('AAPL')")
    aapl = _read_chart(page, 1)
    assert aapl["names"] == ["均價"], f"combined cost line expected, got {aapl['names']!r}"
    # redesigned buy marker: custom triangle+stem path, always-on 「買N」 label (sparse: 1 buy).
    assert aapl["tradeSymbol"].startswith("path://"), f"symbol={aapl['tradeSymbol']!r}"
    assert aapl["tradeShow"] is True, f"trade label.show={aapl['tradeShow']!r}"
    assert aapl["tradeLabel"].startswith("買"), f"trade label={aapl['tradeLabel']!r}"
    # the ▲買 / ▼賣 legend renders alongside the chart.
    legend = page.locator(".sd-drawer .sd-chart-legend")
    expect(legend).to_be_visible()
    expect(legend).to_contain_text("買")
    expect(legend).to_contain_text("賣")

    # 2330: a cash dividend adjusts the average → TWO distinct labels.
    page.evaluate("() => window.pdOpenSymbol('2330')")
    tsmc = _read_chart(page, 2)
    assert tsmc["names"] == ["原始均價", "調整均價"], (
        f"two distinct cost lines expected, got {tsmc['names']!r}"
    )

    assert not console_errors and not page_errors, (
        f"cost-line labels: console={console_errors!r} page={page_errors!r}"
    )


# --- multi-account aggregate 部位摘要 + account filter (owner #2c / #2b) --------------

@pytest.mark.e2e
def test_symbol_drawer_multi_account_position(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """AAPL held in schwab (30) + moomoo_my (10): the drawer head shows「2 個帳戶」, 部位摘要 the
    AGGREGATE 40 股 + a per-account breakdown, and 交易明細 an account filter that narrows both
    the table and its reconciliation footer."""
    base = flow_server(_seed_multi_account)
    page = fresh_page
    console_errors, page_errors = _collect_errors(page)

    page.goto(base + "/index.html", wait_until="load")
    page.wait_for_selector(".kpi-card")
    with page.expect_response("**/api/symbol/AAPL/detail") as resp:
        page.evaluate("() => window.pdOpenSymbol('AAPL')")
    assert resp.value.status == 200
    page.wait_for_selector(".sd-drawer .sd-stats")
    drawer = page.locator(".sd-drawer")

    # head reflects the aggregate (owner #2c): 「2 個帳戶」, not one account's name.
    expect(drawer.locator(".sd-head")).to_contain_text("2 個帳戶")
    # 部位摘要 股數 stat = the AGGREGATE 40 (30 + 10), not one account's 30 or 10.
    shares_stat = drawer.locator(".sd-stat").filter(has_text="股數").locator(".v")
    expect(shares_stat).to_have_text("40")
    # SECONDARY per-account breakdown table with exactly 2 account rows.
    breakdown = drawer.locator(".sd-acct-breakdown")
    expect(breakdown).to_be_visible()
    expect(breakdown.locator("tbody tr")).to_have_count(2)

    # 交易明細 account filter (owner #2b): 全部 + 2 accounts = 3 buttons; both buys shown.
    tx = drawer.locator(".sd-tx-section")
    flt = tx.locator(".sd-tx-filter")
    expect(flt).to_be_visible()
    expect(flt.locator("button")).to_have_count(3)
    expect(tx.locator("table.data tbody tr")).to_have_count(2)
    # filter to the first specific account → the table narrows to that account's single row.
    flt.locator("button").nth(1).click()
    expect(tx.locator("table.data tbody tr")).to_have_count(1)

    assert not console_errors and not page_errors, (
        f"multi-account position: console={console_errors!r} page={page_errors!r}"
    )
