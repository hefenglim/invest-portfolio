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
def test_login_page_smoke(live_server: str, browser_page: Page) -> None:
    """/login.html loads clean and makes NO /api/* call on load (spec 19, 9.x).

    After wiring login to POST /api/auth/login, the page loads api.js + an inline auth
    script, but the inline script must NOT fire any network call on load (the shell — not
    this page — routes signed-out users here). This asserts: (a) the form is present
    (#login-btn / #login-user / #login-pass), (b) the page stays on /login.html (no
    redirect), (c) ZERO /api/* requests were issued on load, and (d) ZERO console + page
    errors. A login ATTEMPT is NOT triggered here (a 401 would emit a "Failed to load
    resource: 401" console message); the 401 logic is covered by the auth.py contract
    tests + the api.js 401-redirect smoke.
    """
    page = browser_page
    assert isinstance(page, Page)

    console_errors: list[str] = []
    page_errors: list[str] = []
    api_requests: list[str] = []

    def _on_console(msg: object) -> None:
        if getattr(msg, "type", None) == "error":
            console_errors.append(getattr(msg, "text", repr(msg)))

    def _on_pageerror(exc: object) -> None:
        page_errors.append(str(exc))

    def _on_request(req: object) -> None:
        url = getattr(req, "url", "")
        if "/api/" in url:
            api_requests.append(url)

    page.on("console", _on_console)
    page.on("pageerror", _on_pageerror)
    page.on("request", _on_request)
    try:
        page.goto(live_server + "/login.html", wait_until="load")
        # Form markup present (the page rendered, not a redirect).
        page.wait_for_selector("#login-btn")
        page.wait_for_selector("#login-user")
        page.wait_for_selector("#login-pass")
        assert page.url.endswith("login.html"), f"unexpected redirect to {page.url}"
    finally:
        page.remove_listener("console", _on_console)
        page.remove_listener("pageerror", _on_pageerror)
        page.remove_listener("request", _on_request)

    assert not api_requests, f"login.html fired /api/* on load: {api_requests!r}"
    assert not console_errors and not page_errors, (
        f"/login.html: console errors={console_errors!r}; page errors={page_errors!r}"
    )


@pytest.mark.e2e
def test_index_page_smoke(live_server: str, browser_page: Page) -> None:
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
    live_server: str, browser_page: Page
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
    live_server: str, browser_page: Page
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
    live_server: str, browser_page: Page
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
    live_server: str, browser_page: Page
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
def test_trades_ledger_smoke(live_server: str, browser_page: Page) -> None:
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
    live_server: str, browser_page: Page
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
def test_instruments_page_smoke(live_server: str, browser_page: Page) -> None:
    """/instruments.html list wired to the REAL /api/instruments (spec 19, Task 2.5).

    After Task 2.5, instruments.js drops its inline window.INSTRUMENTS_DATA mock and
    instead fetches GET /api/instruments through pdApi, then renders the list table from
    Decimal-STRING money (last / chg_pct / target_low via window.fmt). The golden DB
    seeds two instruments (2330 TWSE + AAPL US), so the table renders at least one row.

    Waits for a POST-fetch selector (#inst-body tr, produced only after the
    /api/instruments fetch resolves) so the assertion observes the populated table — not
    just the empty shell — catching a Decimal-string `.toFixed`/`<=` TypeError, an
    undefined field, or an unhandled fetch rejection. ZERO console + ZERO page errors.
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
        page.goto(live_server + "/instruments.html", wait_until="load")
        # One row lands once GET /api/instruments resolves (golden DB has 2330 + AAPL).
        page.wait_for_selector("#inst-body tr")
    finally:
        page.remove_listener("console", _on_console)
        page.remove_listener("pageerror", _on_pageerror)

    assert not console_errors and not page_errors, (
        f"/instruments.html (list wired): console errors={console_errors!r}; "
        f"page errors={page_errors!r}"
    )


@pytest.mark.e2e
def test_instruments_quick_add_duplicate_surfaces_backend_error(
    live_server: str, browser_page: Page
) -> None:
    """One-step add hits the REAL POST /api/instruments/quick (2026-07-02).

    Deterministic + provider-free path: adding golden-registered 2330 answers 409
    duplicate_symbol BEFORE any quote fetch, so the e2e never touches the network.
    Asserts (a) the button POSTs the real endpoint and receives the 409, (b) the
    backend message surfaces as a fail toast, and (c) ZERO console + page errors
    over the interaction (an unhandled POST rejection would fail this).
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
        page.goto(live_server + "/instruments.html", wait_until="load")
        page.wait_for_selector("#inst-body tr")  # list landed (page fully booted)
        page.fill("#new-symbol", "2330")
        # Market defaults to TW; the one-step add POSTs /api/instruments/quick.
        with page.expect_response("**/api/instruments/quick") as resp_info:
            page.click("#quick-add-btn")
        assert resp_info.value.status == 409, f"quick status {resp_info.value.status}"
        # The backend duplicate message surfaces as a fail toast.
        page.wait_for_selector(".toast-fail")
        assert "已註冊" in page.inner_text(".toast-fail")
    finally:
        page.remove_listener("console", _on_console)
        page.remove_listener("pageerror", _on_pageerror)

    # Chrome logs a console "error" for ANY non-2xx fetch ("Failed to load
    # resource: ... 409") — that is network logging of the DELIBERATE 409, not an
    # app error. Filter it; everything else (and all page errors) stays fatal.
    app_errors = [e for e in console_errors if "Failed to load resource" not in e]
    assert not app_errors and not page_errors, (
        f"/instruments.html (quick add): console errors={app_errors!r}; "
        f"page errors={page_errors!r}"
    )


