"""A segmented bar must never assign a width the CSSOM will throw away (QA-21).

The 2026-08-05 sweep fixed this class once, in ``web/app.js``'s ``barWidth`` helper, on the
holdings table's ``.mini-bar``. The currency-mix stacked bar (``.ccy-stack .seg``, ~220
lines below in the same file) wrote ``(share * 100) + '%'`` straight into the inline style
with no clamp, and the e2e guard that should have caught it swept only ``.mini-bar``.

Two distinct harms, and the SECOND one is the reason this is not cosmetic:

1. **The out-of-range segment disappears.** ``.ccy-stack .seg`` declares no width in
   ``web/styles.css``, so the inline value is the only source. An invalid CSS value is
   DISCARDED by the CSSOM, not clamped — ``width: -137.23%`` leaves the element at its
   auto (content) size, i.e. 0px, and the currency vanishes from the bar entirely while
   the row underneath still prints its weight.
2. **Every innocent sibling is rescaled.** The remaining segments keep their assigned
   bases (e.g. ``180.80%``), the flex container overflows, and ``flex-shrink`` divides the
   track in proportion to those bases. A healthy in-range currency is therefore squeezed
   by an amount that depends entirely on how wrong its *neighbour* is. One out-of-range
   sibling corrupts the whole bar.

The fixture is built so both harms are measurable at once: a net-SHORT USD leg (negative
weight by construction — the net-exposure convention of ``domain-ledger.md``) beside a TWD
leg over 100% and an in-range MYR "control" leg.

Server-computed shares for this ledger (probed against ``build_dashboard``, TWD reporting):

    TWD  +180.8046%   (over-range; assigned verbatim today)
    USD  -137.2307%   (negative; DISCARDED by the CSSOM today)
    MYR   +56.4261%   (the control — a perfectly legal width)

Rendered fraction of the track for the MYR control, measured in Chromium:

    before the clamp:  56.4261 / (180.8046 + 56.4261)  =  23.8%
    after  the clamp:  56.4261 / (100.0000 + 56.4261)  =  36.1%

All money/weight figures arrive from the API as Decimal strings; nothing here computes
money — the assertions are about CSS lengths and pixels only.
"""

from datetime import date
from decimal import Decimal

import pytest
from playwright.sync_api import Page

from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import GOLDEN_NOW, _seed_golden
from tests.e2e.conftest import FlowServerFactory

_AS_OF = date(2026, 6, 9)

# The MYR control's own assigned base, in percent (see the module docstring).
_CONTROL_BASE_PCT = 56.4261
# An over-range sibling can legitimately claim AT MOST the whole track once it is clamped,
# so the control must keep at least its share against a full-bar neighbour. This bound is
# derived from the clamp's contract, not from the fix's arithmetic.
_CONTROL_MIN_FRACTION = _CONTROL_BASE_PCT / (_CONTROL_BASE_PCT + 100.0)  # 0.3607


def _seed_three_currencies_one_short(conn) -> None:               # type: ignore[no-untyped-def]
    """The golden ledger + an MY position + a LOSING declared short on the USD leg.

    The short must be big enough to drive the whole USD currency NET negative (AAPL's
    +1,200 USD long sits in the same currency), because the defect is on the per-currency
    SUM, not on any single holding's weight.
    """
    _seed_golden(conn)
    upsert_instrument(conn, Instrument(symbol="1155", market=Market.MY,
                                       quote_ccy=Currency.MYR, sector="Financials",
                                       name="Maybank"))
    insert_transaction(conn, account_id="moomoo_my", symbol="1155", side=Side.BUY,
                       quantity=Decimal("25000"), price=Decimal("1"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 15))
    upsert_instrument(conn, Instrument(symbol="MSFT", market=Market.US,
                                       quote_ccy=Currency.USD, sector="Tech",
                                       name="Microsoft"))
    insert_transaction(conn, account_id="schwab", symbol="MSFT", side=Side.SELL,
                       quantity=Decimal("30"), price=Decimal("480"),
                       fees=Decimal("0"), tax=Decimal("0"),
                       trade_date=date(2026, 6, 1), short_sale=True)
    upsert_prices(conn, [
        PriceRow(instrument="MSFT", market=Market.US, as_of=_AS_OF,
                 close=Decimal("500"), source="test"),
        PriceRow(instrument="1155", market=Market.MY, as_of=_AS_OF,
                 close=Decimal("1.07"), source="test"),
    ], fetched_at=GOLDEN_NOW)


# Read back what the CSSOM KEPT (``el.style.width`` is '' when the value was discarded),
# together with the real laid-out box. Both are needed: the inline read proves the discard,
# the box proves the sibling rescale.
_SEGMENTS = """
() => {
  const stack = document.querySelector('.ccy-stack');
  if (!stack) return null;
  const track = Math.round(stack.getBoundingClientRect().width);
  const segs = Array.from(stack.querySelectorAll('.seg')).map((s) => ({
    inline: s.style.width,
    title: s.title,
    px: s.getBoundingClientRect().width
  }));
  return { track: track, segs: segs };
}
"""


