"""E2E: AI-input inline quick registration (FU-D33; Wave C Fable F4d) — the 立即註冊 resume flow.

Drives the REAL stack (uvicorn subprocess + SQLite + served web/). The LLM is inactive on the
flow server, so the AI-parse seam is stubbed with ``page.route``:

  * ``/api/input/ai/preview`` returns a canned preview whose single row is an UNREGISTERED
    symbol (``code: unregistered_symbol``),
  * ``/api/instruments/lookup`` returns a canned found-response so the shared quick-add dialog
    enables its confirm.

The registration POST (``/api/instruments``) is NOT routed — it hits the REAL endpoint, so the
symbol is genuinely registered.

Fable F4d (Wave C): registering NO LONGER re-runs the paid vision parse. The old flow did
``await runAiPreview()`` on resume — a fresh ``/api/input/ai/preview`` LLM call per registered
symbol that also discarded the user's checkbox selections. The new resume ONLY reloads the
structural context and re-validates the affected row LOCALLY: because the REAL register genuinely
adds the symbol, ``GET /api/input/context`` now resolves it, so the row heals to ✓ with NO second
AI preview call. The flow asserts: the unregistered row shows an inline 立即註冊 action → the
shared dialog opens PREFILLED + EDITABLE (FU-D42a) → confirming registers → the row heals locally
→ ``/api/input/ai/preview`` was called EXACTLY ONCE, all with ZERO console / page errors.
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

    # /api/input/ai/preview: ALWAYS the unregistered row. Fable F4d: the resume must NOT
    # re-call this endpoint — the heal is local, so this stub is expected to fire exactly once.
    calls = {"n": 0}

    def _ai_route(route: Route) -> None:
        calls["n"] += 1
        body = _preview("error", "unregistered_symbol",
                        reason=f"未註冊標的 {_SYMBOL} — 請先至「標的管理」註冊",
                        counts={"ok": 0, "warn": 0, "error": 1})
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

    # Fable F4d: the row heals LOCALLY (reloadContext resolves the now-registered symbol) → the
    # error clears and the 立即註冊 action disappears WITHOUT a second vision parse. Generous
    # timeout: the confirm crosses the REAL POST /api/instruments, whose synchronous instant-quote
    # fetch probes live providers — on a network-restricted runner that probe can stall well past
    # the 5s default before force-registering (the flow itself is correct; only the latency varies).
    expect(page.locator("#ai-body").get_by_role("button", name="立即註冊")).to_have_count(
        0, timeout=30000)
    expect(page.locator("#ai-body .st-ok")).to_have_count(1)
    # The vision parse ran ONCE (the initial 解析) — registering did NOT re-POST
    # /api/input/ai/preview.
    assert calls["n"] == 1, f"AI preview should not re-run on register; got {calls['n']} calls"

    assert not console_errors and not page_errors, (
        f"AI quick-add flow: console={console_errors!r} page={page_errors!r}"
    )


def _preview_two() -> str:
    """A two-row canned preview: row 0 a CLEAN buy of the golden-registered AAPL (ok), row 1 the
    UNREGISTERED NEWCO (error). Both on schwab (US)."""
    csv = ("account,symbol,side,date,shares,price,note\n"
           "schwab,AAPL,BUY,2026-06-02,5,100,\n"
           f"schwab,{_SYMBOL},BUY,2026-06-02,10,100,\n")
    rows = [
        {"n": 0, "status": "ok", "reason": None, "code": None,
         "data": {"account_id": "schwab", "symbol": "AAPL", "side": "buy",
                  "trade_date": "2026-06-02", "quantity": "5", "price": "100"}},
        {"n": 1, "status": "error", "reason": f"未註冊標的 {_SYMBOL}",
         "code": "unregistered_symbol",
         "data": {"account_id": "schwab", "symbol": _SYMBOL, "side": "buy",
                  "trade_date": "2026-06-02", "quantity": "10", "price": "100"}},
    ]
    return json.dumps({
        "rows": rows, "summary": {"total": 2, "ok": 1, "warn": 0, "error": 1},
        "meta": {"model": "mock", "via": "litellm", "cost_usd": None}, "csv_text": csv,
    })


@pytest.mark.e2e
def test_ai_register_preserves_other_rows_checkbox_state(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """Fable F4d: registering ONE unregistered row must NOT discard the user's checkbox choices on
    the OTHER rows (the old re-parse rebuilt the whole table with defaults). Here row 0 (AAPL) is
    UNCHECKED by the user; registering row 1 (NEWCO) heals it to ✓ + auto-checks it while row 0
    stays UNCHECKED — and the vision parse still ran exactly once."""
    base = flow_server(_seed_golden)
    page = fresh_page

    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(getattr(m, "text", ""))
            if getattr(m, "type", None) == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    calls = {"n": 0}

    def _ai_route(route: Route) -> None:
        calls["n"] += 1
        route.fulfill(status=200, content_type="application/json", body=_preview_two())

    page.route("**/api/input/ai/preview", _ai_route)
    page.route("**/api/instruments/lookup**",
               lambda route: route.fulfill(status=200, content_type="application/json",
                                           body=_lookup_found()))

    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#csv-kinds .chip", state="attached")
    page.click("#tab-ai")
    page.wait_for_selector("#ai-dropzone", state="visible")

    page.fill("#ai-text", f"buy AAPL and {_SYMBOL}")
    page.click("#ai-parse")
    page.wait_for_selector("#ai-body tr:nth-child(2)")

    rows = page.locator("#ai-body tr")
    # row 0 (AAPL, ok) is checked by default — UNCHECK it (a deliberate user choice to preserve).
    rows.nth(0).locator("input[type=checkbox]").uncheck()

    # register row 1 (NEWCO): the inline action opens the shared dialog; confirm registers it
    # via the REAL POST /api/instruments.
    reg_btn = page.locator("#ai-body").get_by_role("button", name="立即註冊")
    reg_btn.wait_for(state="visible")
    reg_btn.click()
    dialog = page.locator(".modal-backdrop").last
    dialog.locator("input.qa-symbol").wait_for(state="visible")
    confirm = dialog.get_by_role("button", name="確認", exact=True)
    expect(confirm).to_be_enabled()
    confirm.click()

    # heal is local (no second AI call): NEWCO row → checked+enabled; AAPL row stays UNCHECKED.
    expect(page.locator("#ai-body").get_by_role("button", name="立即註冊")).to_have_count(
        0, timeout=30000)
    rows2 = page.locator("#ai-body tr")
    expect(rows2.nth(0).locator("input[type=checkbox]")).not_to_be_checked()  # preserved
    expect(rows2.nth(1).locator("input[type=checkbox]")).to_be_checked()      # healed → auto-check
    assert calls["n"] == 1, f"AI preview should not re-run on register; got {calls['n']} calls"

    assert not console_errors and not page_errors, (
        f"AI checkbox-preservation flow: console={console_errors!r} page={page_errors!r}"
    )
