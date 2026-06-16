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
    """/index.html (dashboard) loads clean from the REAL /api/dashboard (Task 2.2).

    After Task 2.2, index.html no longer loads mock-data.js / history-mock.js: app.js,
    charts.js and alerts.js all boot off the single shared window.pdDashboard promise
    (one GET /api/dashboard against the golden DB). This asserts the full async wiring
    renders with ZERO console errors + ZERO uncaught page errors — catching a botched
    async conversion, a Decimal-string `.toFixed` TypeError, sparkline/echarts breakage,
    or an undefined insight field. ECharts loads from the jsdelivr CDN (the browser
    subprocess has network); the page must be console-error-clean WITH echarts available.

    Waits for a POST-render selector (.kpi-card, produced by renderKpis only after the
    /api/dashboard payload resolves) so the assertion observes the full async render —
    not just the empty shell — before checking the console/pageerror sinks.
    """
    assert_page_ok(browser_page, live_server, "/index.html", root_selector=".kpi-card")


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
def test_symbol_detail_drawer_held_smoke(
    live_server: str, browser_page: object
) -> None:
    """Symbol-detail drawer wired to /api/symbol/{symbol}/detail (spec 19, Task 2.3).

    The drawer is opened by INTERACTION, not a page load, so this navigates /index.html
    (golden DB), waits for the dashboard render, then triggers the drawer via
    window.pdOpenSymbol('2330') (2330 is a held golden symbol with one stored price row).
    The drawer fetches BOTH /api/symbol/2330/detail AND the shared /api/dashboard promise,
    then renders head + chart (#sd-chart) + the holding sections from Decimal-STRING money.

    Asserts the FULL async wiring renders with ZERO console errors + ZERO uncaught page
    errors — catching the async boot, the chart rewire (price_history/trade_events), and
    any undefined-field / Decimal-string `.toFixed` TypeError.
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
        page.goto(live_server + "/index.html", wait_until="load")
        page.wait_for_selector(".kpi-card")  # dashboard async render landed
        page.evaluate("() => window.pdOpenSymbol('2330')")
        page.wait_for_selector(".sd-drawer")
        page.wait_for_selector("#sd-chart")  # chart rendered from real price_history
        # The head re-renders with the holding summary after both fetches resolve.
        page.wait_for_selector(".sd-drawer .sym-name")
    finally:
        page.remove_listener("console", _on_console)
        page.remove_listener("pageerror", _on_pageerror)

    assert not console_errors and not page_errors, (
        f"symbol drawer (held 2330): console errors={console_errors!r}; "
        f"page errors={page_errors!r}"
    )


@pytest.mark.e2e
def test_symbol_detail_drawer_watchlist_smoke(
    live_server: str, browser_page: object
) -> None:
    """Drawer watchlist (unheld) variant (spec 19, Task 2.3).

    MSFT is not an instrument in the golden DB, so /api/symbol/MSFT/detail returns
    cost_basis=null + price_history.available=false and it is absent from /api/dashboard
    holdings -> the rich holding `h` is null. The drawer must NOT crash: it renders the
    '非持倉標的' head + a chart-only / empty-price variant, skipping the holding sections.
    Asserts ZERO console + ZERO page errors over that null-holding path.
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
        page.goto(live_server + "/index.html", wait_until="load")
        page.wait_for_selector(".kpi-card")
        page.evaluate("() => window.pdOpenSymbol('MSFT')")
        page.wait_for_selector(".sd-drawer")
        # Unheld -> '非持倉標的' badge in the head + chart-only body (no holding sections).
        page.wait_for_selector(".sd-drawer .sd-empty")
    finally:
        page.remove_listener("console", _on_console)
        page.remove_listener("pageerror", _on_pageerror)

    assert not console_errors and not page_errors, (
        f"symbol drawer (unheld MSFT): console errors={console_errors!r}; "
        f"page errors={page_errors!r}"
    )


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
    async shell over the Task-2.2 async body render (app/charts/alerts boot off the
    shared /api/dashboard promise).
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