@pytest.mark.e2e
def test_input_page_smoke(live_server: str, browser_page: Page) -> None:
    """/input.html boots from the REAL /api/input/context (spec 19, Task 2.6).

    After Task 2.6 input.js drops window.INPUT_DATA and sources ALL form structural
    data (accounts / instruments / fee-rule context / holdings) from GET
    /api/input/context, then runs the manual tab's first live preview against
    /api/input/manual/preview. The golden DB seeds the four accounts + 2330/AAPL
    instruments + a tw_broker 2330 holding, so the account/symbol dropdowns populate
    and the seeded default draft (2330 買 1,000 @ 612.5) previews cleanly.

    Waits for a post-boot selector (#m-account option, populated only after the context
    fetch resolves) so the assertion observes the full async boot — then asserts ZERO
    console errors + ZERO uncaught page errors (catching a Decimal-string `.toFixed`
    TypeError on the server-fed preview fee/tax/total, an undefined field, or an
    unhandled fetch rejection).
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
        page.goto(live_server + "/input.html", wait_until="load")
        # Account dropdown populated only after GET /api/input/context resolves. <option>
        # nodes live inside a collapsed <select> (never "visible"), so wait for ATTACHED.
        page.wait_for_selector("#m-account option", state="attached")
    finally:
        page.remove_listener("console", _on_console)
        page.remove_listener("pageerror", _on_pageerror)

    assert not console_errors and not page_errors, (
        f"/input.html (context wired): console errors={console_errors!r}; "
        f"page errors={page_errors!r}"
    )


@pytest.mark.e2e
def test_input_manual_preview_roundtrip(
    live_server: str, browser_page: Page
) -> None:
    """Manual tab live preview hits the REAL POST /api/input/manual/preview (Task 2.6).

    The preview round-trip: navigate /input.html (golden DB), wait for the context-fed
    account dropdown, fill the manual form (account tw_broker / symbol 2330 / 1000 @
    612.5 — all golden), and trigger a preview by editing the price. Then assert (a) the
    real POST /api/input/manual/preview returns 200, (b) the preview card renders the
    server-computed total (#m-pc-value is non-empty, NOT the em-dash null glyph — i.e.
    the Decimal-STRING fee/tax/total went through fmt and the confirm enabled), and
    (c) ZERO console + page errors over the interaction.
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
        page.goto(live_server + "/input.html", wait_until="load")
        # <option> nodes are inside a collapsed <select> -> wait for ATTACHED, not visible.
        page.wait_for_selector("#m-account option", state="attached")  # context landed
        # The form boots EMPTY now (design-stub prefill retired 2026-07-02): fill every
        # field; the preview POST fires only once the local checks pass (price last).
        page.fill("#m-symbol", "2330")
        page.fill("#m-shares", "1000")
        with page.expect_response("**/api/input/manual/preview") as resp_info:
            page.fill("#m-price", "612.5")
        assert resp_info.value.status == 200, f"preview status {resp_info.value.status}"
        # The preview card big value renders the server total (not the null em-dash).
        page.wait_for_function(
            "() => { const v = document.querySelector('#m-pc-value');"
            " const t = v && v.textContent ? v.textContent.trim() : '';"
            " return t && t !== '\\u2014'; }"
        )
    finally:
        page.remove_listener("console", _on_console)
        page.remove_listener("pageerror", _on_pageerror)

    assert not console_errors and not page_errors, (
        f"/input.html (manual preview): console errors={console_errors!r}; "
        f"page errors={page_errors!r}"
    )


@pytest.mark.e2e
def test_ledger_page_smoke(live_server: str, browser_page: Page) -> None:
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


