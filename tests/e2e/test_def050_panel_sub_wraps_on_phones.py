"""E2E (real browser) — DEF-050: a panel head's subtitle is never cut off on a phone.

Measured on the demo (R2 observation, R3 register): 收件匣 › 待確認退款（折讓款） at 390x844 —
the head's ``.panel-sub`` had scrollWidth 849 against clientWidth 349, so the ellipsis cut
the very sentence DEF-011 added: 「不改變持倉成本與資產損益，但會計入 XIRR 與含匯兌總損益」.

Root cause: ``web/styles.css`` ``.panel-head .panel-sub { white-space: nowrap; }`` with
``.panel-sub { overflow: hidden; text-overflow: ellipsis }`` — one clipped line is the right
read on a desktop head, and a silent amputation on a 390px one.

Why ``test_no_horizontal_scroll`` never saw it: that guard hunts PAGE-level sideways scroll,
and skips every element whose ``overflow-x`` is ``hidden`` — an ellipsis is by construction
content that is clipped instead of scrolled. Truncation needs its own measurement: this file
reads ``scrollWidth > clientWidth`` on every head subtitle of every shipped page (and every
settings tab), with every disclosure expanded, at phone width — and pins that the desktop
head still reads as one line.
"""

import pytest
from playwright.sync_api import Page

from tests.e2e.test_no_horizontal_scroll import _ALL_PAGES, _EXPAND, _SETTINGS_TABS

_HEAD_SUBS = ".panel-head .panel-sub, .input-toggle .panel-sub, .set-head .panel-sub"

_CLIPPED = """
(sel) => [...document.querySelectorAll(sel)].filter((e) => {
  const cs = getComputedStyle(e);
  return cs.display !== 'none' && e.clientWidth > 0 && e.scrollWidth > e.clientWidth + 1;
}).map((e) => ({text: (e.textContent || '').trim().slice(0, 40),
                sw: e.scrollWidth, cw: e.clientWidth}))
"""

_KEY = "不改變持倉成本與資產損益，但會計入 XIRR 與含匯兌總損益"


def _open(page: Page, url: str, width: int) -> None:
    page.set_viewport_size({"width": width, "height": 844})
    page.goto("about:blank")  # a fresh document per page (see test_no_horizontal_scroll)
    page.goto(url, wait_until="networkidle")
    page.wait_for_timeout(600)


@pytest.mark.e2e
def test_the_refund_inbox_subtitle_is_whole_on_a_phone(
    live_server: str, browser_page: Page
) -> None:
    """The verifier's exact element: 390x844, 收件匣 › 待確認退款 head subtitle."""
    page = browser_page
    _open(page, f"{live_server}/dividend-inbox.html", 390)
    sub = page.locator("#rebate-section .panel-head .panel-sub")
    m = sub.evaluate("(e) => ({sw: e.scrollWidth, cw: e.clientWidth,"
                     " ws: getComputedStyle(e).whiteSpace,"
                     " right: e.getBoundingClientRect().right})")
    assert m["sw"] <= m["cw"] + 1, f"the refund subtitle is clipped at 390px: {m}"
    assert m["right"] <= 390, f"the refund subtitle runs past the viewport: {m}"
    assert _KEY in (sub.inner_text() or "")
    doc = page.evaluate("() => [document.scrollingElement.scrollWidth,"
                        " document.scrollingElement.clientWidth]")
    assert doc[0] <= doc[1] + 1, f"the wrapped head pushed the page sideways: {doc}"


@pytest.mark.e2e
def test_the_desktop_head_subtitle_still_reads_as_one_line(
    live_server: str, browser_page: Page
) -> None:
    """The ruling keeps the desktop read: at 1440px the head subtitle stays nowrap / one row."""
    page = browser_page
    _open(page, f"{live_server}/dividend-inbox.html", 1440)
    m = page.locator("#inbox-section .panel-head .panel-sub").evaluate(
        "(e) => ({ws: getComputedStyle(e).whiteSpace, h: e.getBoundingClientRect().height,"
        " lh: parseFloat(getComputedStyle(e).lineHeight) || 16})")
    assert m["ws"] == "nowrap", m
    assert m["h"] <= m["lh"] * 1.5, f"the desktop subtitle wrapped to a second line: {m}"


@pytest.mark.e2e
def test_no_head_subtitle_is_clipped_on_any_page_at_phone_width(
    live_server: str, browser_page: Page
) -> None:
    """Class scan, as a guard: every head subtitle on every shipped page + settings tab."""
    page = browser_page
    clipped: dict[str, list[dict[str, object]]] = {}
    seen = 0
    urls = [f"{live_server}/{p}" for p in _ALL_PAGES] + [
        f"{live_server}/settings.html?tab={t}#{t}" for t in _SETTINGS_TABS]
    for url in urls:
        _open(page, url, 390)
        page.evaluate(_EXPAND)
        page.wait_for_timeout(250)
        seen += page.evaluate(f"() => document.querySelectorAll({_HEAD_SUBS!r}).length")
        bad = page.evaluate(_CLIPPED, _HEAD_SUBS)
        if bad:
            clipped[url.replace(live_server, "")] = bad
    assert seen >= 20, f"only {seen} head subtitles found — the scan is blind"
    assert not clipped, f"head subtitles clipped at 390px: {clipped}"