@pytest.mark.e2e
def test_trades_ledger_smoke(live_server: str, browser_page: object) -> None:
    """/trades.html ledger view wired to the REAL /api/ledgers/* (spec 19, Task 2.4).

    After Task 2.4, ledger.js drops its inline window.LEDGER_DATA mock and instead
    fetches the four append-only ledgers in PARALLEL through pdApi
    (transactions/dividends/fx/openings), then renders the four tabs. The golden DB
    seeds a 2330 BUY, an AAPL BUY, a 2330 CASH dividend and a TWD->USD fx conversion
    (no opening row), so the default 交易 tab renders at least one expandable row.

    trades.html ALSO loads input.js (still the Task-2.6 mock) and the input-mock-data
    glue; this asserts the now-wired ledger coexists with that mock and the WHOLE page
    is console-error-clean. Waits for a POST-fetch selector (#tx-body tr.expandable,
    produced only after /api/ledgers/transactions resolves) so the assertion observes
    the populated table — catching a Decimal-string `.toFixed` TypeError, the implied_rate
    rewire, an undefined field, or an unhandled fetch rejection.
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
        page.goto(live_server + "/trades.html", wait_until="load")
        # Transactions tab is the default; one expandable row lands once the parallel
        # /api/ledgers/* fetches resolve (golden DB has a 2330 + an AAPL buy).
        page.wait_for_selector("#tx-body tr.expandable")
    finally:
        page.remove_listener("console", _on_console)
        page.remove_listener("pageerror", _on_pageerror)

    assert not console_errors and not page_errors, (
        f"/trades.html (ledger wired): console errors={console_errors!r}; "
        f"page errors={page_errors!r}"
    )


@pytest.mark.e2e
def test_trades_ledger_account_filter_keeps_rows(
    live_server: str, browser_page: object
) -> None:
    """Clicking a specific-account chip filters by account_id, NOT the display name.

    Regression guard (spec 19, Task 2.4 senior review): the chips + the byAccount filter
    used to match the hardcoded Chinese DISPLAY name against rows whose `account` carries
    the ENGLISH name ("TW Broker"), so any specific-account chip filtered to EMPTY against
    real data. The default 全部 view masked it, so the load-smoke missed it. The fix keys
    the chips + predicate on the stable `account_id`; this asserts an INTERACTION (click)
    keeps the table NON-empty.

    The golden DB seeds a 2330 BUY under `tw_broker`, so its chip is rendered with a
    `data-account-id="tw_broker"` attribute. Navigate /trades.html, wait for the default
    transactions tab to populate, click that chip, then assert (a) at least one
    `#tx-body tr` row survives the filter (did NOT go empty) and (b) ZERO console + page
    errors over the interaction.
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
        page.goto(live_server + "/trades.html", wait_until="load")
        # Transactions tab populated once the parallel /api/ledgers/* fetches resolve.
        page.wait_for_selector("#tx-body tr.expandable")
        # The tw_broker chip is built only after boot() re-runs initFilters off real rows.
        page.wait_for_selector('#ledger-filters .chip[data-account-id="tw_broker"]')
        page.click('#ledger-filters .chip[data-account-id="tw_broker"]')
        # Filter keyed on account_id -> 2330 (tw_broker) rows survive (NOT empty).
        page.wait_for_selector("#tx-body tr.expandable")
        rows = page.query_selector_all("#tx-body tr.expandable")
        assert rows, "account-id filter (tw_broker) went EMPTY — regression not fixed"
    finally:
        page.remove_listener("console", _on_console)
        page.remove_listener("pageerror", _on_pageerror)

    assert not console_errors and not page_errors, (
        f"/trades.html (account filter): console errors={console_errors!r}; "
        f"page errors={page_errors!r}"
    )


@pytest.mark.e2e
def test_ledger_page_smoke(live_server: str, browser_page: object) -> None:
    """/ledger.html (standalone ledger view) wired to /api/ledgers/* (spec 19, Task 2.4).

    The alternate ledger page owns its own tabs (it has no #pane-ldiv) and carries a
    default date-range filter (2026-01-01 .. 2026-06-11) that spans all golden flows.
    Asserts the four parallel fetches render the default 交易 tab with ZERO console +
    ZERO page errors, waiting for the populated #tx-body row.
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
        page.goto(live_server + "/ledger.html", wait_until="load")
        page.wait_for_selector("#tx-body tr.expandable")
    finally:
        page.remove_listener("console", _on_console)
        page.remove_listener("pageerror", _on_pageerror)

    assert not console_errors and not page_errors, (
        f"/ledger.html (ledger wired): console errors={console_errors!r}; "
        f"page errors={page_errors!r}"
    )