@pytest.mark.e2e
def test_settings_llm_page_smoke(live_server: str, browser_page: Page) -> None:
    """/settings-llm.html boots from the REAL /api/llm/config (spec 19, Task 2.7a).

    settings-llm.js drops its inline window.LLM_DATA mock and boots off GET
    /api/llm/config -> { models, roles, quota, usage }. The golden DB seeds the LLM
    tables to the AI-off state (NO models, all roles NULL, quota remaining "0"), so this
    exercises the empty/graceful path: the model table stays empty, the role selects show
    "（空 = 關閉）", and the quota chip + value render from the Decimal-STRING quota.

    Waits for #quota-value to carry text (rendered only after the config fetch resolves —
    the quota object is always present), proving the full async boot landed, then asserts
    ZERO console + ZERO page errors. This catches the C2 fix: the former bare
    `remaining.toFixed(2)` / `m.price_in.toFixed(2)` would TypeError on a Decimal STRING;
    routed through window.fmt they do not.
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
        page.goto(live_server + "/settings-llm.html", wait_until="load")
        # #quota-value gets text only after GET /api/llm/config resolves (renderQuota).
        page.wait_for_function(
            "() => { const v = document.querySelector('#quota-value');"
            " return v && v.textContent && v.textContent.trim().length > 0; }"
        )
    finally:
        page.remove_listener("console", _on_console)
        page.remove_listener("pageerror", _on_pageerror)

    assert not console_errors and not page_errors, (
        f"/settings-llm.html (llm config wired): console errors={console_errors!r}; "
        f"page errors={page_errors!r}"
    )


@pytest.mark.e2e
def test_settings_scheduler_page_smoke(live_server: str, browser_page: Page) -> None:
    """/settings-scheduler.html boots from the REAL /api/scheduler/* (spec 19, Task 2.7a).

    settings-scheduler.js drops window.SCHED_DATA and boots off GET /api/scheduler/jobs +
    GET /api/scheduler/runs (parallel). The golden DB seeds the registry jobs (so the jobs
    table renders rows) and an EMPTY run log (so the history table renders empty cleanly).

    Waits for a populated #jobs-body tr (rendered only after the jobs fetch resolves), then
    asserts ZERO console + ZERO page errors. This catches war-game Finding 8: a run's
    Decimal-STRING cost_usd "0"/"0.00" is truthy, so the nil-check is `== null` and the
    value is shown via f.num — never `bareString.toFixed`.
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
        page.goto(live_server + "/settings-scheduler.html", wait_until="load")
        # One job row lands once GET /api/scheduler/jobs resolves (registry-seeded jobs).
        page.wait_for_selector("#jobs-body tr")
    finally:
        page.remove_listener("console", _on_console)
        page.remove_listener("pageerror", _on_pageerror)

    assert not console_errors and not page_errors, (
        f"/settings-scheduler.html (scheduler wired): console errors={console_errors!r}; "
        f"page errors={page_errors!r}"
    )


@pytest.mark.e2e
def test_settings_datasources_page_smoke(
    live_server: str, browser_page: Page
) -> None:
    """/settings-datasources.html boots from the REAL /api/datasources (Task 2.7a).

    settings-datasources.js drops window.DATASOURCES_DATA and boots off GET
    /api/datasources -> { sources, account_fallbacks, account_names }. The golden DB seeds
    the data_sources catalog (so source groups render) and the four accounts (so the
    per-account fallback cards render). The render is robust to all real type/status/auth
    values (stock/dividend/sentiment/fx/macro/trends/news; ok/error/off/unknown/pending/
    blocked; none/apikey/oauth).

    Waits for a rendered source section (#sources-wrap .ds-section, produced only after the
    fetch resolves), then asserts ZERO console + ZERO page errors.
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
        page.goto(live_server + "/settings-datasources.html", wait_until="load")
        # A source section lands once GET /api/datasources resolves (seeded catalog).
        page.wait_for_selector("#sources-wrap .ds-section")
    finally:
        page.remove_listener("console", _on_console)
        page.remove_listener("pageerror", _on_pageerror)

    assert not console_errors and not page_errors, (
        f"/settings-datasources.html (datasources wired): console errors={console_errors!r}; "
        f"page errors={page_errors!r}"
    )


@pytest.mark.e2e
def test_settings_combined_page_smoke(live_server: str, browser_page: Page) -> None:
    """/settings.html (combined) loads ALL settings scripts console-clean (Task 2.7a/b).

    settings.html loads api.js + every settings script: the NOW-WIRED ones
    (settings-llm/scheduler/datasources from 2.7a + settings-prompts/users from 2.7b, each
    booting off its real /api/* endpoint) AND the still-mock alerts (settings-alerts, 2.7c).
    This asserts the wired sections coexist with the remaining mock script on a single page
    with ZERO console + ZERO page errors.

    Waits for the wired LLM quota value, a scheduler job row, a datasource section, the
    prompts variable-total panel (#vars-panel, mounted only after GET /api/prompt-vars
    resolves), AND the system-prompt textarea carrying text (GET /api/system-prompt) — so
    all the async boots, including the two newly-wired 2.7b ones, landed before the sinks
    are checked.
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
        page.goto(live_server + "/settings.html", wait_until="load")
        # All wired boots landed: scheduler jobs + datasource section + LLM quota (2.7a) AND
        # the prompts vars panel + system-prompt textarea (2.7b). settings.html tabs all
        # sections into one page; only the active tab's nodes are VISIBLE, so wait for
        # ATTACHED (the rows/sections exist once each boot resolves).
        page.wait_for_selector("#jobs-body tr", state="attached")
        page.wait_for_selector("#sources-wrap .ds-section", state="attached")
        page.wait_for_selector("#vars-panel", state="attached")  # GET /api/prompt-vars landed
        page.wait_for_function(
            "() => { const v = document.querySelector('#quota-value');"
            " return v && v.textContent && v.textContent.trim().length > 0; }"
        )
        # system-prompt textarea is filled only after GET /api/system-prompt resolves.
        page.wait_for_function(
            "() => { const v = document.querySelector('#sys-prompt');"
            " return v && v.value && v.value.trim().length > 0; }"
        )
    finally:
        page.remove_listener("console", _on_console)
        page.remove_listener("pageerror", _on_pageerror)

    assert not console_errors and not page_errors, (
        f"/settings.html (all settings wired): console errors={console_errors!r}; "
        f"page errors={page_errors!r}"
    )


@pytest.mark.e2e
def test_settings_prompts_page_smoke(live_server: str, browser_page: Page) -> None:
    """/settings-prompts.html boots from /api/prompt-vars + /api/system-prompt (Task 2.7b).

    settings-prompts.js drops its inline window.PROMPTS_DATA + (vars.js) PD_VARS mocks and
    boots async: V.load() fetches GET /api/prompt-vars (the 29-var registry with per-var
    tier metadata) and GET /api/system-prompt fills the editor. The golden DB seeds the
    default system prompt and the finmind source at its default tier, so the variable-total
    panel renders rows and tier-locked FinMind chips (if any) are greyed out (option.disabled
    in the insert-variable selects + .tier-locked rows in the total table).

    Waits for the system-prompt textarea to carry text (proving GET /api/system-prompt
    resolved) AND the #vars-panel variable-total to mount with rendered rows (proving GET
    /api/prompt-vars resolved + the registry rendered), then asserts ZERO console + ZERO
    page errors — catching a botched async boot, a missing tier field, or an unhandled
    fetch rejection.
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
        page.goto(live_server + "/settings-prompts.html", wait_until="load")
        # system-prompt editor filled only after GET /api/system-prompt resolves.
        page.wait_for_function(
            "() => { const v = document.querySelector('#sys-prompt');"
            " return v && v.value && v.value.trim().length > 0; }"
        )
        # The variable-total panel (and its rows) mount only after GET /api/prompt-vars
        # resolves and V.CATEGORIES is populated — proving the registry render + tier pills.
        # #vars-panel is a collapsed <details>, so its rows are ATTACHED but not visible.
        page.wait_for_selector("#vars-panel .vars-table tbody tr", state="attached")
    finally:
        page.remove_listener("console", _on_console)
        page.remove_listener("pageerror", _on_pageerror)

    assert not console_errors and not page_errors, (
        f"/settings-prompts.html (prompts/vars wired): console errors={console_errors!r}; "
        f"page errors={page_errors!r}"
    )


@pytest.mark.e2e
def test_evolution_config_panel_roundtrip(
    live_server: str, browser_page: Page
) -> None:
    """自我進化設定 panel wired to GET/PUT /api/evolution-config (spec 19 defer ①).

    settings-prompts.js drops the localStorage 'pd_evolution_cfg' read/write and instead
    boots the 自我進化設定 panel off GET /api/evolution-config, then saves via PUT (a
    read-then-PUT that preserves the non-panel knobs — horizon_basis / defer_limit_days /
    shadow_on_alert — and sends gap_alert_pp as a Decimal STRING). The golden DB seeds the
    evolution_config defaults (min_samples=8) via ensure_composer_seeded.

    This proves a REAL backend round-trip (not localStorage): navigate /settings-prompts.html
    (no tabs -> the panel mounts directly), assert (1) the min_samples input reflects the
    backend GET (default 8); (2) change it to 12 and click 儲存; (3) the PUT fired + returned
    200; (4) reload and assert the value PERSISTED to 12 (a fresh GET returns it). Asserts
    ZERO console + ZERO page errors throughout.
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
        page.goto(live_server + "/settings-prompts.html", wait_until="load")
        # Panel mounts (with its fields populated from GET /api/evolution-config) before
        # the page dispatches pd-prompts-mounted, since the IIFE is awaited in boot(). The
        # standalone prompts page has no shell CSS, so the panel's fields are ATTACHED but
        # not "visible" to Playwright — drive them via state="attached" + evaluate (the
        # established pattern in this file for tabbed/hidden nodes).
        min_sel = '[data-evo-field="min_samples"]'
        page.wait_for_selector(min_sel, state="attached")
        # (1) The field reflects the backend GET (golden default min_samples=8).
        assert page.input_value(min_sel) == "8", (
            f"min_samples did not reflect the backend GET: {page.input_value(min_sel)!r}"
        )

        # (2) change to a different valid value (the save handler reads inp.value on click),
        # then (3) save -> PUT /api/evolution-config returns 200.
        page.eval_on_selector(
            min_sel, "(el) => { el.value = '12'; }"
        )
        with page.expect_response("**/api/evolution-config") as resp_info:
            page.eval_on_selector("[data-evo-save]", "(btn) => btn.click()")
        resp = resp_info.value
        assert resp.request.method == "PUT", f"expected a PUT, got {resp.request.method}"
        assert resp.status == 200, f"PUT /api/evolution-config status {resp.status}"
        # The non-panel knobs survive the read-then-PUT (lossless round-trip).
        body = resp.json()
        assert body.get("min_samples") == 12, f"PUT did not persist min_samples: {body!r}"
        assert body.get("horizon_basis") == "trading_days", (
            f"read-then-PUT dropped a non-panel knob: {body!r}"
        )

        # (4) reload -> a fresh GET must return the changed value (proves the backend
        # round-trip, NOT localStorage which has been removed).
        page.goto(live_server + "/settings-prompts.html", wait_until="load")
        page.wait_for_selector(min_sel, state="attached")
        assert page.input_value(min_sel) == "12", (
            "min_samples did not PERSIST across reload — round-trip to the backend failed "
            f"(got {page.input_value(min_sel)!r})"
        )
    finally:
        page.remove_listener("console", _on_console)
        page.remove_listener("pageerror", _on_pageerror)

    assert not console_errors and not page_errors, (
        f"/settings-prompts.html (evolution-config round-trip): "
        f"console errors={console_errors!r}; page errors={page_errors!r}"
    )


