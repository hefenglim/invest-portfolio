"""E2E — owner ruling 2026-10-10: the manual page recognises a pasted contract code by itself.

Before the ruling a contract code typed or pasted into 代號 that the page's own instrument list
did not know went out as a STOCK body, the server answered 400 「選擇權請選擇方向…」, and the
form showed no side buttons. Now the page asks ``GET /api/input/option-symbol?text=…`` (the
server parses the shape — never the browser) once the typing settles, and a contract takes the
same path the 「＋選擇權合約」 builder takes: the canonical symbol in 代號, the contract's name,
option mode with the four side buttons, 口數. An unknown STOCK ticker is untouched: one ask,
answered ``{"symbol": null}`` (200, never a console error), then the stock form as before.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any

import pytest
from playwright.sync_api import Page
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory

#: A contract that is still alive on any run date (the browser never composes one; the TEST
#: writes the two spellings an owner pastes).
_EXPIRY = date.today() + timedelta(days=70)
CANONICAL = f"XYZ{_EXPIRY:%y%m%d}P00050000"
OCC_LOWER = CANONICAL.lower()
SCHWAB_TEXT = f"XYZ {_EXPIRY:%m/%d/%Y} 50.00 P"


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


class _Watch:
    """Console errors, 4xx/5xx answers, the recognition asks and the manual-door bodies."""

    def __init__(self, page: Page) -> None:
        self.errors: list[str] = []
        self.bad: list[str] = []
        self.asks: list[str] = []
        self.bodies: list[dict[str, Any]] = []
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.on("console", self._console)
        page.on("response", self._response)
        page.on("request", self._request)

    def _console(self, m: Any) -> None:
        if getattr(m, "type", None) == "error":
            self.errors.append(getattr(m, "text", ""))

    def _response(self, r: Any) -> None:
        if r.status >= 400:
            self.bad.append(f"{r.status} {r.url}")

    def _request(self, req: Any) -> None:
        if "/api/input/option-symbol" in req.url:
            self.asks.append(req.url)
        if req.method == "POST" and "/api/input/manual/" in req.url:
            self.bodies.append(req.post_data_json or {})


def _manual(page: Page, base: str) -> None:
    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#m-account option[value='schwab']", state="attached")
    page.select_option("#m-account", "schwab")


def _sell_to_open_one_and_commit(page: Page, base: str, w: _Watch) -> None:
    page.wait_for_selector("#m-opt-side-field:visible")
    assert page.input_value("#m-symbol") == CANONICAL
    assert page.is_hidden("#m-side-field") and page.is_hidden("#m-shares-field")
    hint = page.inner_text("#m-sym-hint")
    assert f"XYZ {_EXPIRY.isoformat()} 50 Put" in hint and "選擇權" in hint
    page.wait_for_selector("#m-opt-sides button[data-intent='STO'].active")   # OPT-D18
    page.click("#m-opt-sides button[data-intent='STO']")                      # 賣出開倉
    page.fill("#m-contracts", "1")
    page.fill("#m-price", "2.00")
    page.wait_for_function(
        "() => document.querySelector('#m-pc-label').textContent === '淨收權利金'")
    page.wait_for_function(       # the server's preview: 1 口 → 100 股當量, schwab's option fee
        "() => document.querySelector('#m-contracts-hint').textContent.includes('100 股當量')"
        " && document.querySelector('#m-fee-rule').textContent.includes('每口 0.65')")
    page.wait_for_function("() => !document.querySelector('#m-confirm').disabled")
    page.click("#m-confirm")
    page.wait_for_selector("#tx-body .tx-opt-side:text('賣出開倉')")
    # registered once, by the commit (the recognition itself writes nothing)
    ctx = page.request.get(base + "/api/input/context").json()
    assert [i["symbol"] for i in ctx["instruments"]].count(CANONICAL) == 1
    # every manual-door body was an OPTION body with a side — never the stock body that the
    # server refused before the ruling
    assert w.bodies, "no preview / commit was sent"
    assert all(b.get("symbol") == CANONICAL for b in w.bodies), w.bodies
    assert all(b.get("option_intent") or b.get("close_reason") for b in w.bodies), w.bodies
    assert not any("shares" in b or "side" in b for b in w.bodies), w.bodies


@pytest.mark.e2e
def test_a_pasted_lower_case_occ_symbol_opens_option_mode_and_commits(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_golden)
    page = fresh_page
    w = _Watch(page)
    _manual(page, base)
    page.fill("#m-symbol", OCC_LOWER)
    _sell_to_open_one_and_commit(page, base, w)
    assert len(w.asks) == 1 and "text=" in w.asks[0], w.asks      # once, for the pasted value
    assert not w.bad, w.bad
    assert not w.errors, w.errors


@pytest.mark.e2e
def test_a_pasted_statement_string_opens_option_mode_and_commits(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_golden)
    page = fresh_page
    w = _Watch(page)
    _manual(page, base)
    page.fill("#m-symbol", SCHWAB_TEXT)
    _sell_to_open_one_and_commit(page, base, w)
    assert len(w.asks) == 1, w.asks
    assert not w.bad, w.bad
    assert not w.errors, w.errors


@pytest.mark.e2e
def test_an_unknown_stock_ticker_stays_a_stock(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_golden)
    page = fresh_page
    w = _Watch(page)
    _manual(page, base)
    page.click("#m-symbol")
    page.keyboard.type("ZZQX", delay=40)          # a keystroke burst: ONE ask, after it
    page.fill("#m-shares", "1")
    page.fill("#m-price", "10")
    with page.expect_response("**/api/input/manual/preview") as resp:
        pass
    assert resp.value.status == 200
    assert page.is_visible("#m-side-field") and page.is_hidden("#m-opt-side-field")
    assert "未註冊" in page.inner_text("#m-sym-hint")
    assert len(w.asks) == 1 and "ZZQX" in w.asks[0], w.asks
    assert w.bodies and all(b.get("symbol") == "ZZQX" and "option_intent" not in b
                            for b in w.bodies), w.bodies
    assert not w.bad, w.bad
    assert not w.errors, w.errors
