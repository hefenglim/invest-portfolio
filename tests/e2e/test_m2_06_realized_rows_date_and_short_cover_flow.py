"""M2-06 — the drawer's 已實現記錄 table hid the date and dressed a short cover as a sale.

Found 2026-09-05 by the drawer sweep (E2). Both halves are of the "the payload was correct
and the screen was not" kind, so both are asserted from the RENDERED table, never the JSON:

1. **No date column.** ``/api/symbol/<s>/detail.realized_rows`` carries ``sell_date`` on
   every row, but ``web/detail.js::realizedSection`` rendered
   帳戶／賣出股數／淨收款／調整成本移除／已實現損益 and never read it. A symbol sold in three
   tranches over two years showed three undated rows — while 交易明細, two sections lower in
   the SAME drawer, leads with 日期. Owner ruling: **date goes first**, matching that table.

2. **``kind == "short_cover"`` rendered identically to an ordinary sale.** The only ``kind``
   branch was ``dividend`` (audit H2). A short cover's 淨收款 is the short's weighted-average
   SALE value and its 成本移除 is the covering BUY's cost — the columns read the other way
   round from a sale, and the row is dated the COVER, not the sale. Undistinguished, a reader
   books it as a long position closed for a gain. Owner ruling: chip 「空單回補」, in the same
   ``.rz-kind`` slot the 股利 chip already uses (no new style, no fourth vocabulary).

⚠ Counter-proofs in the same file: an ordinary sale row gets NO chip, the post-close dividend
row keeps its 股利 chip, and the five pre-existing columns' text is unchanged cell for cell.
The narrow-viewport check is here too: the sixth column must be absorbed by the table's own
``overflow-x: auto`` wrapper, never by widening the document.
"""

from datetime import date, datetime
from decimal import Decimal

import pytest
from playwright.sync_api import Page

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
from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory

_AS_OF = date(2026, 6, 11)
_FETCHED = datetime(2026, 6, 11, 13, 0)

# The rendered 已實現記錄 table: header texts, then per row the cell texts WITHOUT any
# ``.rz-kind`` chip (so the account cell compares equal whether or not it carries one) and
# the chips separately (text + title).
_REALIZED = """
() => {
  const d = document.querySelector('.sd-drawer');
  if (!d) return null;
  const sec = Array.from(d.querySelectorAll('.sd-section')).find(
    s => { const t = s.querySelector('.sd-sec-title');
           return t && t.textContent === '已實現記錄'; });
  if (!sec) return { headers: null, rows: null };
  return {
    headers: Array.from(sec.querySelectorAll('thead th')).map(th => th.textContent.trim()),
    rows: Array.from(sec.querySelectorAll('tbody tr')).map(tr => ({
      cells: Array.from(tr.children).map(td => {
        const c = td.cloneNode(true);
        c.querySelectorAll('.rz-kind').forEach(x => x.remove());
        return c.textContent.trim();
      }),
      chips: Array.from(tr.querySelectorAll('.rz-kind')).map(
        x => ({ text: x.textContent.trim(), title: x.title })),
    })),
  };
}
"""

# Document-level overflow + the realized table's own wrapper, for the narrow-width check.
_MEASURE = """
() => {
  const se = document.scrollingElement;
  const d = document.querySelector('.sd-drawer');
  const sec = d && Array.from(d.querySelectorAll('.sd-section')).find(
    s => { const t = s.querySelector('.sd-sec-title');
           return t && t.textContent === '已實現記錄'; });
  const wrap = sec && sec.querySelector('.table-wrap');
  const table = wrap && wrap.querySelector('table');
  return {
    sw: se.scrollWidth, cw: se.clientWidth,
    drawer: d ? d.getBoundingClientRect().width : null,
    wrapClient: wrap ? wrap.clientWidth : null,
    wrapScroll: wrap ? wrap.scrollWidth : null,
    wrapOverflowX: wrap ? getComputedStyle(wrap).overflowX : null,
    table: table ? table.getBoundingClientRect().width : null,
    cols: table ? table.querySelectorAll('thead th').length : null,
  };
}
"""


