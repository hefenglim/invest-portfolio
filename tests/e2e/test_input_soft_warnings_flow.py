"""M4-02: the manual door must show EVERY soft warning, and ack them one at a time.

``web/input.js`` picked exactly one:

    const oversell = soft.find((i) => i.code === 'sell_exceeds_holdings') || soft[0] || null;

and rendered only that one. Measured 2026-09-02 against the running app:

===========================================  ==========================================
server ``warn`` issues                       what reached the DOM
===========================================  ==========================================
``etf_flag_unknown``                         correct
``duplicate_trade`` + ``etf_flag_unknown``   only 重複交易 — the ETF warning vanished
``sell_exceeds_holdings`` + ``etf_flag_…``   only 賣超 — the ETF warning vanished
``stamp_fx_missing`` + ``cash_overdraft``    only 現金不足 — the un-computed stamp vanished
===========================================  ==========================================

``etf_flag_unknown`` is the one that makes this a P1: ``markets-and-fees.md`` (AI-D40) says
「an unknown rate is disclosed, never defaulted in silence」, and with a second soft issue on
screen it was defaulted in exactly that silence — the sell was taxed at 現股 0.3% with no
word of it, which is the 3× overtax the tri-state flag exists to prevent.

The aggravation is the tick. There was ONE checkbox bound to ONE ``m.acked`` boolean, so a
single click acknowledged every soft warning at once — including the ones that were never
drawn. Each warning now carries its own tick and ``確認寫入`` waits for all of them, and
``ack_oversell`` is sent from the OVERSELL tick alone rather than from "whichever box was on
screen".

The 賣超 block keeps its rich shape (§6.7's 補登公司行動 repair, offered before the
destructive tick) — this is per-issue rendering, not a levelling-down.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from playwright.sync_api import Page, Route, expect

from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory

_PREVIEW = "**/api/input/manual/preview"

_ETF_TEXT = "無法判定是否為 ETF，賣出稅率待確認"
_DUP_TEXT = "相同交易已存在"


def _issue(code: str, text: str) -> dict[str, Any]:
    return {"sev": "warn", "code": code, "text": text, "field": None}


@contextmanager
def _with_extra_issues(page: Page, extra: list[dict[str, Any]]) -> Iterator[None]:
    """Serve the REAL preview with *extra* soft issues spliced into ``issues``.

    Fetched through the route so fee/tax/總成本/position_preview stay the numbers the server
    actually computed — a hand-written payload would drift from the contract the moment a
    field is added, and would also stop proving that the OTHER warning is a real one.
    """
    def _handler(route: Route) -> None:
        response = route.fetch()
        payload = response.json()
        payload["issues"] = list(payload.get("issues") or []) + extra
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(payload))

    page.route(_PREVIEW, _handler)
    try:
        yield
    finally:
        page.unroute(_PREVIEW, _handler)


def _draft(page: Page, base: str, *, shares: str, side: str = "sell") -> None:
    """Fill the manual form and wait for the preview that the last keystroke fires."""
    page.goto(base + "/input.html", wait_until="load")
    page.wait_for_selector("#m-account option", state="attached")
    page.select_option("#m-account", "tw_broker")
    page.click("#m-side-" + side)
    page.fill("#m-symbol", "2330")
    page.fill("#m-shares", shares)
    with page.expect_response(_PREVIEW) as pv:
        page.fill("#m-price", "600")
    assert pv.value.status == 200


def _ticks(page: Page) -> Any:
    return page.locator("#m-issues input[type=checkbox]")


@pytest.mark.e2e
def test_two_soft_warnings_both_reach_the_screen_with_their_own_tick(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """Scenario B of the table: a real 賣超 beside a spliced ``etf_flag_unknown``."""
    base = flow_server(_seed_golden)
    page = fresh_page
    console_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(getattr(m, "text", ""))
            if getattr(m, "type", None) == "error" else None)

    with _with_extra_issues(page, [_issue("etf_flag_unknown", _ETF_TEXT)]):
        _draft(page, base, shares="1500")  # > 1000 held -> real oversell
        issues = page.locator("#m-issues")
        expect(issues).to_contain_text("超過持有的 1000 股")   # the 賣超 warning...
        expect(issues).to_contain_text(_ETF_TEXT)       # ...and the one that used to vanish
        expect(_ticks(page)).to_have_count(2)

        # One tick is not consent for two warnings.
        confirm = page.locator("#m-confirm")
        assert confirm.is_disabled()
        page.check("#m-ack")
        assert confirm.is_disabled(), "one tick acknowledged a warning it was not attached to"
        page.locator("#m-issues input[type=checkbox]").nth(1).check()
        page.wait_for_function(
            "() => { const b = document.querySelector('#m-confirm'); return b && !b.disabled; }")

        with page.expect_request("**/api/input/manual/commit") as rq:
            confirm.click()
        assert (rq.value.post_data_json or {})["ack_oversell"] is True

    assert not console_errors, console_errors


@pytest.mark.e2e
def test_a_warning_the_user_never_read_is_not_acknowledged_by_another_ones_tick(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """Scenario A: two soft warnings, NEITHER an oversell.

    ``ack_oversell`` used to ride whichever single box was on screen, so ticking 「重複交易」
    told the server the owner had accepted a 賣超 — a claim they were never shown and never
    made. The commit-time replay is the only thing that re-checks it, and that flag is
    exactly what silences it.
    """
    base = flow_server(_seed_golden)
    page = fresh_page

    with _with_extra_issues(page, [_issue("duplicate_trade", _DUP_TEXT),
                                   _issue("etf_flag_unknown", _ETF_TEXT)]):
        _draft(page, base, shares="100", side="buy")   # well within holdings -> no 賣超
        issues = page.locator("#m-issues")
        expect(issues).to_contain_text(_DUP_TEXT)
        expect(issues).to_contain_text(_ETF_TEXT)
        expect(_ticks(page)).to_have_count(2)
        # No 賣超 among them, so §6.7's repair must not be offered here.
        expect(page.locator("#m-oversell-fix")).to_have_count(0)

        _ticks(page).nth(0).check()
        _ticks(page).nth(1).check()
        page.wait_for_function(
            "() => { const b = document.querySelector('#m-confirm'); return b && !b.disabled; }")
        with page.expect_request("**/api/input/manual/commit") as rq:
            page.locator("#m-confirm").click()
        assert (rq.value.post_data_json or {})["ack_oversell"] is False, (
            "acknowledging a duplicate must not acknowledge an oversell")


@pytest.mark.e2e
def test_the_single_warning_case_is_not_degraded(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """Counter-evidence for M4-02: the ONE-issue rendering must come out unchanged.

    Everything §6.7 put in the 賣超 box is still there and still in its order — the repair
    first, the tick after it, and the tick still naming what it costs — and the checkbox is
    still ``#m-ack`` (five e2e flows drive it by that id).
    """
    base = flow_server(_seed_golden)
    page = fresh_page

    _draft(page, base, shares="1500")
    expect(_ticks(page)).to_have_count(1)
    expect(page.locator("#m-ack")).to_have_count(1)
    repair = page.locator("#m-oversell-fix")
    expect(repair).to_be_visible()
    expect(repair).to_contain_text("補登公司行動")
    assert page.evaluate(
        "() => { const fix = document.querySelector('#m-oversell-fix');"
        " const ack = document.querySelector('#m-ack');"
        " return !!(fix && ack && (fix.compareDocumentPosition(ack)"
        "   & Node.DOCUMENT_POSITION_FOLLOWING)); }"
    ), "§6.7 lists 補登公司行動 FIRST — the tick beside it discards the basis permanently"
    expect(page.locator("#m-issues")).to_contain_text("成本基礎會被永久捨棄")

    assert page.locator("#m-confirm").is_disabled()
    page.check("#m-ack")
    page.wait_for_function(
        "() => { const b = document.querySelector('#m-confirm'); return b && !b.disabled; }")
