"""E2E DEF-083: when the AI door's answer came from another model, the page says so.

#520 on the demo: the owner picked haiku-4.5, haiku was refused (HTTP 400), the chain failed
over to gemini, and the page read 「✓ 解析完成 共 1 筆草稿」 with
「模型：google/gemini-2.5-flash-lite」 — the only trace of the failure was a model name the
owner had not picked. The server now
sends ``meta.fallback_note`` (which model failed, why, who answered); this drives the REAL
page with the preview route canned (the flow server has no provider) and checks what a user
sees: an amber note above the drafts and a warning toast — and neither on a clean parse.
"""

import json
from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, Route, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory

_NOTE = "指定模型 haiku-4.5 失敗：請求內容被拒（HTTP 400）。本次改由 gemini-2.5-flash-lite 產出。"
_CSV = ("account,symbol,side,date,shares,price,daytrade,short_sale,note\n"
        "tw_broker,2884,BUY,2026-06-01,100,46,0,0,\n")


def _preview(note: str | None) -> str:
    return json.dumps({
        "previews": {"transactions": {
            "rows": [{"n": 0, "status": "ok", "reason": None, "code": None,
                      "data": {"account_id": "tw_broker", "symbol": "2884", "side": "buy",
                               "trade_date": "2026-06-01", "quantity": "100", "price": "46",
                               "fee": "20", "tax": "0", "daytrade": "0", "short_sale": "0"}}],
            "summary": {"total": 1, "ok": 1, "warn": 0, "error": 0}}},
        "unparsed": [],
        "meta": {"model": "google/gemini-2.5-flash-lite", "via": "litellm",
                 "cost_usd": "0.0005165", "fallback_note": note},
        "csv_texts": {"transactions": _CSV},
    })


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


@pytest.mark.e2e
def test_a_failover_is_shown_and_a_clean_parse_clears_it(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_golden)
    page = fresh_page
    page_errors: list[str] = []
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    replies = [_preview(_NOTE), _preview(None)]

    def _route(route: Route) -> None:
        route.fulfill(status=200, content_type="application/json", body=replies.pop(0))

    page.route("**/api/input/ai/preview", _route)
    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#csv-kinds .chip", state="attached")
    page.click("#tab-ai")
    page.wait_for_selector("#ai-dropzone", state="visible")
    page.fill("#ai-text", "台灣券商 買進 2884 100股 成交價 46")

    # 1) the answer came from another model: the note, verbatim, in the amber banner class.
    page.click("#ai-parse")
    note = page.locator("#ai-failover")
    expect(note).to_be_visible()
    expect(note).to_have_text(_NOTE)
    expect(page.locator("#ai-model")).to_have_text("google/gemini-2.5-flash-lite")
    colours = note.evaluate(
        "n => [getComputedStyle(n).color, getComputedStyle(document.documentElement)"
        ".getPropertyValue('--amber').trim()]")
    probe = page.evaluate(
        "c => { const d = document.createElement('div'); d.style.color = c;"
        " document.body.appendChild(d); const v = getComputedStyle(d).color; d.remove();"
        " return v; }", colours[1])
    assert colours[0] == probe, colours   # the amber token, not the green success colour
    toast = page.locator(".toast.toast-warn").last
    expect(toast).to_contain_text("解析完成（已改用其他模型）")
    expect(toast).to_contain_text("原因見結果上方說明")

    # 2) the next parse answered first time: no note, a plain success toast.
    page.click("#ai-parse")
    expect(note).to_be_hidden()
    expect(page.locator(".toast.toast-ok").last).to_contain_text("解析完成")
    assert page_errors == []
