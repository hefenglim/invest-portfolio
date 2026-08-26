"""AI-D48's third KPI term must RENDER and must not push the page sideways.

The golden fixture books only `DEPOSIT` cash movements, so `trading_financing_cost` is
exactly zero there and the site-wide width sweep never draws this row. That is not a safe
place to leave it: the owner's 群益 account is on the FE-D1 charge-first model and books a
`REBATE` every month, so a non-zero value is the NORMAL case in production and the ZERO case
is the one the fixtures happen to cover.

Hence a canned payload rather than seeded money. Seeding a rebate into `_seed_golden` would
move every figure in the spec-17 golden payload — a money-of-record change to buy a layout
test, which is the wrong trade. Intercepting `/api/dashboard` changes nothing but the pixels
under test.

The 768px width is the one that actually broke: the first version of this feature appended
the term as a THIRD segment of the existing 含匯兌總損益 subline and took the dashboard's
scrollWidth to 881px against a 768px viewport.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from playwright.sync_api import Page, Route

_COST = "-12345.67"
_PATTERN = "**/api/dashboard*"


@contextmanager
def _with_trading_cost(page: Page) -> Iterator[None]:
    """Serve the real dashboard payload with a non-zero third term spliced in.

    Fetched through the route so every OTHER number stays the real computed one — a
    hand-written whole payload would drift from the contract the moment a field is added.

    ⚠ A CONTEXT MANAGER, not a plain installer, because ``browser_page`` is **session-scoped**
    and shared by every e2e test. A `page.route` left installed does not merely break the next
    assertion in this file — it silently feeds a doctored `/api/dashboard` to every test that
    runs afterwards. `unroute` in a `finally` is the whole point of the shape.
    """
    def _handler(route: Route) -> None:
        response = route.fetch()
        payload = response.json()
        kpis = payload.get("kpis") or {}
        # The row only renders when B itself is present, so give it one if the fixture
        # could not compute it — otherwise this test would silently assert nothing.
        if kpis.get("total_return_fx_complete") is None:
            kpis["total_return_fx_complete"] = "1000.00"
            kpis["principal_fx_effect"] = "0"
            kpis["fx_complete_reason"] = None
        kpis["trading_financing_cost"] = _COST
        payload["kpis"] = kpis
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(payload))

    page.route(_PATTERN, _handler)
    try:
        yield
    finally:
        page.unroute(_PATTERN, _handler)


@pytest.mark.e2e
def test_the_trading_cost_row_renders_and_never_scrolls_sideways(
    live_server: str, browser_page: Page
) -> None:
    with _with_trading_cost(browser_page):
        for width in (1440, 900, 768, 390):
            browser_page.set_viewport_size({"width": width, "height": 900})
            browser_page.goto(f"{live_server}/index.html", wait_until="networkidle")
            browser_page.wait_for_timeout(600)

            band = browser_page.locator("#kpi-band").inner_text()
            assert "交易與融資成本" in band, f"the third term is missing at {width}px"

            m = browser_page.evaluate(
                "() => ({sw: document.scrollingElement.scrollWidth,"
                " cw: document.scrollingElement.clientWidth})"
            )
            assert m["sw"] <= m["cw"] + 1, (
                f"index.html @ {width}px scrolls horizontally with the 交易與融資成本 row: "
                f"scrollWidth={m['sw']} > clientWidth={m['cw']}"
            )


@pytest.mark.e2e
def test_a_zero_cost_says_nothing_at_all(live_server: str, browser_page: Page) -> None:
    """A ledger with no rebate / margin interest / broker fee has nothing to disclose.

    Asserted rather than assumed, because 「交易與融資成本 $0」 on every account that has never
    paid one is noise — and it is what the un-intercepted golden fixture would show if the
    zero guard regressed.
    """
    browser_page.set_viewport_size({"width": 1440, "height": 900})
    browser_page.goto(f"{live_server}/index.html", wait_until="networkidle")
    browser_page.wait_for_timeout(600)
    assert "交易與融資成本" not in browser_page.locator("#kpi-band").inner_text()
