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
  1101 — KPI v3's widest tier at its NARROWEST: detail cards at `span 3` are 178px here
  1100 — one pixel below, where the detail row folds to 2x2
   900 — the old 1280->860 gap (KPI band still 4 columns, 換匯 grid still 2)
   861 — KPI v3 heroes at `span 4` are 171px here, against a 168px 大數字
   768 — tablet, above the <=760px mobile layer
   481 — one pixel above the tier where the two long-key cards go full width
   390 — phone

The 1101/861/481 widths were added with KPI band v3 (2026-09-01). Every one of them is a
tier boundary read off a MEASURED text width, and the previous three gaps in this file's
own ladder (861-1023, 761-1435, 1024-1077) were all "we sampled 900 and 390 and stepped
over the defect" — the boundary just inside a tier is where a band is squeezed hardest.
"""

import pytest
from playwright.sync_api import Page

from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory

_SWEEP_WIDTHS = (1440, 1280, 1101, 1100, 900, 861, 768, 481, 390)
_ALL_PAGES = (
    "index.html", "trades.html", "ledger.html", "cash.html", "instruments.html",
    "insights.html", "data-center.html", "dividend-inbox.html", "settings.html",
    # Added 2026-08-02. The guard never failed — it simply did not look here, and BOTH
    # defects found by the whole-site sweep were on pages missing from this list:
    # settings-datasources (table overflowed the document from 761px to ~1,435px, 30 days)
    # and pipeline-hub (card head could not wrap at 390px, 50 days). A guard that covers
    # half the site reports green for the half nobody changed.
    "input.html", "news.html", "pipeline-hub.html", "settings-accounts.html",
    "settings-datasources.html", "settings-llm.html", "settings-prompts.html",
    "settings-scheduler.html", "login.html",
)   # this is now every shipped page — keep it that way when adding one.

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
@pytest.mark.parametrize("width", (900, 768, 390))
def test_every_page_never_scrolls_sideways(
    live_server: str, browser_page: Page, width: int
) -> None:
    """Every shipped page at the widths that have actually broken.

    768 was added 2026-08-02 and is not redundant with 900: the 資料來源 fallback grid was
    pinned to three ~205px columns by an inline style, which fits 885px and does not fit
    753px. A sweep that samples only 900 and 390 steps over the defect exactly.
    """
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


@pytest.mark.e2e
def test_pipeline_hub_task_card_never_pushes_the_page_sideways(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """A page whose defect only exists once it HAS data needs the test to create that data.

    洞察管線 renders an empty state until an insight task exists, and neither the golden
    fixture nor `scripts/seed_demo.py` creates one — so the sweep above walks this page while
    it is blank and reports green. The live demo, which accumulated three tasks through the
    UI, overflowed 50px at 390px the whole time (`.pp-card-head` had no `flex-wrap`, so seven
    children could not fit a 301px card). That is the SECOND time richer live data caught a
    phone overflow the local seed could not (see LESSONS_LEARNED, v0.1.24).

    A task at `level=fail` is deliberate: it renders the extra 為什麼沒跑？ button, which is
    the widest the head ever gets, so this asserts the worst case rather than a lucky one.
    """
    base = flow_server(_seed_golden)
    page = fresh_page
    created = page.request.post(f"{base}/api/insight-tasks",
                                data={"name": "持倉週報", "scope": "portfolio",
                                      "enabled": True})
    assert created.ok, created.text()

    page.set_viewport_size({"width": 390, "height": 900})
    page.goto(f"{base}/pipeline-hub.html", wait_until="networkidle")
    page.wait_for_selector(".pp-card-head", state="attached")   # the card must really exist
    page.wait_for_timeout(400)
    m = page.evaluate(_MEASURE)
    assert m["sw"] <= m["cw"] + 1, (
        f"pipeline-hub.html @ 390px scrolls horizontally with one task card: "
        f"scrollWidth={m['sw']} > clientWidth={m['cw']}. Widest offenders: {m['widest']}"
    )


@pytest.mark.e2e
def test_a_kpi_cell_clips_an_over_long_value_instead_of_widening_the_page(
    live_server: str, browser_page: Page
) -> None:
    """Defence in depth for the 2026-08-05 finding, deliberately independent of its cause.

    A 136-digit XIRR (a 1-day window annualized) was rendered into `.kpi-value`, which had
    `white-space: nowrap` and NO overflow rule — so the value set the document width and the
    dashboard scrolled 1,915px sideways at a 1,440px viewport. The number is now suppressed
    upstream, which would make a test written against XIRR pass for the wrong reason; this
    one injects the long string directly, so it keeps failing if the CSS defence is removed
    no matter what produced the value.
    """
    browser_page.set_viewport_size({"width": 1440, "height": 900})
    browser_page.goto(f"{live_server}/index.html", wait_until="networkidle")
    browser_page.wait_for_timeout(600)
    injected = browser_page.evaluate(
        "() => { const n = document.querySelector('.kpi-value');"
        " if (!n) return false; n.textContent = '+' + '1,234,'.repeat(30) + '000';"
        " return true; }"
    )
    assert injected, "no .kpi-value on the dashboard — the selector moved"
    browser_page.wait_for_timeout(300)

    m = browser_page.evaluate(_MEASURE)
    assert m["sw"] <= m["cw"] + 1, (
        f"an over-long KPI value widened the document: scrollWidth={m['sw']} > "
        f"clientWidth={m['cw']}. Offenders: {m['widest']}"
    )
    cell = browser_page.evaluate(
        "() => { const n = document.querySelector('.kpi-value');"
        " return [n.scrollWidth, n.clientWidth]; }"
    )
    assert cell[0] > cell[1], (
        f"the cell did not actually clip ({cell}) — the assertion above would then pass "
        f"even with the defence removed"
    )
