"""The symbol drawer must not drop, or mislabel, what the ledger already told it.

Two defects found 2026-09-02 by the drawer sweep, both of the "the payload was correct and
the screen was not" kind — so both are asserted from the RENDERED drawer, never the JSON:

**M2-01 — a CLOSED position lost its 已實現記錄 and 配息史.** ``web/detail.js`` branched on
``detail.position``: a fully-sold symbol has no holding row, so it fell into the *watchlist*
branch, which renders neither section and prints 「此標的不在持倉中（觀察清單標的）— …無部位
／損益資料。」 Both halves of that sentence were false for a closed position: it is not a
watchlist name (it has 期初/買/賣 in the ledger) and it has realized P&L — which the very
next section, 交易明細, printed the sale for. ``txSection`` had already been taught this case
("A CLOSED position is unheld yet still has history") and its predicate — a non-empty
``detail.activity`` — is reused here rather than a second one being invented.

⚠ Counter-proof in the same file: a symbol with NO ledger activity at all must KEEP the
watchlist sentence. 已清倉 and 純觀察 are two states, and fixing the first must not merge them.

**M2-05 — an oversold position's 市值 was labelled 缺價 while the drawer head showed a
price.** ``market_value`` is nulled for THREE reasons (portfolio/pnl.py): no price,
``oversold``, ``unbookable_action``. Only the first is 缺價; on an oversell the value is
suppressed because the cost basis was DISCARDED. The wrong label sent the reader off to
fetch a quote that was already on screen instead of to the sell row that needs fixing.
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

# The rendered drawer, read as text + the 部位摘要 stat cells (k / v / s per cell).
_DRAWER = """
() => {
  const d = document.querySelector('.sd-drawer');
  if (!d) return null;
  const sec = (title) => Array.from(d.querySelectorAll('.sd-section')).find(
    s => { const t = s.querySelector('.sd-sec-title'); return t && t.textContent === title; });
  const rz = sec('已實現記錄');
  return {
    text: d.textContent,
    head: (d.querySelector('.sd-head') || {}).textContent || '',
    sections: Array.from(d.querySelectorAll('.sd-sec-title')).map(t => t.textContent),
    realizedRows: rz ? Array.from(rz.querySelectorAll('tbody tr')).map(
      r => Array.from(r.children).map(c => c.textContent.trim())) : null,
    split: (() => { const w = d.querySelector('.sd-split');
      const e = w && w.querySelector('.sd-empty'); return e ? e.textContent : null; })(),
    stats: Array.from(d.querySelectorAll('.sd-stat')).map(s => ({
      k: (s.querySelector('.k') || {}).textContent || '',
      v: (s.querySelector('.v') || {}).textContent || '',
      s: (s.querySelector('.s') || {}).textContent || ''}))
  };
}
"""


def _seed_closed_position(conn) -> None:                   # type: ignore[no-untyped-def]
    """The golden ledger plus KESO: bought 80, paid one cash dividend, then sold ALL 80 at a
    loss. The position is FLAT — no holding row — while the ledger holds a realized −1,600
    and a dividend. Both must reach the drawer."""
    _seed_golden(conn)
    upsert_instrument(conn, Instrument(symbol="KESO", market=Market.US,
                                       quote_ccy=Currency.USD, sector="Materials",
                                       name="Keystone Ore"))
    insert_transaction(conn, account_id="schwab", symbol="KESO", side=Side.BUY,
                       quantity=Decimal("80"), price=Decimal("40"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 3, 2))
    insert_dividend(conn, account_id="schwab", symbol="KESO", div_date=date(2026, 4, 6),
                    div_type="CASH", gross=Decimal("100"), withholding=Decimal("30"),
                    net=Decimal("70"))
    insert_transaction(conn, account_id="schwab", symbol="KESO", side=Side.SELL,
                       quantity=Decimal("80"), price=Decimal("20"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 6, 1))
    upsert_prices(conn, [PriceRow(instrument="KESO", market=Market.US, as_of=_AS_OF,
                                  close=Decimal("21.5"), source="test")],
                  fetched_at=_FETCHED)


def _seed_oversold(conn) -> None:                          # type: ignore[no-untyped-def]
    """The golden ledger plus PYRN sold 50 against a holding of 28 — an UNDECLARED oversell
    (``short_sale`` false), which discards the basis and suppresses valuation. A CURRENT
    PRICE is seeded on purpose: that is what makes the mislabel observable — the drawer head
    shows 49.37 while 市值 claimed 缺價."""
    _seed_golden(conn)
    upsert_instrument(conn, Instrument(symbol="PYRN", market=Market.US,
                                       quote_ccy=Currency.USD, sector="Health",
                                       name="Pyren Bioscience"))
    insert_transaction(conn, account_id="schwab", symbol="PYRN", side=Side.BUY,
                       quantity=Decimal("28"), price=Decimal("45"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 3, 2))
    insert_transaction(conn, account_id="schwab", symbol="PYRN", side=Side.SELL,
                       quantity=Decimal("50"), price=Decimal("48"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 6, 1))
    upsert_prices(conn, [PriceRow(instrument="PYRN", market=Market.US, as_of=_AS_OF,
                                  close=Decimal("49.3658"), source="test")],
                  fetched_at=_FETCHED)


def _open(page: Page, base: str, symbol: str) -> dict:      # type: ignore[type-arg]
    page.goto(base + "/index.html", wait_until="load")
    page.wait_for_selector(".kpi-card")
    with page.expect_response(f"**/api/symbol/{symbol}/detail") as resp:
        page.evaluate("(s) => window.pdOpenSymbol(s)", symbol)
    assert resp.value.status == 200, f"/detail status {resp.value.status}"
    page.wait_for_selector(".sd-drawer .sd-signals")   # body render finished
    return page.evaluate(_DRAWER)                      # type: ignore[no-any-return]


# --- M2-01 -------------------------------------------------------------------------


@pytest.mark.e2e
def test_closed_position_drawer_keeps_realized_and_dividend_history(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """A fully-sold symbol: 已實現記錄 + 配息史 render, and the drawer never calls it a
    watchlist name with 「無部位／損益資料」 while its own 交易明細 shows the sale."""
    base = flow_server(_seed_closed_position)
    d = _open(fresh_page, base, "KESO")

    assert d is not None, "drawer did not render"
    assert "已實現記錄" in d["sections"], (
        f"a CLOSED position dropped its 已實現記錄 section — rendered sections: "
        f"{d['sections']}. The realized rows are IN the /detail payload."
    )
    assert "配息史" in d["sections"], (
        f"a CLOSED position dropped its 配息史 section — rendered: {d['sections']}"
    )
    # the realized row itself, not merely the section frame
    assert d["realizedRows"], "已實現記錄 rendered with no rows for a symbol that has one"
    assert any("1,600" in c for row in d["realizedRows"] for c in row), (
        f"the realized −1,600 is missing from the rendered rows: {d['realizedRows']}"
    )
    # and the sentence that contradicted it
    assert "觀察清單標的" not in d["text"], (
        "a closed position with ledger history is still labelled 觀察清單標的"
    )
    assert "無部位／損益資料" not in d["text"], (
        "the drawer claims 無部位／損益資料 above a table of realized P&L"
    )
    # 交易明細 (the section that always knew about this case) is still there
    assert "交易明細" in d["sections"], f"交易明細 missing: {d['sections']}"


@pytest.mark.e2e
def test_true_watchlist_symbol_still_shows_the_empty_state(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """COUNTER-PROOF for M2-01: a symbol with ZERO ledger activity is a real watchlist name
    and must KEEP the 觀察清單標的 empty state — 已清倉 and 純觀察 are two states, and the
    fix for the first must not swallow the second."""
    base = flow_server(_seed_closed_position)
    d = _open(fresh_page, base, "MSFT")          # never seeded → no activity at all

    assert d is not None
    assert "觀察清單標的" in d["text"], (
        f"a never-traded symbol lost its watchlist empty state; sections={d['sections']}"
    )
    assert "已實現記錄" not in d["sections"], (
        f"a never-traded symbol grew holding-history sections: {d['sections']}"
    )
    assert "交易明細" not in d["sections"], f"unexpected 交易明細: {d['sections']}"


# --- M2-05 -------------------------------------------------------------------------


@pytest.mark.e2e
def test_oversold_market_value_is_not_labelled_missing_price(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """An oversold position whose price IS known: 市值 is suppressed because the basis was
    discarded, so its sub-label must say 待釐清 — not 缺價, which points at the wrong fix."""
    base = flow_server(_seed_oversold)
    d = _open(fresh_page, base, "PYRN")

    assert d is not None
    assert "49.37" in d["head"], (
        f"precondition failed — the drawer head should carry the price: {d['head']!r}"
    )
    mv = next((s for s in d["stats"] if s["k"] == "市值"), None)
    assert mv is not None, f"no 市值 stat in the drawer: {[s['k'] for s in d['stats']]}"
    assert mv["s"] != "缺價", (
        "市值 is labelled 缺價 on an oversold position whose price is displayed two "
        f"centimetres above it (head={d['head']!r}). The value is suppressed because the "
        "cost basis was discarded, not because the quote is missing."
    )
    assert "待釐清" in mv["s"], (
        f"市值 sub-label should name the real cause (待釐清), got {mv['s']!r}"
    )


def _seed_no_price(conn) -> None:                          # type: ignore[no-untyped-def]
    """The golden ledger plus VRTA, HELD but with NO price row at all. This is the case that
    IS 缺價 — the counter-proof for NEW-03: naming the oversell cause must not swallow the
    genuine missing-quote one."""
    _seed_golden(conn)
    upsert_instrument(conn, Instrument(symbol="VRTA", market=Market.US,
                                       quote_ccy=Currency.USD, sector="Bio",
                                       name="Verta Bio"))
    insert_transaction(conn, account_id="schwab", symbol="VRTA", side=Side.BUY,
                       quantity=Decimal("67"), price=Decimal("30"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 3, 2))


# --- NEW-03 ------------------------------------------------------------------------


@pytest.mark.e2e
def test_oversold_contribution_split_is_not_labelled_missing_price(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """報酬貢獻拆分 on an oversold position: ``capital_gain`` is null because the basis was
    DISCARDED (and stays null — there is no honest number to state), so the empty state must
    name that cause with the same words 市值 uses, not 缺價. One screen, one story."""
    base = flow_server(_seed_oversold)
    d = _open(fresh_page, base, "PYRN")

    assert d is not None
    assert d["split"] is not None, "報酬貢獻拆分 rendered no empty state for a null 資本利得"
    assert "缺價" not in d["split"], (
        f"報酬貢獻拆分 still says 缺價 on an oversold position ({d['split']!r}) while the "
        f"drawer head shows a price ({d['head']!r}) and 市值 two sections above already "
        f"names the real cause — the M2-05 fix would be half-done."
    )
    assert "賣超待釐清" in d["split"], (
        f"報酬貢獻拆分 should name the oversell as the cause, got {d['split']!r}"
    )
    # and the two cells must agree — this is why both read one helper
    mv = next((s for s in d["stats"] if s["k"] == "市值"), None)
    # Split from the comparison below: a MISSING 市值 row and a DISAGREEING one are
    # different failures and must not share one message (and the narrowing keeps
    # `mypy --strict` over tests/ happy, which the repo-wide gate runs).
    assert mv is not None, "部位摘要 has no 市值 row — nothing to compare against"
    assert mv["s"] in d["split"], (
        f"市值 says {mv['s']!r} while 報酬貢獻拆分 says {d['split']!r} — one position, "
        f"two stories"
    )


@pytest.mark.e2e
def test_a_genuinely_priceless_position_still_says_missing_price(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """COUNTER-PROOF for NEW-03: a held position with NO price row keeps 缺價 in BOTH cells.
    缺價 and 待釐清 are different problems with different fixes (fetch a quote vs. correct a
    row), and naming the second must not rename the first."""
    base = flow_server(_seed_no_price)
    d = _open(fresh_page, base, "VRTA")

    assert d is not None
    mv = next((s for s in d["stats"] if s["k"] == "市值"), None)
    assert mv is not None, f"no 市值 stat: {[s['k'] for s in d['stats']]}"
    assert mv["s"] == "缺價", (
        f"a position with no price at all must still read 缺價, got {mv['s']!r}"
    )
    assert d["split"] and "缺價" in d["split"], (
        f"報酬貢獻拆分 lost its 缺價 wording for a genuinely price-less position: "
        f"{d['split']!r}"
    )