@pytest.mark.e2e
def test_settings_accounts_users_wired_smoke(
    live_server: str, browser_page: Page
) -> None:
    """/settings-accounts.html users list wired to GET /api/users (spec 19, Task 2.7b).

    settings-users.js drops the localStorage pdAuth CRUD and boots off GET /api/users; the
    page now loads api.js BEFORE shell.js. The golden DB seeds EMPTY auth tables (guest
    mode = ZERO users), so the users panel must render the empty-state affordance ("尚無
    授權用戶 … 新增第一個用戶後即啟用帳密保護") rather than a table — and do so with ZERO
    console + ZERO page errors. This supersedes the Task-2.1 shell-only smoke: the page no
    longer depends on the (now-removed) pdAuth.setSession shim.

    Waits for #users-wrap to carry the empty-state text (rendered only after GET /api/users
    resolves to []), then asserts ZERO console + ZERO page errors.
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
        page.goto(live_server + "/settings-accounts.html", wait_until="load")
        # Golden DB has ZERO users -> GET /api/users returns [] -> the empty-state div
        # renders inside #users-wrap (proves the async boot landed, not the empty shell).
        page.wait_for_selector("#users-wrap .users-empty")
    finally:
        page.remove_listener("console", _on_console)
        page.remove_listener("pageerror", _on_pageerror)

    assert not console_errors and not page_errors, (
        f"/settings-accounts.html (users wired): console errors={console_errors!r}; "
        f"page errors={page_errors!r}"
    )


@pytest.mark.e2e
def test_offdashboard_bell_reads_api_alerts(
    live_server: str, browser_page: Page
) -> None:
    """Topbar bell on a NON-dashboard page renders from GET /api/alerts (spec 19/03 I1).

    After the Task-2.7c I1 retirement, alerts.js off the dashboard drops the legacy
    localStorage client-compute path and instead fetches GET /api/alerts -> {as_of,
    alerts} through pdApi, mapping each Alert.href via mapAlertHref and rendering the bell
    count + panel. The golden DB holds 2330 at ~94% of the portfolio (single_weight rule,
    default 30% threshold) so the rule engine returns at least one risk alert -> the bell
    count badge (.bell-count, appended only when alerts.length > 0) renders.

    /instruments.html is a non-dashboard page that loads api.js + format.js + alerts.js.
    Navigate it, wait for the bell count to land (proving GET /api/alerts resolved and the
    bell rendered from the backend), then assert ZERO console + ZERO page errors over the
    async fetch — catching an unhandled rejection or a botched render of the Alert wire.
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
        # Deterministically catch the off-dashboard /api/alerts fetch (alerts.js boot).
        with page.expect_response("**/api/alerts") as resp_info:
            page.goto(live_server + "/instruments.html", wait_until="load")
        assert resp_info.value.status == 200, f"/api/alerts status {resp_info.value.status}"
        # 2330 ~94% weight > 30% single_weight default -> >=1 alert -> the count badge lands.
        page.wait_for_selector(".bell-count")
        count_text = page.inner_text(".bell-count")
        assert count_text.strip().isdigit() and int(count_text.strip()) >= 1, (
            f"bell count did not render a positive count: {count_text!r}"
        )
    finally:
        page.remove_listener("console", _on_console)
        page.remove_listener("pageerror", _on_pageerror)

    assert not console_errors and not page_errors, (
        f"/instruments.html (off-dashboard bell): console errors={console_errors!r}; "
        f"page errors={page_errors!r}"
    )