def _seed_three_tranches(conn) -> None:                    # type: ignore[no-untyped-def]
    """The golden ledger plus ORBX: bought 100, then sold in THREE tranches on three
    different dates. Three ``sale`` rows whose ONLY distinguishing feature, besides the
    numbers, is the date — which is exactly the column that was missing."""
    _seed_golden(conn)
    upsert_instrument(conn, Instrument(symbol="ORBX", market=Market.US,
                                       quote_ccy=Currency.USD, sector="Industrials",
                                       name="Orbex Systems"))
    insert_transaction(conn, account_id="schwab", symbol="ORBX", side=Side.BUY,
                       quantity=Decimal("100"), price=Decimal("10"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 5))
    insert_transaction(conn, account_id="schwab", symbol="ORBX", side=Side.SELL,
                       quantity=Decimal("30"), price=Decimal("12"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 2, 2))
    insert_transaction(conn, account_id="schwab", symbol="ORBX", side=Side.SELL,
                       quantity=Decimal("30"), price=Decimal("13"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 3, 2))
    insert_transaction(conn, account_id="schwab", symbol="ORBX", side=Side.SELL,
                       quantity=Decimal("40"), price=Decimal("14"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 4, 1))
    upsert_prices(conn, [PriceRow(instrument="ORBX", market=Market.US, as_of=_AS_OF,
                                  close=Decimal("15"), source="test")],
                  fetched_at=_FETCHED)


def _seed_mixed_kinds(conn) -> None:                       # type: ignore[no-untyped-def]
    """The golden ledger plus CVRX, whose 已實現記錄 holds one row of EACH kind:

    * ``sale``        — bought 10 @ 50, sold 10 @ 55 (2026-03-02): +50, position flat
    * ``dividend``    — a cash dividend paid 2026-03-20, AFTER the position closed (audit H2)
    * ``short_cover`` — declared short 15 @ 60 (2026-04-01), covered 15 @ 56 (2026-05-04):
                        (60 − 56) × 15 = +60, dated the COVER

    The dividend lands while the position is FLAT (not short — a dividend on an open short
    is unbookable by rule), so all three rows are real bookings.
    """
    _seed_golden(conn)
    upsert_instrument(conn, Instrument(symbol="CVRX", market=Market.US,
                                       quote_ccy=Currency.USD, sector="Health",
                                       name="Covera Rx"))
    insert_transaction(conn, account_id="schwab", symbol="CVRX", side=Side.BUY,
                       quantity=Decimal("10"), price=Decimal("50"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 2, 2))
    insert_transaction(conn, account_id="schwab", symbol="CVRX", side=Side.SELL,
                       quantity=Decimal("10"), price=Decimal("55"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 3, 2))
    insert_dividend(conn, account_id="schwab", symbol="CVRX", div_date=date(2026, 3, 20),
                    div_type="CASH", gross=Decimal("20"), withholding=Decimal("6"),
                    net=Decimal("14"))
    insert_transaction(conn, account_id="schwab", symbol="CVRX", side=Side.SELL,
                       quantity=Decimal("15"), price=Decimal("60"),
                       fees=Decimal("0"), tax=Decimal("0"),
                       trade_date=date(2026, 4, 1), short_sale=True)
    insert_transaction(conn, account_id="schwab", symbol="CVRX", side=Side.BUY,
                       quantity=Decimal("15"), price=Decimal("56"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 5, 4))
    upsert_prices(conn, [PriceRow(instrument="CVRX", market=Market.US, as_of=_AS_OF,
                                  close=Decimal("57"), source="test")],
                  fetched_at=_FETCHED)


def _open(page: Page, base: str, symbol: str) -> None:
    page.goto(base + "/index.html", wait_until="load")
    page.wait_for_selector(".kpi-card")
    with page.expect_response(f"**/api/symbol/{symbol}/detail") as resp:
        page.evaluate("(s) => window.pdOpenSymbol(s)", symbol)
    assert resp.value.status == 200, f"/detail status {resp.value.status}"
    page.wait_for_selector(".sd-drawer .sd-signals")   # body render finished


def _realized(page: Page) -> dict:                         # type: ignore[type-arg]
    out = page.evaluate(_REALIZED)
    assert out is not None, "drawer did not render"
    assert out["rows"] is not None, "已實現記錄 section missing from the drawer"
    return out                                             # type: ignore[no-any-return]


# --- the date column ------------------------------------------------------------------


