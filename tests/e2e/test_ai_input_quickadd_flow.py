"""E2E: AI-input inline quick registration (FU-D33) — the 立即註冊 resume flow.

Drives the REAL stack (uvicorn subprocess + SQLite + served web/). The LLM is inactive on the
flow server, so the two AI-parse seams are stubbed with ``page.route``:

  * ``/api/input/ai/preview`` returns a canned preview whose single row is an UNREGISTERED
    symbol (``code: unregistered_symbol``) on the FIRST call, and a HEALED (ok) row on the
    SECOND — mimicking the same request re-run after registration,
  * ``/api/instruments/lookup`` returns a canned found-response so the shared quick-add dialog
    enables its confirm.

The registration POST (``/api/instruments``) is NOT routed — it hits the REAL endpoint, so the
symbol is genuinely registered. The flow asserts: the unregistered row shows an inline 立即註冊
action → the shared dialog opens PREFILLED with the symbol (EDITABLE since FU-D42a — a wrong
AI-parsed symbol is fixable in place; market inferred from the row's account) → confirming
registers → the SAME preview re-runs automatically → the row heals, all with ZERO console /
page errors.
"""

import json
from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, Route, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory

_SYMBOL = "NEWCO"  # US symbol NOT in the golden registry (force-registered even quote-less)
_ACCOUNT = "schwab"  # a USD account → market inferred as US (the row's account)


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    """Re-enable loopback sockets PER TEST (pytest-socket re-bans before every test); each flow
    spawns a fresh isolated uvicorn (free-port probe + readiness poll need loopback TCP)."""
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _row(status: str, code: object, *, reason: object) -> dict[str, object]:
    return {
        "n": 0, "status": status, "reason": reason, "code": code,
        "data": {"account_id": _ACCOUNT, "symbol": _SYMBOL, "side": "buy",
                 "trade_date": "2026-06-02", "quantity": "10", "price": "100"},
    }


def _preview(status: str, code: object, *, reason: object, counts: dict[str, int]) -> str:
    return json.dumps({
        "rows": [_row(status, code, reason=reason)],
        "summary": {"total": 1, **counts},
        "meta": {"model": "mock", "via": "litellm", "cost_usd": None},
        "csv_text": f"account,symbol,side,date,shares,price,note\n"
                    f"{_ACCOUNT},{_SYMBOL},BUY,2026-06-02,10,100,\n",
    })


def _lookup_found() -> str:
    return json.dumps({
        "found": True, "registered": False, "archived": False,
        "name": "New Co", "sector": "", "board": "", "is_etf": False,
    })


@pytest.mark.e2e
def test_ai_input_inline_register_resumes(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_golden)
    page = fresh_page

    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(getattr(m, "text", ""))
            if getattr(m, "type", None) == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    # /api/input/ai/preview: unregistered on call 1, healed on call 2 (the auto re-run).
    calls = {"n": 0}

    def _ai_route(route: Route) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            body = _preview("error", "unregistered_symbol",
                            reason=f"未註冊標的 {_SYMBOL} — 請先至「標的管理」註冊",
                            counts={"ok": 0, "warn": 0, "error": 1})
        else:
            body = _preview("ok", None, reason=None, counts={"ok": 1, "warn": 0, "error": 0})
        route.fulfill(status=200, content_type="application/json", body=body)

    page.route("**/api/input/ai/preview", _ai_route)
    page.route("**/api/instruments/lookup**",
               lambda route: route.fulfill(status=200, content_type="application/json",
                                           body=_lookup_found()))

    # Open the AI tab (boot done once the pane + model picker are bound).
    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#csv-kinds .chip", state="attached")
    page.click("#tab-ai")
    page.wait_for_selector("#ai-dropzone", state="visible")

    # Parse → the canned unregistered row renders with the inline 立即註冊 action.
    page.fill("#ai-text", f"在嘉信買 10 股 {_SYMBOL} @ 100")
    page.click("#ai-parse")
    reg_btn = page.locator("#ai-body").get_by_role("button", name="立即註冊")
    reg_btn.wait_for(state="visible")

    # Click 立即註冊 → the shared quick-add dialog opens PREFILLED with the symbol. FU-D42a:
    # the field is EDITABLE (lockSymbol is deprecated/ignored) so a wrong symbol is fixable.
    reg_btn.click()
    dialog = page.locator(".modal-backdrop").last
    sym_input = dialog.locator("input.qa-symbol")
    expect(sym_input).to_have_value(_SYMBOL)
    expect(sym_input).to_be_editable()  # editable = enabled + NOT readonly (FU-D42a)

    # The canned lookup enables 確認; confirming registers via the REAL endpoint.
    confirm = dialog.get_by_role("button", name="確認", exact=True)
    expect(confirm).to_be_enabled()
    confirm.click()

    # The SAME preview re-runs automatically (call 2 → healed) → the row loses its error and
    # the 立即註冊 action disappears. Generous timeout: the confirm crosses the REAL
    # POST /api/instruments, whose synchronous instant-quote fetch probes live providers —
    # on a network-restricted runner that probe can stall well past the 5s default before
    # force-registering (the flow itself is correct; only the latency varies by host).
    expect(page.locator("#ai-body").get_by_role("button", name="立即註冊")).to_have_count(
        0, timeout=30000)
    expect(page.locator("#ai-body .st-ok")).to_have_count(1)
    assert calls["n"] >= 2  # first parse + the automatic resume re-preview

    assert not console_errors and not page_errors, (
        f"AI quick-add flow: console={console_errors!r} page={page_errors!r}"
    )