@pytest.mark.e2e
def test_settings_alert_rules_editor_wired(
    live_server: str, browser_page: Page
) -> None:
    """Alert-rules editor on /settings.html renders from GET /api/alert-rules (Task 2.7c).

    After the Task-2.7c wiring, settings-alerts.js drops its localStorage 'pd_alert_rules'
    editor and boots off GET /api/alert-rules -> {rules:[{id,enabled,value,unit,min,max}]},
    rendering one editor row per backend rule (rules_config.py RULE_META has 8 rules,
    including the calib_gap rule). The golden DB seeds the default rules, so the editor
    renders rows with the toggle switches + numeric inputs.

    Navigate /settings.html (which loads api.js + settings-alerts.js), wait for the editor
    rows to mount inside #alert-rules-wrap (proving GET /api/alert-rules resolved), then
    assert ZERO console + ZERO page errors over the async boot.
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
        with page.expect_response("**/api/alert-rules") as resp_info:
            page.goto(live_server + "/settings.html", wait_until="load")
        assert resp_info.value.status == 200, (
            f"/api/alert-rules status {resp_info.value.status}"
        )
        # Editor rows mount only after GET /api/alert-rules resolves. The 預警規則 tab is
        # not the active tab, so rows are ATTACHED (in the DOM) but not visible.
        page.wait_for_selector("#alert-rules-wrap .ar-row", state="attached")
        rows = page.query_selector_all("#alert-rules-wrap .ar-row")
        assert len(rows) >= 8, f"expected >=8 alert-rule rows, got {len(rows)}"
    finally:
        page.remove_listener("console", _on_console)
        page.remove_listener("pageerror", _on_pageerror)

    assert not console_errors and not page_errors, (
        f"/settings.html (alert-rules editor): console errors={console_errors!r}; "
        f"page errors={page_errors!r}"
    )


@pytest.mark.e2e
def test_insights_page_smoke(live_server: str, browser_page: Page) -> None:
    """/insights.html boots from GET /api/insights + GET /api/ai-score (spec 19, Task 2.8).

    The page drops its inline design-preview mock and async-boots two endpoints: the 洞察卡
    grid from GET /api/insights (stored cards; cost_usd is a Decimal STRING rendered via
    window.fmt — never a bare .toFixed) and the AI 戰績 panel from GET /api/ai-score
    (totals / by_combo / calibration_bins / rows, all Decimal STRINGS). The golden DB seeds
    ZERO insight cards (composer + insights tables created EMPTY), so this exercises the
    graceful empty path: the cards grid renders its empty-state affordance.

    insights.html ALSO loads api.js + alerts.js, so the off-dashboard /api/alerts +
    /api/llm/config path now runs here too (Task 2.7c); this asserts that coexists clean.
    Waits for the empty-state node inside #ins-cards-grid (rendered only after GET
    /api/insights resolves to []), then asserts ZERO console + ZERO page errors — catching a
    Decimal-string `.toFixed` TypeError on cost_usd, an undefined field, or a botched async
    boot.
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
        page.goto(live_server + "/insights.html", wait_until="load")
        # Golden DB has ZERO cards -> GET /api/insights returns [] -> the empty-state div
        # renders inside #ins-cards-grid (proves the async boot landed, not the empty shell).
        page.wait_for_selector("#ins-cards-grid .wz-note")
    finally:
        page.remove_listener("console", _on_console)
        page.remove_listener("pageerror", _on_pageerror)

    assert not console_errors and not page_errors, (
        f"/insights.html (insights + ai-score wired): console errors={console_errors!r}; "
        f"page errors={page_errors!r}"
    )


