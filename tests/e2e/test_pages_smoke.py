"""Baseline Playwright smoke (spec 19, Task 0.3): prove the harness end to end.

The harness (tests/e2e/conftest.py) serves the REAL app over a uvicorn subprocess
against a seeded golden DB and drives a headless chromium browser. This baseline asserts
the static pages load with ZERO console errors + ZERO uncaught page errors. Per-page
smokes for the other pages are added later by Phase-2 (not here) using `assert_page_ok`.
"""

import pytest
from playwright.sync_api import Page

from tests.e2e.conftest import assert_page_ok


@pytest.mark.e2e
def test_login_page_smoke(live_server: str, browser_page: object) -> None:
    """/login.html loads clean (guest mode renders it without auth)."""
    assert_page_ok(browser_page, live_server, "/login.html")


@pytest.mark.e2e
def test_index_page_smoke(live_server: str, browser_page: object) -> None:
    """/index.html (dashboard shell) loads clean from mock-data.js (self-contained)."""
    assert_page_ok(browser_page, live_server, "/index.html")


@pytest.mark.e2e
def test_settings_accounts_shell_smoke(
    live_server: str, browser_page: object
) -> None:
    """/settings-accounts.html (shell-bearing) boots clean (spec 19, Task 2.1 fix).

    This page loads shell.js + settings-users.js; the latter aliases
    `const A = window.pdAuth` at module scope and depends on `pdAuth.setSession`
    existing. After Task 2.1 made the session backend-sourced, setSession was removed
    from pdAuth; this asserts the transitional no-op shim is in place so the settings
    shell loads with ZERO console/page errors. (User add/remove is still the localStorage
    mock flow, rewired to the backend in Task 2.7 — NOT driven here.)
    """
    assert_page_ok(browser_page, live_server, "/settings-accounts.html")


@pytest.mark.e2e
def test_shell_session_guard_guest_no_redirect(
    live_server: str, browser_page: object
) -> None:
    """shell.js global scaffold (Task 2.1): the async /api/auth/session guard via pdApi.

    The golden DB seeds EMPTY auth tables -> session returns {"mode":"guest"}, so the
    shell must NOT redirect to login. This drives /index.html, waits for the shell's
    async GET /api/auth/session to RESOLVE (it lazily loads api.js then fetches), and
    asserts: (a) the session call returned 200, (b) the page stayed on index.html (no
    login redirect), and (c) ZERO console errors + ZERO uncaught page errors with the
    new async shell layered over the existing mock-data.js body render (app.js arrives
    in Task 2.2).
    """
    page = browser_page
    assert isinstance(page, Page)

    console_errors: list[str] = []
    page_errors: list[str] = []

    def _on_console(msg: object) -> None:
        if getattr(msg, "type", None) == "error":
            console_errors.append(getattr(msg, "text", repr(msg)))

    def _on_pageerror(exc: object) -> None:
        page_errors.append(str(exc))

    page.on("console", _on_console)
    page.on("pageerror", _on_pageerror)
    try:
        # Wrap navigation so we deterministically catch the shell's async session call
        # (fired on shell boot, after load) instead of racing it.
        with page.expect_response("**/api/auth/session") as resp_info:
            page.goto(live_server + "/index.html", wait_until="load")
        resp = resp_info.value
        assert resp.status == 200, f"session call status {resp.status}"
        body = resp.json()
        assert body == {"mode": "guest"}, f"unexpected session body: {body!r}"

        # Guest mode -> no redirect: still on index.html.
        page.wait_for_selector("body")
        assert page.url.endswith("index.html"), f"unexpected redirect to {page.url}"
    finally:
        page.remove_listener("console", _on_console)
        page.remove_listener("pageerror", _on_pageerror)

    assert not console_errors and not page_errors, (
        f"/index.html (async shell): console errors={console_errors!r}; "
        f"page errors={page_errors!r}"
    )
