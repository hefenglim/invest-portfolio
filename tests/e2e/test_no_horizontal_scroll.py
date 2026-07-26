"""E2E layout guard (audit M1/M2, 2026-07-26): the document must NEVER scroll sideways.

A page-level horizontal scrollbar is always a layout defect here — wide content (tables,
charts) is supposed to scroll inside its own ``overflow-x: auto`` wrapper, never by pushing
the whole document. Before this guard existed the dashboard carried a FIXED 1,257px scroll
width at every viewport between 761px and 1,279px (a `.topbar { flex-wrap: nowrap }` rule
declared below the `.topbar` block won on order and stopped the header shrinking), and the
資料中心 body copy set a 584px floor at mobile widths (`.panel-sub { white-space: nowrap }`
applied to a paragraph, where `overflow: hidden` is inert because the span is inline).

Both were invisible to every existing test: pages loaded, no console errors, all data
correct. Only a width sweep catches them, so the sweep is the regression surface.

The widths are the measured boundaries, not round numbers:
  1440 — full labels, one topbar row
  1280 — the old dead `.kpi-band` breakpoint (`.kpi-band.v2` out-specified it)
  1100 — trimmed labels, one topbar row
   900 — the old 1280->860 gap (KPI band still 4 columns, 換匯 grid still 2)
   768 — tablet, above the <=760px mobile layer
   390 — phone
"""

import pytest
from playwright.sync_api import Page

_SWEEP_WIDTHS = (1440, 1280, 1100, 900, 768, 390)
_ALL_PAGES = (
    "index.html", "trades.html", "ledger.html", "cash.html", "instruments.html",
    "insights.html", "data-center.html", "dividend-inbox.html", "settings.html",
)

_MEASURE = """
() => {
  const se = document.scrollingElement;
  const widest = [];
  const walk = (el) => {
    for (const c of el.children) {
      const cs = getComputedStyle(c);
      if (cs.display === 'none') continue;
      if (c.scrollWidth > c.clientWidth + 1 && !/auto|scroll/.test(cs.overflowX)
          && c.clientWidth > 0 && cs.overflowX !== 'hidden') {
        widest.push(c.tagName + '#' + c.id + '.' + String(c.className).slice(0, 40)
                    + ' sw=' + c.scrollWidth + ' cw=' + c.clientWidth);
      }
      walk(c);
    }
  };
  walk(document.body);
  return {sw: se.scrollWidth, cw: se.clientWidth, widest: widest.slice(0, 4)};
}
"""


def _assert_no_h_scroll(page: Page, base_url: str, path: str, width: int) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{base_url}/{path}", wait_until="networkidle")
    # Charts/cards render after the payload lands; measure the settled layout.
    page.wait_for_timeout(600)
    m = page.evaluate(_MEASURE)
    assert m["sw"] <= m["cw"] + 1, (
        f"{path} @ {width}px scrolls horizontally: document scrollWidth={m['sw']} > "
        f"clientWidth={m['cw']}. Widest offenders: {m['widest']}"
    )


@pytest.mark.e2e
@pytest.mark.parametrize("width", _SWEEP_WIDTHS)
def test_dashboard_never_scrolls_sideways(
    live_server: str, browser_page: Page, width: int
) -> None:
    """The dashboard is the densest page (KPI band + FX grid + wide tables + topbar)."""
    _assert_no_h_scroll(browser_page, live_server, "index.html", width)


@pytest.mark.e2e
@pytest.mark.parametrize("width", (900, 390))
def test_every_page_never_scrolls_sideways(
    live_server: str, browser_page: Page, width: int
) -> None:
    """Every shipped page at the two widths that used to break (tablet gap + phone)."""
    for path in _ALL_PAGES:
        _assert_no_h_scroll(browser_page, live_server, path, width)


@pytest.mark.e2e
def test_topbar_stays_single_row_on_common_laptops(
    live_server: str, browser_page: Page
) -> None:
    """1,280-1,600px must keep the header on ONE row.

    Wrapping is the safety net, not the desktop look: the <=1365px tier trims the control
    labels to icons precisely so a common laptop never shows a two-row header. A regression
    that re-inflates the controls would show up here before it shows up as an overflow.
    """
    for width in (1600, 1440, 1366, 1280):
        browser_page.set_viewport_size({"width": width, "height": 900})
        browser_page.goto(f"{live_server}/index.html", wait_until="networkidle")
        browser_page.wait_for_timeout(400)
        height = browser_page.evaluate(
            "() => Math.round(document.getElementById('topbar')"
            ".getBoundingClientRect().height)"
        )
        assert height < 70, (
            f"topbar wrapped to a second row at {width}px (height={height}px); "
            f"the <=1365px label-trim tier should keep it single-row here"
        )