@pytest.mark.e2e
def test_rebalance_drawer_smoke(live_server: str, browser_page: Page) -> None:
    """Rebalance what-if drawer boots off the shared /api/dashboard promise (Task 3.1).

    After Task 3.1, rebalance.js drops `window.DASHBOARD_DATA` (mock-data.js is deleted)
    and sources its holdings from the SAME shared window.pdDashboard promise app.js /
    charts.js / alerts.js / detail.js use (one GET /api/dashboard). The drawer is opened by
    INTERACTION, not a page load, so this navigates /index.html (golden DB), waits for the
    dashboard render + the mounted '再平衡試算' trigger, clicks it, and asserts the drawer
    renders a holdings table sourced from the backend payload.

    The golden DB seeds 2330 (TW, priced 600) and AAPL (US, priced 120) — both priced with
    a non-null weight — so the drawer's priced table renders at least one row. Asserts the
    FULL async wiring (await pdDashboard -> build rows -> what-if estimate via window.pdFeeTax
    + window.fmt on the backend Decimal-STRING weights/prices) runs with ZERO console errors
    + ZERO uncaught page errors — proving rebalance.js boots off /api/dashboard, not the
    deleted mock, and that no bare `.toFixed` on a backend Decimal string slipped in.
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
        page.wait_for_selector(".rb-open-btn")  # rebalance trigger mounted on holdings panel
        page.click(".rb-open-btn")
        page.wait_for_selector(".rb-drawer")  # drawer opened
        # The priced holdings table renders >=1 row sourced from the /api/dashboard payload
        # (golden 2330 + AAPL are priced) — proves holdings came from the backend, not a mock.
        page.wait_for_selector(".rb-drawer .rb-table tbody tr .sym-code")
    finally:
        page.remove_listener("console", _on_console)
        page.remove_listener("pageerror", _on_pageerror)

    assert not console_errors and not page_errors, (
        f"rebalance drawer (/index.html): console errors={console_errors!r}; "
        f"page errors={page_errors!r}"
    )


@pytest.mark.e2e
def test_rebalance_preview_roundtrip(live_server: str, browser_page: Page) -> None:
    """Setting a target weight hits the REAL POST /api/rebalance/preview (spec 19 defer ③).

    After defer ③, rebalance.js drops its client-side what-if math (the FX_TWD mock rates,
    the window.pdFeeTax fee estimate, the client turnover/new-weight) and instead POSTs the
    user's target weight RATIOS (as STRINGS) to /api/rebalance/preview — the AUTHORITATIVE
    backend computation (real fee engine, real FX, integer-share / MY-100-lot snapping). The
    drawer renders the backend rows (side/shares/amount/fee+tax/new_weight) + summary
    (turnover/fees/cash_after), all Decimal STRINGS via window.fmt.

    The golden DB holds 2330 at ~94% of the portfolio. Navigate /index.html, open the drawer,
    wait for the 2330 row + its target input, then DROP 2330's target sharply (94% -> 20%) to
    force a SELL. Assert (a) the debounced POST /api/rebalance/preview fires + returns 200, and
    (b) the result renders — the summary 預估周轉額 shows a backend-computed value (not the
    em-dash null glyph). Zero console + zero page errors over the round-trip (catching a bare
    `.toFixed` on a Decimal string, an unhandled POST rejection, or a botched render).
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
        page.wait_for_selector(".rb-open-btn")  # rebalance trigger mounted
        page.click(".rb-open-btn")
        page.wait_for_selector(".rb-drawer")
        # The 2330 row carries a .rb-input (its target-weight control). Locate it by the row
        # whose .sym-code text is 2330, then fill its input — the input dispatch debounces
        # (~250ms) into the backend POST.
        row_input = page.locator(
            ".rb-drawer .rb-table tbody tr",
            has=page.locator(".sym-code", has_text="2330"),
        ).locator(".rb-input")
        row_input.wait_for(state="attached")
        # Drop 2330 sharply (~94% -> 20%) to force a SELL trade. expect_response catches the
        # debounced POST the fill triggers.
        with page.expect_response("**/api/rebalance/preview") as resp_info:
            row_input.fill("20")
        resp = resp_info.value
        assert resp.request.method == "POST", f"expected a POST, got {resp.request.method}"
        assert resp.status == 200, f"/api/rebalance/preview status {resp.status}"
        # The summary 預估周轉額 renders a backend-computed value (not the em-dash null glyph).
        # The .rb-foot is re-rendered only after the preview resolves; wait for its turnover
        # KV value to carry a non-em-dash string.
        page.wait_for_function(
            "() => {"
            " const kvs = document.querySelectorAll('.rb-foot .rb-kv');"
            " for (const kv of kvs) {"
            "  const k = kv.querySelector('.k');"
            "  if (k && k.textContent && k.textContent.indexOf('\\u5468\\u8f49\\u984d') !== -1) {"
            "   const v = kv.querySelector('.v');"
            "   const t = v && v.textContent ? v.textContent.trim() : '';"
            "   return t && t.indexOf('\\u2014') === -1;"
            "  }"
            " }"
            " return false; }"
        )
    finally:
        page.remove_listener("console", _on_console)
        page.remove_listener("pageerror", _on_pageerror)

    assert not console_errors and not page_errors, (
        f"rebalance preview round-trip (/index.html): console errors={console_errors!r}; "
        f"page errors={page_errors!r}"
    )


