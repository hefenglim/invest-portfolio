"""E2E: the AI 輸入 tab — id contract, screenshot dropzone, model picker, graceful degrade (FU-D20).

Guards the dead-id fix: before FU-D20 the textarea had no ``#ai-text`` and the dropzone had no
``#ai-dropzone``, so text parse short-circuited and the upload box was inert. This drives the REAL
stack (uvicorn + SQLite + the served web/ frontend) against a fresh isolated server and asserts:
every id ``input.js`` binds exists in markup, the dropzone opens a real file chooser, and — with NO
LLM activated on the flow server — clicking 解析 renders a graceful degrade panel (never a crash),
all with ZERO console / page errors.
"""

from collections.abc import Iterator

import pytest
from playwright.sync_api import Page
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    """Re-enable loopback sockets PER TEST (pytest-socket re-bans before every test); each flow
    spawns a fresh isolated uvicorn (free-port probe + readiness poll need loopback TCP)."""
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _sink(page: Page) -> tuple[list[str], list[str]]:
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(getattr(m, "text", ""))
            if getattr(m, "type", None) == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    return console_errors, page_errors


def _open_ai_tab(page: Page, base: str) -> None:
    page.goto(base + "/trades.html", wait_until="load")
    # boot (GET /input/context) done once initAi has bound the pane + model picker.
    page.wait_for_selector("#csv-kinds .chip", state="attached")
    page.click("#tab-ai")
    page.wait_for_selector("#ai-dropzone", state="visible")


@pytest.mark.e2e
def test_ai_pane_id_contract_dropzone_and_degrade(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_golden)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    _open_ai_tab(page, base)

    # (1) id contract: every id input.js binds must exist in markup (the exact FU-D20 bug).
    for sel in ("#ai-text", "#ai-dropzone", "#ai-file-input", "#ai-model-select", "#ai-images"):
        assert page.query_selector(sel) is not None, f"missing id in markup: {sel}"

    # the model picker always carries the 自動（角色預設）default (value "").
    first_value = page.eval_on_selector("#ai-model-select option", "o => o.value")
    assert first_value == ""

    # (2) clicking the dropzone opens the browser file chooser (the click->picker path).
    with page.expect_file_chooser() as fc_info:
        page.click("#ai-dropzone")
    assert fc_info.value is not None

    # (3) with NO LLM activated on the flow server, 解析 degrades gracefully (402/409/503) —
    # a degrade panel renders, never a crash.
    page.fill("#ai-text", "在元大買 10 股 2330 @ 600")
    with page.expect_response("**/api/input/ai/preview") as resp:
        page.click("#ai-parse")
    assert resp.value.status in (402, 409, 503)
    page.wait_for_function(
        "() => ['#ai-degrade-off','#ai-degrade-quota','#ai-degrade-down']"
        ".some(s => { const n = document.querySelector(s); return n && !n.hidden; })"
    )

    # The intentional 402/409/503 degrade emits ONE expected browser "Failed to load
    # resource" network log (same precedent as the E6 login-loop 401); that benign line is
    # filtered, but any OTHER console error — and ANY uncaught page error (a real crash) —
    # still fails. The degrade path must not throw in the browser.
    def _benign(msg: str) -> bool:
        return "Failed to load resource" in msg and any(
            code in msg for code in ("402", "409", "503"))

    real_console = [e for e in console_errors if not _benign(e)]
    assert not real_console and not page_errors, (
        f"AI input flow: console={real_console!r} page={page_errors!r}"
    )
