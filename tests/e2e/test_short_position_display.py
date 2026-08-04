"""An open short must not be DISPLAYED as the opposite of what it is.

Found 2026-08-05 by the 0->1 sweep, on a ledger whose only unusual row was one declared
short. Two independent defects in the same table row, both of the "wrong number that looks
right" kind — the row rendered plausibly and no test, console error or layout guard saw it:

1. **The percentage disagreed with its own amount.** ``web/app.js`` still divided
   ``unrealized_pnl / adjusted_cost_total`` client-side. A short's basis is the proceeds
   received, i.e. NEGATIVE by construction, so the ratio flipped: −75.98 USD of unrealized
   LOSS was rendered ``+3.17%`` in the same cell as the loss. Audit H1 (2026-07-26) had
   already added the server-computed ``unrealized_pct`` (which divides by abs(original
   cost)) and moved the DRAWER onto it — the main holdings table was missed. The same
   divide is wrong for a long, too: adjusted cost is legally <= 0 once cumulative dividends
   exceed cost (domain-ledger.md), which is the 已回本 case.

2. **The weight bar showed a full bar for a negative weight.** ``.mini-bar .fill`` declares
   no width, so as a block it fills its track; an invalid inline ``width: -2.29%`` is
   DISCARDED by the CSSOM rather than clamped, and the element fell back to 100%. A −2.29%
   position drew a bar indistinguishable from the 99.33% holding above it.

Both are asserted from the RENDERED cell, not from the payload — the payload was already
correct in case 1, which is exactly why nothing caught it.
"""

from datetime import date, datetime
from decimal import Decimal

import pytest
from playwright.sync_api import Page

from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory

_AS_OF = date(2026, 6, 11)


def _seed_with_a_losing_short(conn) -> None:               # type: ignore[no-untyped-def]
    """The golden ledger plus ONE declared short that is currently LOSING.

    Sold 25 @ 480 (proceeds 12,000 -> a basis of −12,000); the mark is 495.185, above the
    sale price, so the short is down. The loss and the negative basis together are what
    make the sign flip observable: with a WINNING short both the right and the wrong
    formula produce a positive number, and the test would pass while broken.
    """
    _seed_golden(conn)
    upsert_instrument(conn, Instrument(symbol="MSFT", market=Market.US,
                                       quote_ccy=Currency.USD, sector="Tech",
                                       name="Microsoft"))
    insert_transaction(conn, account_id="schwab", symbol="MSFT", side=Side.SELL,
                       quantity=Decimal("25"), price=Decimal("480"),
                       fees=Decimal("0"), tax=Decimal("0"),
                       trade_date=date(2026, 6, 1), short_sale=True)
    upsert_prices(conn, [PriceRow(instrument="MSFT", market=Market.US, as_of=_AS_OF,
                                  close=Decimal("495.185"), source="test")],
                  fetched_at=datetime(2026, 6, 11, 13, 0))


_ROW = """
(sym) => {
  const tr = Array.from(document.querySelectorAll('tbody tr'))
                  .find(r => r.textContent.includes(sym));
  if (!tr) return null;
  const bars = Array.from(tr.querySelectorAll('.mini-bar')).map(mb => {
    const fill = mb.querySelector('.fill');
    const track = mb.querySelector('.track');
    return {inline: fill.style.width,
            fillPx: Math.round(fill.getBoundingClientRect().width),
            trackPx: Math.round(track.getBoundingClientRect().width),
            label: mb.lastChild.textContent.trim()};
  });
  return {cells: Array.from(tr.children).map(c => c.textContent.trim()), bars};
}
"""


@pytest.mark.e2e
def test_a_losing_short_does_not_render_as_a_gain(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_with_a_losing_short)
    page = fresh_page
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{base}/index.html", wait_until="networkidle")
    page.wait_for_selector("tbody tr", state="attached")
    page.wait_for_timeout(600)

    row = page.evaluate(_ROW, "MSFT")
    assert row is not None, "the short position is missing from the holdings table"

    # The P&L cell holds the amount and its percentage together. Whatever the formatting,
    # the two must not carry opposite signs — that is the defect, stated directly.
    pnl_cell = next((c for c in row["cells"] if "%" in c and ("-" in c or "−" in c)), None)
    assert pnl_cell is not None, f"no signed P&L cell found in {row['cells']}"
    amount, _, pct = pnl_cell.rpartition("−") if "−" in pnl_cell else pnl_cell.rpartition("-")
    assert "+" not in pct, (
        f"the short is DOWN (mark 495.185 > sale 480) but its percentage reads as a gain: "
        f"{pnl_cell!r}. The frontend must use the server's `unrealized_pct` (divided by "
        f"abs(original cost)), never `unrealized_pnl / adjusted_cost_total`."
    )


@pytest.mark.e2e
def test_a_negative_weight_never_draws_a_full_bar(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_with_a_losing_short)
    page = fresh_page
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{base}/index.html", wait_until="networkidle")
    page.wait_for_selector("tbody tr", state="attached")
    page.wait_for_timeout(600)

    row = page.evaluate(_ROW, "MSFT")
    assert row is not None and row["bars"], "no mini-bars rendered for the short row"
    negative = [b for b in row["bars"] if b["label"].lstrip("+-−").startswith(("0", "1", "2",
                "3", "4", "5", "6", "7", "8", "9")) and b["label"].startswith(("-", "−"))]
    assert negative, (
        f"expected at least one negatively-labelled bar on a net-short row, got "
        f"{[b['label'] for b in row['bars']]}"
    )
    for bar in negative:
        assert bar["inline"], (
            f"inline width is empty for label {bar['label']} — an INVALID value was "
            f"discarded by the CSSOM, so `.fill` falls back to a full-width block"
        )
        assert bar["fillPx"] < bar["trackPx"], (
            f"a {bar['label']} weight drew a FULL bar ({bar['fillPx']}px of "
            f"{bar['trackPx']}px) — visually identical to the largest holding"
        )

    # And every bar on the page stays inside its track, whatever the sign.
    all_rows = page.evaluate("""() => Array.from(document.querySelectorAll('.mini-bar'))
        .map(mb => {
          const f = mb.querySelector('.fill'), t = mb.querySelector('.track');
          return {fill: Math.round(f.getBoundingClientRect().width),
                  track: Math.round(t.getBoundingClientRect().width),
                  label: mb.lastChild.textContent.trim()}; })""")
    over = [b for b in all_rows if b["fill"] > b["track"] + 1]
    assert not over, f"mini-bar fill overflows its track: {over}"