@pytest.mark.e2e
def test_pipeline_hub_page_smoke(live_server: str, browser_page: Page) -> None:
    """/pipeline-hub.html boots from GET /api/insight-tasks/status (spec 19, Task 2.8).

    The page drops its window.PIPE mock (pipeline-data.js no longer loaded) and async-boots
    the task list + health bar from GET /api/insight-tasks/status. The golden DB seeds ZERO
    insight types (composer tables created EMPTY), so the status payload returns tasks:[]
    and an AI-off health bar -> the task list renders its empty-state ("尚無洞察任務 …").
    quota_remaining + last_batch.cost_usd are Decimal STRINGS routed through window.fmt.

    The page ALSO loads api.js + alerts.js, so the off-dashboard /api/alerts + /api/llm/config
    path runs here too (Task 2.7c) and must be console-clean. Waits for the empty-state node
    inside #pp-list (rendered only after GET
    /api/insight-tasks/status resolves to tasks:[]), then asserts ZERO console + ZERO page
    errors — catching a botched PIPE retirement, a Decimal-string `.toFixed` on quota/cost,
    or an undefined node-state field.
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
        page.goto(live_server + "/pipeline-hub.html", wait_until="load")
        # Golden DB has ZERO insight types -> tasks:[] -> the empty-state div renders inside
        # #pp-list (proves the async status boot landed, not the empty shell).
        page.wait_for_selector("#pp-list .wz-note")
    finally:
        page.remove_listener("console", _on_console)
        page.remove_listener("pageerror", _on_pageerror)

    assert not console_errors and not page_errors, (
        f"/pipeline-hub.html (status wired): console errors={console_errors!r}; "
        f"page errors={page_errors!r}"
    )


@pytest.mark.e2e
def test_dashboard_trend_chart_mounts(
    live_server: str, browser_page: Page
) -> None:
    """Trend chart still mounts after the dead PD_HISTORY marker code is removed (defer ②).

    charts.js dropped the unreachable E8 large-trade-marker block (window.PD_HISTORY was
    permanently undefined once history-mock.js was deleted in Phase 3) plus its downstream
    `markPoint` render. This guards that initTrend STILL builds the trend chart off the real
    /api/dashboard payload: navigate /index.html (golden DB), wait for the dashboard render,
    then assert #trend-chart mounted an echarts instance (a <canvas> inside the host). The
    existing test_index_page_smoke already covers ZERO console/page errors after the removal.
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
        # initTrend runs after the shared /api/dashboard promise resolves; ECharts mounts a
        # <canvas> inside the #trend-chart host. Its presence proves the trend chart built
        # cleanly without the removed dead block.
        page.wait_for_selector("#trend-chart canvas")
    finally:
        page.remove_listener("console", _on_console)
        page.remove_listener("pageerror", _on_pageerror)

    assert not console_errors and not page_errors, (
        f"/index.html (trend chart mount): console errors={console_errors!r}; "
        f"page errors={page_errors!r}"
    )


