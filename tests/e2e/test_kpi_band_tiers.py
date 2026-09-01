"""KPI band v3: at every tier, seven cards, no orphan, no hole, one baseline.

The v2 band was five fixed `fr` tracks and the complaint against it was not a bug in the
usual sense — every number was right. It was that the cards did not LINE UP: the busiest
card had the second-narrowest track, the XIRR card's status badge pushed its big number a
line below its neighbours', and the detail of one card wrapped wherever the flex container
happened to break it. None of that is visible to a test that asserts on text.

So this file asserts on GEOMETRY, which is the only place those defects live:

  · the seven cards fill each visual row EXACTLY (that is what the 12-column ladder buys —
    12 divides by 3, 4, 2 and 6, so `span n` is width-identical to `repeat(n, 1fr)`);
  · cards sharing a row share a height, and their captions share a baseline;
  · big values sharing a row share a baseline — the `min-height` on `.kpi-label` that makes
    a badged card as tall as an un-badged one;
  · no key/value row ever overflows its card, and a value is never pushed below its key.

⚠ The tier is read from `matchMedia`, NOT from the viewport number the test asked for.
Chrome's media-query width and the requested viewport width differ by the scrollbar, so
asserting "1101px must be the wide tier" would be asserting a browser detail. Reading the
tier back and checking the shape it actually produced tests the ladder without that
coupling — and `test_the_sweep_reaches_every_tier` makes sure the widths still cover all
four, which is the assumption that would otherwise rot silently.
"""

from typing import Any

import pytest
from playwright.sync_api import Page

# Widths chosen to sit well INSIDE each tier, not on its boundary (see the note above).
# The boundaries themselves are swept by test_no_horizontal_scroll, which asserts overflow
# rather than shape and so is indifferent to which side of a boundary it lands on.
_WIDTHS = (1440, 1200, 1000, 900, 800, 600, 430, 390)

# Cards per visual row, per tier. Derived from the span ladder in styles.css:
#   lg  4·4·4 / 3·3·3·3        md  4·4·4 / 6·6·6·6
#   sm  6·6·12 / 6·6·6·6       xs  6·6·12 / 6·6·12·12
_EXPECTED_ROWS = {
    "lg": [3, 4],
    "md": [3, 2, 2],
    "sm": [2, 1, 2, 2],
    "xs": [2, 1, 2, 1, 1],
}

_TIER = """
() => {
  const m = (q) => window.matchMedia(q).matches;
  if (m('(max-width: 480px)')) return 'xs';
  if (m('(max-width: 860px)')) return 'sm';
  if (m('(max-width: 1100px)')) return 'md';
  return 'lg';
}
"""

_MEASURE = """
() => {
  const band = document.getElementById('kpi-band');
  const cards = Array.from(band.querySelectorAll(':scope > .kpi-card'));
  const cs = getComputedStyle(band);
  return {
    tier: null,
    bandWidth: Math.round(band.getBoundingClientRect().width),
    colGap: Math.round(parseFloat(cs.columnGap) || 0),
    cards: cards.map((c) => {
      const b = c.getBoundingClientRect();
      const cap = c.querySelector('.kpi-cap');
      const val = c.querySelector('.kpi-value');
      const lab = c.querySelector('.kpi-label');
      return {
        cls: c.className,
        label: lab ? lab.textContent : '',
        top: Math.round(b.top),
        left: Math.round(b.left),
        w: Math.round(b.width),
        h: Math.round(b.height),
        capBottom: cap ? Math.round(cap.getBoundingClientRect().bottom) : null,
        valTop: val ? Math.round(val.getBoundingClientRect().top) : null,
        rows: Array.from(c.querySelectorAll('.combo-row')).map((r) => ({
          overflow: r.scrollWidth - r.clientWidth,
          kTop: Math.round(r.querySelector('.k').getBoundingClientRect().top),
          vTop: Math.round(r.querySelector('.v').getBoundingClientRect().top),
          text: r.querySelector('.k').textContent,
        })),
      };
    }),
  };
}
"""


def _load(page: Page, base_url: str, width: int) -> tuple[str, dict[str, Any]]:
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{base_url}/index.html", wait_until="networkidle")
    page.wait_for_selector(".kpi-card")
    page.wait_for_timeout(400)          # the async render has settled
    return page.evaluate(_TIER), page.evaluate(_MEASURE)


