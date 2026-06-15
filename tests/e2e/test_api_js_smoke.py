"""Playwright smoke (spec 19, Task 0.2): prove `web/api.js` — the single fetch layer.

Strategy (deterministic, no real backend state): navigate to a served static page
(`/login.html`), inject the served `/api.js` via `page.add_script_tag(url=...)`, then
`page.route("**/api/**", ...)` to STUB crafted responses and `page.evaluate(...)` to
drive `window.pdApi`. This isolates the fetch-layer contract from backend data.

Coverage:
  1. Error envelope → PdApiError (status/code/message/field; name === "PdApiError").
  2. 401 → window.location.replace('login.html') (the ONE redirect site).
  3. 402 → re-thrown WITHOUT redirect (degraded-state path for the AI block).
  4. Decimal passthrough → money stays a STRING (no parseFloat/Number/+ coercion).
  5. abortable(key) → a second same-key call aborts the first in-flight request.

A fresh page is used per test so 401's real navigation never bleeds across tests.
"""

import pytest
from playwright.sync_api import Page


def _fresh_api_page(browser_page: Page, live_server: str) -> Page:
    """Open a fresh page on /login.html (served, clean) and inject the served /api.js.

    A new context+page per test keeps the 401-redirect test's navigation from leaking
    into the others, and gives each test its own `page.route` handler registry. (The
    session `browser_page` is created via `browser.new_page()`, whose owning context
    forbids `context.new_page()`; so we make a fresh context off the same browser.)
    """
    browser = browser_page.context.browser
    assert browser is not None
    context = browser.new_context()
    page = context.new_page()
    page.goto(live_server + "/login.html", wait_until="load")
    page.add_script_tag(url="/api.js")
    # Sanity: the fetch layer is present before we exercise it.
    assert page.evaluate("() => typeof window.pdApi === 'object' && !!window.pdApi") is True
    assert page.evaluate("() => typeof window.PdApiError === 'function'") is True
    return page


@pytest.mark.e2e
def test_error_envelope_becomes_pdapierror(live_server: str, browser_page: Page) -> None:
    """422 envelope → rejected PdApiError carrying {status, code, message, field}."""
    page = _fresh_api_page(browser_page, live_server)
    try:
        page.route(
            "**/api/**",
            lambda route: route.fulfill(
                status=422,
                content_type="application/json",
                body='{"error":{"code":"unprocessable","message":"bad","field":"qty"}}',
            ),
        )
        result = page.evaluate(
            """async () => {
                try {
                    await window.pdApi.get('/api/anything');
                    return { ok: true };
                } catch (e) {
                    return {
                        ok: false,
                        name: e.name,
                        status: e.status,
                        code: e.code,
                        message: e.message,
                        field: e.field,
                    };
                }
            }"""
        )
        assert result["ok"] is False
        assert result["name"] == "PdApiError"
        assert result["status"] == 422
        assert result["code"] == "unprocessable"
        assert result["message"] == "bad"
        assert result["field"] == "qty"
    finally:
        page.context.close()


@pytest.mark.e2e
def test_401_redirects_to_login(live_server: str, browser_page: Page) -> None:
    """401 → window.location.replace('login.html'); page navigates to login.html."""
    page = _fresh_api_page(browser_page, live_server)
    try:
        page.route(
            "**/api/**",
            lambda route: route.fulfill(
                status=401,
                content_type="application/json",
                body='{"error":{"code":"unauthorized","message":"請先登入"}}',
            ),
        )
        # Fire the call; it triggers a real navigation to login.html. Don't await the
        # promise in-page (the navigation tears down the JS context) — observe the URL.
        page.evaluate("() => { window.pdApi.get('/api/protected').catch(() => {}); }")
        page.wait_for_url("**/login.html")
        assert page.url.endswith("login.html")
    finally:
        page.context.close()


@pytest.mark.e2e
def test_402_rethrows_without_redirect(live_server: str, browser_page: Page) -> None:
    """402 budget_exceeded → re-thrown PdApiError; the page does NOT navigate."""
    page = _fresh_api_page(browser_page, live_server)
    try:
        url_before = page.url
        page.route(
            "**/api/**",
            lambda route: route.fulfill(
                status=402,
                content_type="application/json",
                body='{"error":{"code":"budget_exceeded","message":"budget gone"}}',
            ),
        )
        result = page.evaluate(
            """async () => {
                try {
                    await window.pdApi.get('/api/insights/run');
                    return { ok: true };
                } catch (e) {
                    return { ok: false, name: e.name, status: e.status, code: e.code };
                }
            }"""
        )
        assert result["ok"] is False
        assert result["name"] == "PdApiError"
        assert result["status"] == 402
        assert result["code"] == "budget_exceeded"
        # No redirect for 402 — URL is unchanged (still /login.html).
        assert page.url == url_before
    finally:
        page.context.close()


@pytest.mark.e2e
def test_decimal_passthrough_is_string(live_server: str, browser_page: Page) -> None:
    """200 money → resolves with the value as a STRING (proves no coercion)."""
    page = _fresh_api_page(browser_page, live_server)
    try:
        page.route(
            "**/api/**",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body='{"market_value":"123456.789"}',
            ),
        )
        result = page.evaluate(
            """async () => {
                const data = await window.pdApi.get('/api/dashboard');
                return {
                    value: data.market_value,
                    type: typeof data.market_value,
                };
            }"""
        )
        assert result["type"] == "string"
        assert result["value"] == "123456.789"
    finally:
        page.context.close()


@pytest.mark.e2e
def test_abortable_cancels_prior_inflight(live_server: str, browser_page: Page) -> None:
    """abortable('k') twice → the first in-flight request rejects with an AbortError."""
    page = _fresh_api_page(browser_page, live_server)
    try:
        # Hang every /api request so the first call is genuinely in-flight when the
        # second abortable('k') aborts it. We never fulfill — the abort wins the race.
        page.route("**/api/**", lambda route: None)
        result = page.evaluate(
            """async () => {
                const c1 = window.pdApi.abortable('k');
                const p1 = window.pdApi.get('/api/slow', null, { signal: c1.signal });
                const first = p1.then(() => 'resolved').catch((e) => e.name);
                window.pdApi.abortable('k');   // same key → aborts c1
                return await first;
            }"""
        )
        assert result == "AbortError"
    finally:
        page.context.close()
