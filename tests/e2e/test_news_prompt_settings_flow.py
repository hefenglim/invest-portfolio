"""E2E flow (Playwright, real server + real frontend) — the news-organizer prompt panel.

Spec: docs/spec/2026-09-10-news-prompt-settings.html (owner rulings D1(a) / D2(b) / D3(a) /
D4(a)). Drives the REAL stack against the guest golden DB and walks acceptance rows 3–7:

  * settings.html#prompts boots the third panel from GET /api/news-prompt — the official
    434-character body, badge 「與官方版相同」, meta naming the official version;
  * typing flips the badge to 「未儲存的修改」; dropping a field name raises the amber
    reminder naming it, and the reminder never blocks;
  * 儲存 → ok toast, badge 「已自訂」, the text survives a reload (it is on the server);
  * a blank 儲存 → fail toast carrying the server's 422 sentence, the textarea keeps what the
    user typed, and the SERVER body is unchanged (a reload shows the custom text again);
  * 重置回官方版 → confirmDialog (never window.confirm) → official body, badge back;
  * ZERO console errors + ZERO uncaught page errors throughout.
"""

from collections.abc import Iterator

import pytest
from playwright.sync_api import ConsoleMessage, Page
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    """Re-enable loopback sockets PER TEST (flow_server's port probe + readiness poll)."""
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


_CUSTOM = (
    "你是財經新聞整理員（自訂版）。回傳 JSON：title、news_date、body_summary、related_stocks。"
)
_MISSING_ONE = "你是財經新聞整理員（自訂版）。回傳 JSON：title、news_date、body_summary。"

_TOAST_JS = """
(want) => Array.from(document.querySelectorAll('.toast'))
    .some((t) => (t.textContent || '').indexOf(want) !== -1)
"""


def _badge(page: Page) -> str:
    return (page.locator("#news-prompt-badge").text_content() or "").strip()


def _wait_boot(page: Page) -> None:
    page.wait_for_function(
        "() => { const v = document.querySelector('#news-prompt');"
        " return v && v.value && v.value.trim().length > 0; }"
    )


@pytest.mark.e2e
def test_news_prompt_panel_edit_guard_and_reset(live_server: str, browser_page: Page) -> None:
    page = browser_page
    console_errors: list[str] = []
    page_errors: list[str] = []

    def on_console(msg: ConsoleMessage) -> None:
        if msg.type == "error":
            console_errors.append(msg.text)

    page.on("console", on_console)
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    # --- boot: the official body, the badge, the meta (acceptance 3) ---------------------
    page.goto(live_server + "/settings.html#prompts", wait_until="load")
    _wait_boot(page)
    official = page.locator("#news-prompt").input_value()
    assert "財經新聞整理員" in official
    assert _badge(page) == "與官方版相同"
    meta = page.locator("#news-prompt-meta").text_content() or ""
    assert "官方 v" in meta and "更新" in meta
    assert page.locator("#news-prompt-schema").is_hidden()

    # --- typing: unsaved badge; a dropped field name raises the reminder (acceptance 4, 6) --
    page.fill("#news-prompt", _MISSING_ONE)
    assert _badge(page) == "未儲存的修改"
    page.wait_for_selector("#news-prompt-schema:not([hidden])", state="visible")
    hint = page.locator("#news-prompt-schema").text_content() or ""
    assert "related_stocks" in hint and "不擋儲存" in hint
    page.fill("#news-prompt", _CUSTOM)
    assert page.locator("#news-prompt-schema").is_hidden()

    # --- save: ok toast, badge 已自訂, survives a reload (acceptance 4) -------------------
    page.click("#news-save")
    page.wait_for_function(_TOAST_JS, arg="已儲存")
    assert _badge(page) == "已自訂"
    page.reload(wait_until="load")
    _wait_boot(page)
    assert page.locator("#news-prompt").input_value() == _CUSTOM
    assert _badge(page) == "已自訂"

    # --- blank save: refused by the server, text kept, server body unchanged (acceptance 5) --
    page.fill("#news-prompt", "")
    page.click("#news-save")
    page.wait_for_function(_TOAST_JS, arg="不可為空白")
    assert page.locator("#news-prompt").input_value() == ""  # never cleared, never replaced
    page.reload(wait_until="load")
    _wait_boot(page)
    assert page.locator("#news-prompt").input_value() == _CUSTOM  # the server kept the custom text

    # --- reset: confirmDialog, then the official body and the badge come back (acceptance 7) --
    page.click("#news-reset")
    page.wait_for_selector(".modal .btn-danger", state="visible")
    page.click(".modal .btn-danger")
    page.wait_for_function(_TOAST_JS, arg="已重置")
    page.wait_for_function(
        "() => (document.querySelector('#news-prompt-badge').textContent || '').trim()"
        " === '與官方版相同'"
    )
    assert page.locator("#news-prompt").input_value() == official

    # Chromium logs ONE 「Failed to load resource … 422」 line for the blank save the flow
    # deliberately provokes — that is the server doing its job (same precedent as
    # test_ledger_correction_doors_flow.py). Exactly one, and no other console or page error.
    refused = [e for e in console_errors if "Failed to load resource" in e and "422" in e]
    real_console = [e for e in console_errors if e not in refused]
    assert len(refused) == 1, f"expected exactly one refused PUT in the console: {refused!r}"
    assert not real_console and not page_errors, (
        f"console errors={real_console!r}; page errors={page_errors!r}"
    )