def _pct(inline: str) -> float | None:
    """The numeric percentage of an inline ``width`` string, or None if not a percentage."""
    text = inline.strip()
    if not text.endswith("%"):
        return None
    try:
        return float(text[:-1])
    except ValueError:
        return None


@pytest.mark.e2e
def test_currency_stack_never_assigns_a_width_the_cssom_discards(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_three_currencies_one_short)
    page = fresh_page
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{base}/index.html", wait_until="networkidle")
    page.wait_for_selector(".ccy-stack .seg", state="attached")
    page.wait_for_timeout(400)

    data = page.evaluate(_SEGMENTS)
    assert data is not None and data["segs"], "the currency-mix stacked bar rendered no segments"

    for seg in data["segs"]:
        assert seg["inline"], (
            f"segment {seg['title']!r} carries NO inline width — the value assigned to it "
            f"was INVALID and the CSSOM DISCARDED it (an out-of-range width is not clamped "
            f"by the browser). The segment therefore falls back to its auto size and "
            f"disappears from the bar. Clamp to [0, 100] before assigning, the way "
            f"web/app.js's barWidth already does."
        )
        pct = _pct(seg["inline"])
        assert pct is not None, (
            f"segment {seg['title']!r} width is not a percentage: {seg['inline']!r}")
        assert 0.0 <= pct <= 100.0, (
            f"segment {seg['title']!r} claims {pct}% of a bar that is 100% long. An "
            f"over-range base does not merely overdraw its own segment — flex-shrink "
            f"rescales every sibling in proportion to it."
        )


@pytest.mark.e2e
def test_an_in_range_currency_is_not_squeezed_by_an_out_of_range_sibling(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """The RENDERED width of a healthy segment, measured in the browser.

    MYR's own share is 56.43% — a perfectly legal width. Before the clamp the 180.80% TWD
    sibling overflowed the flex container and MYR was shrunk to 23.8% of the track; after
    the clamp the worst a sibling can claim is the full bar, so MYR holds 36.1%.
    """
    base = flow_server(_seed_three_currencies_one_short)
    page = fresh_page
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{base}/index.html", wait_until="networkidle")
    page.wait_for_selector(".ccy-stack .seg", state="attached")
    page.wait_for_timeout(400)

    data = page.evaluate(_SEGMENTS)
    assert data is not None and data["segs"], "the currency-mix stacked bar rendered no segments"
    assert data["track"] > 0, "the stacked bar has no width to measure against"

    control = next((s for s in data["segs"] if s["title"].startswith("MYR")), None)
    assert control is not None, (
        f"the MYR control segment is missing from the bar: "
        f"{[s['title'] for s in data['segs']]}"
    )
    fraction = control["px"] / data["track"]
    assert fraction >= _CONTROL_MIN_FRACTION - 0.01, (
        f"the MYR segment holds only {fraction:.1%} of the {data['track']}px track "
        f"(>= {_CONTROL_MIN_FRACTION:.1%} expected). Its own share is "
        f"{_CONTROL_BASE_PCT}% — it was shrunk by an out-of-range SIBLING, not by its own "
        f"value. Measured: 23.8% before the clamp, 36.1% after. Segments: "
        f"{[(s['title'], s['inline'], round(s['px'])) for s in data['segs']]}"
    )


# --- the widened guard -------------------------------------------------------------
#
# The 2026-08-05 guard swept exactly one class name ('.mini-bar') and therefore could not
# see this bar. A guard that names one class will miss the next sibling in the same way, so
# this one is driven by a LIST: every segmented bar on the dashboard, one line each.
_BAR_SELECTORS = (".mini-bar .fill", ".ccy-stack .seg")

_ALL_BARS = """
(selectors) => selectors.flatMap((sel) => Array.from(document.querySelectorAll(sel))
  .map((node) => {
    const box = node.parentElement.getBoundingClientRect();
    return {sel: sel, inline: node.style.width,
            px: Math.round(node.getBoundingClientRect().width),
            hostPx: Math.round(box.width),
            label: (node.title || node.parentElement.textContent || '').trim().slice(0, 40)};
  }))
"""


@pytest.mark.e2e
def test_no_bar_on_the_dashboard_overflows_or_loses_its_width(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_three_currencies_one_short)
    page = fresh_page
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{base}/index.html", wait_until="networkidle")
    page.wait_for_selector("tbody tr", state="attached")
    page.wait_for_timeout(600)

    bars = page.evaluate(_ALL_BARS, list(_BAR_SELECTORS))
    assert bars, f"no bars matched {_BAR_SELECTORS} — the guard would sweep nothing"

    dropped = [b for b in bars if not b["inline"]]
    assert not dropped, (
        f"these bars carry no inline width, i.e. the browser DISCARDED an invalid value "
        f"and the element fell back to its auto size: {dropped}"
    )
    over = [b for b in bars if b["px"] > b["hostPx"] + 1]
    assert not over, f"bar fill overflows its host: {over}"