def _rows(cards: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group cards into visual rows by their top edge (grid rows are pixel-identical)."""
    out: list[list[dict[str, Any]]] = []
    for c in sorted(cards, key=lambda c: (c["top"], c["left"])):
        if out and abs(out[-1][0]["top"] - c["top"]) <= 2:
            out[-1].append(c)
        else:
            out.append([c])
    return out


@pytest.mark.e2e
@pytest.mark.parametrize("width", _WIDTHS)
def test_every_row_is_filled_exactly(live_server: str, browser_page: Page,
                                     width: int) -> None:
    """No orphan card, no leftover column — at any width.

    This is the whole point of moving to a 12-column grid: a row of `span 3`, `span 4`,
    `span 6` or `span 12` cards adds back up to exactly 12 columns plus their gaps, so a
    tier physically cannot leave a hole at the end of a row. A five-`fr`-track band had no
    such guarantee, which is why 7 cards could not be laid out in it at all.
    """
    tier, m = _load(browser_page, live_server, width)
    assert len(m["cards"]) == 7, (
        f"expected 7 KPI cards at {width}px, got {len(m['cards'])}: "
        f"{[c['label'] for c in m['cards']]}"
    )
    rows = _rows(m["cards"])
    assert [len(r) for r in rows] == _EXPECTED_ROWS[tier], (
        f"{width}px resolved to tier '{tier}' but laid out "
        f"{[len(r) for r in rows]} cards per row, expected {_EXPECTED_ROWS[tier]}"
    )
    for r in rows:
        spanned = sum(c["w"] for c in r) + m["colGap"] * (len(r) - 1)
        assert abs(spanned - m["bandWidth"]) <= 2, (
            f"row starting at y={r[0]['top']} ({[c['label'] for c in r]}) spans "
            f"{spanned}px of a {m['bandWidth']}px band at {width}px (tier '{tier}') — "
            f"a {m['bandWidth'] - spanned}px hole"
        )


@pytest.mark.e2e
@pytest.mark.parametrize("width", _WIDTHS)
def test_cards_in_one_row_align(live_server: str, browser_page: Page, width: int) -> None:
    """Equal height, one caption baseline, one value baseline.

    The value baseline is the load-bearing one. XIRR carries a status badge and its
    neighbours do not, so before `.kpi-label { min-height }` the badged card's label was a
    line taller and its big number sat ~15px below the others — the single most visible
    "these cards are not lined up" defect, and invisible to any text assertion.
    """
    tier, m = _load(browser_page, live_server, width)
    for r in _rows(m["cards"]):
        heights = {c["h"] for c in r}
        assert len(heights) == 1, (
            f"cards in one row have different heights at {width}px (tier '{tier}'): "
            f"{[(c['label'], c['h']) for c in r]}"
        )
        caps = [c["capBottom"] for c in r if c["capBottom"] is not None]
        assert len(set(caps)) <= 1, (
            f"captions in one row sit at different baselines at {width}px: "
            f"{[(c['label'], c['capBottom']) for c in r if c['capBottom'] is not None]}"
        )
        vals = [c["valTop"] for c in r if c["valTop"] is not None]
        assert len(set(vals)) <= 1, (
            f"big values in one row sit at different baselines at {width}px: "
            f"{[(c['label'], c['valTop']) for c in r if c['valTop'] is not None]} — "
            f"the .kpi-label min-height that absorbs a status badge has regressed"
        )


@pytest.mark.e2e
@pytest.mark.parametrize("width", _WIDTHS)
def test_no_row_overflows_and_no_value_is_orphaned(live_server: str, browser_page: Page,
                                                   width: int) -> None:
    """A key/value row fits its card, and the number stays on the key's first line.

    Both halves matter. The overflow half checks the measured tier boundaries were measured
    correctly — every span in the ladder was chosen so the widest row (「含匯兌總損益 ⟷
    +22,075,147」, 172px) fits. The orphan half is the 2026-09-01 defect itself: v2 rendered
    the detail as one wrapping flex run, so a label could sit at the end of one line and its
    number alone at the head of the next, reading as an independent KPI.
    """
    tier, m = _load(browser_page, live_server, width)
    for c in m["cards"]:
        for r in c["rows"]:
            assert r["overflow"] <= 1, (
                f"row 「{r['text']}」 in 「{c['label']}」 overflows its card by "
                f"{r['overflow']}px at {width}px (tier '{tier}') — the span for this card "
                f"is narrower than the row it has to hold"
            )
            assert r["vTop"] <= r["kTop"] + 2, (
                f"the value of 「{r['text']}」 in 「{c['label']}」 dropped below its key at "
                f"{width}px (kTop={r['kTop']}, vTop={r['vTop']}) — a line break landed "
                f"between a label and its number"
            )


@pytest.mark.e2e
def test_the_sweep_reaches_every_tier(live_server: str, browser_page: Page) -> None:
    """The widths above must actually exercise all four tiers.

    Without this the parametrized tests keep passing while quietly covering one tier — the
    exact shape of every ladder gap this repo has found (861-1023, 761-1435, 1024-1077).
    """
    seen = set()
    for width in _WIDTHS:
        tier, _ = _load(browser_page, live_server, width)
        seen.add(tier)
    assert seen == set(_EXPECTED_ROWS), (
        f"the width sweep {_WIDTHS} reached tiers {sorted(seen)}, "
        f"not all of {sorted(_EXPECTED_ROWS)}"
    )