@pytest.mark.e2e
def test_realized_rows_lead_with_their_own_sell_date(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """Three tranches, three dates: 日期 is the FIRST column (as in 交易明細 below it) and
    each row prints ITS OWN ``sell_date`` — not one date repeated, not a neighbouring
    column shifted into the slot."""
    base = flow_server(_seed_three_tranches)
    _open(fresh_page, base, "ORBX")
    t = _realized(fresh_page)

    assert "日期" in t["headers"], (
        f"已實現記錄 has no 日期 column — thead is {t['headers']}, while every row of "
        f"/detail.realized_rows carries sell_date and 交易明細 in the same drawer leads with 日期"
    )
    assert t["headers"][0] == "日期", (
        f"日期 must be the FIRST column (owner ruling — the precedent is 交易明細 in this same "
        f"drawer), got {t['headers']}"
    )
    assert t["headers"] == ["日期", "帳戶", "賣出股數", "淨收款", "調整成本移除", "已實現損益"], (
        f"unexpected column set: {t['headers']}"
    )
    assert len(t["rows"]) == 3, f"expected 3 realized rows, got {t['rows']}"
    dates = sorted(r["cells"][0] for r in t["rows"])
    assert dates == ["2026-02-02", "2026-03-02", "2026-04-01"], (
        f"the three rows must print three DIFFERENT dates, the sells' own: {t['rows']}"
    )
    # and each date sits beside the tranche it belongs to
    pairs = {(r["cells"][0], r["cells"][2], r["cells"][3]) for r in t["rows"]}
    assert pairs == {("2026-02-02", "30", "360.00"),
                     ("2026-03-02", "30", "390.00"),
                     ("2026-04-01", "40", "560.00")}, (
        f"a date is printed beside the WRONG tranche: {t['rows']}"
    )


# --- the short_cover chip -------------------------------------------------------------


@pytest.mark.e2e
def test_short_cover_row_is_chipped_and_sale_row_is_not(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """One table, three kinds: the short cover carries 「空單回補」 in the ``.rz-kind`` slot,
    the ordinary sale carries NO chip, and the post-close dividend keeps its 「股利」 chip."""
    base = flow_server(_seed_mixed_kinds)
    _open(fresh_page, base, "CVRX")
    t = _realized(fresh_page)
    assert len(t["rows"]) == 3, f"expected sale + dividend + short_cover, got {t['rows']}"

    # rows are identified by the money they book, never by position
    def row_with(realized: str) -> dict:                   # type: ignore[type-arg]
        m = [r for r in t["rows"] if r["cells"][-1] == realized]
        assert len(m) == 1, f"no unique row booking {realized}: {t['rows']}"
        return m[0]                                        # type: ignore[no-any-return]

    cover = row_with("+60.00")
    sale = row_with("+50.00")
    div = row_with("+14.00")

    assert [c["text"] for c in cover["chips"]] == ["空單回補"], (
        f"the short_cover row (+60.00) renders with NO distinguishing mark — chips="
        f"{cover['chips']}, cells={cover['cells']}. It is indistinguishable from the "
        f"ordinary sale beside it, yet its 淨收款 is the SHORT's sale value and its 成本移除 "
        f"is the covering BUY — the columns read the other way round."
    )
    assert "放空回補" in cover["chips"][0]["title"], (
        f"the chip's title must explain what a cover is: {cover['chips'][0]}"
    )
    assert sale["chips"] == [], (
        f"an ORDINARY sale must carry no chip, got {sale['chips']}"
    )
    assert [c["text"] for c in div["chips"]] == ["股利"], (
        f"the post-close dividend row must KEEP its 股利 chip (audit H2), got {div['chips']}"
    )
    # the cover's own numbers, dated the COVER (2026-05-04), not the short sale (04-01)
    assert cover["cells"] == ["2026-05-04", "嘉信 Schwab", "15", "900.00", "840.00", "+60.00"], (
        f"short_cover cells: {cover['cells']}"
    )
    # COUNTER-PROOF: the five pre-existing columns are byte-for-byte what they were
    assert sale["cells"][1:] == ["嘉信 Schwab", "10", "550.00", "500.00", "+50.00"], (
        f"the sale row's original five columns changed: {sale['cells']}"
    )
    assert div["cells"][1:] == ["嘉信 Schwab", "—", "14.00", "—", "+14.00"], (
        f"the dividend row's original five columns changed: {div['cells']}"
    )
    assert sale["cells"][0] == "2026-03-02" and div["cells"][0] == "2026-03-20", (
        f"dates: sale={sale['cells'][0]} dividend={div['cells'][0]}"
    )


# --- narrow viewports -----------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.parametrize("width", (481, 390))
def test_sixth_column_never_widens_the_document(
    flow_server: FlowServerFactory, fresh_page: Page, width: int
) -> None:
    """At the phone widths the 5-column table already scrolled inside its own
    ``.table-wrap`` (``overflow-x: auto``, ``table { min-width: 640px }`` under 760px); the
    sixth column must eat that 640px, not the page. Document scrollWidth == clientWidth."""
    base = flow_server(_seed_mixed_kinds)
    fresh_page.set_viewport_size({"width": width, "height": 900})
    _open(fresh_page, base, "CVRX")
    m = fresh_page.evaluate(_MEASURE)

    assert m["cols"] == 6, f"precondition: the realized table has 6 columns, got {m}"
    assert m["sw"] <= m["cw"], (
        f"@{width}px the document scrolls sideways: scrollWidth={m['sw']} > "
        f"clientWidth={m['cw']} (drawer={m['drawer']}, table-wrap client/scroll="
        f"{m['wrapClient']}/{m['wrapScroll']}, table={m['table']})"
    )
    assert m["wrapOverflowX"] in ("auto", "scroll"), (
        f"the realized table's wrapper must own the overflow, got {m['wrapOverflowX']}"
    )