@pytest.mark.e2e
def test_favicon_present_no_ico_404(live_server: str, browser_page: Page) -> None:
    """Every page references favicon.svg so the browser never 404s on /favicon.ico (defer ⑥).

    shell.js injects <link rel="icon" type="image/svg+xml" href="favicon.svg"> into the head
    at boot (covering all shell-bearing pages); login.html (no shell.js) declares the same
    <link> directly. This asserts: (a) /index.html (shell) carries a link[rel="icon"] whose
    href ends 'favicon.svg'; (b) /login.html carries the same; and (c) GET /favicon.svg
    returns 200 (the asset exists, so the default /favicon.ico request is never made).
    """
    page = browser_page
    assert isinstance(page, Page)

    # (a) Shell page: shell.js injected the favicon link at boot.
    page.goto(live_server + "/index.html", wait_until="load")
    href = page.get_attribute('link[rel="icon"]', "href")
    assert href is not None and href.endswith("favicon.svg"), (
        f"/index.html missing favicon link[rel=icon] -> favicon.svg (got {href!r})"
    )

    # (b) Login page: the <link> is declared directly in its <head>.
    page.goto(live_server + "/login.html", wait_until="load")
    href = page.get_attribute('link[rel="icon"]', "href")
    assert href is not None and href.endswith("favicon.svg"), (
        f"/login.html missing favicon link[rel=icon] -> favicon.svg (got {href!r})"
    )

    # (c) The asset is actually served (200), so the browser uses it instead of /favicon.ico.
    resp = page.request.get(live_server + "/favicon.svg")
    assert resp.status == 200, f"GET /favicon.svg returned {resp.status}, expected 200"
