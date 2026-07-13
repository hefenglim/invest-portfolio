"""spec-17 §17.5 — E2E user-flow tests (Playwright, real server + real frontend).

Each flow runs against its OWN isolated uvicorn subprocess (tests/e2e/conftest.py
::flow_server) seeded for the flow, so write/auth flows are order-independent and do not
pollute the shared smoke server. Assertions use expect-polling (wait_for_selector /
wait_for_function / expect_response), never sleeps (spec-17 §17.7.4).

Coverage map (the remaining spec-17 flows are covered elsewhere — see the spec-17 report):
  E1 dashboard render  : KPI text == golden, 00919 缺價 badge, asof + stale chip
  E2 manual buy commit : form -> preview -> confirm -> 201 -> position grows in the API
  E4 oversell warning  : soft warning + confirm gated until ack, then writable (201)
  E6 login loop        : protected mode -> wrong pass stays on login -> correct -> dashboard
  E5 drawer / E7 scheduler / E8 bell -> test_pages_smoke.py
  E3 CSV / E9 AI / E10 export -> contract suite + live walkthrough (see report)
"""

import json
import re
import urllib.request
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from playwright.sync_api import Page
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from tests.conftest import _seed_dual_account, _seed_golden
from tests.contract.test_oversell_graceful import _seed_oversold
from tests.contract.test_spec17_financials import seed_full
from tests.e2e.conftest import FlowServerFactory


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    """Re-enable loopback sockets PER TEST. pytest-socket's --disable-socket re-bans
    sockets before every test; the session-scoped _e2e_loopback_socket only lifts the
    ban once. These flows create fresh Python sockets per test (flow_server's free-port
    probe + readiness poll), so each needs the loopback exception re-applied here."""
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _get_json(base_url: str, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(base_url + path, timeout=5) as r:  # noqa: S310 (loopback)
        data: dict[str, Any] = json.loads(r.read().decode("utf-8"))
        return data


def _shares_of(body: dict[str, Any], symbol: str) -> str | None:
    for h in body["holdings"]:
        if h["symbol"] == symbol:
            shares: str = h["shares"]
            return shares
    return None


def _sink(page: Page) -> tuple[list[str], list[str]]:
    """Attach console-error + pageerror sinks; return the two lists."""
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(getattr(m, "text", ""))
            if getattr(m, "type", None) == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    return console_errors, page_errors


@pytest.mark.e2e
def test_e1_dashboard_kpis_and_missing_price_badge(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """E1: the dashboard renders the golden KPIs + degradation badges from the real API."""
    base = flow_server(seed_full)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    page.goto(base + "/index.html", wait_until="load")
    page.wait_for_selector(".kpi-card")
    # asof chip carries the frozen report date.
    page.wait_for_function(
        "() => { const e = document.querySelector('#asof-value');"
        " return e && e.textContent && e.textContent.includes('2026'); }"
    )
    # any_stale (00919 missing + MSFT stale) -> header shows the '部分過期' chip.
    assert "部分過期" in page.inner_text("#fresh-chip")
    # 總市值 hero value == golden total_market_value (2,937,965), separator/decimal-agnostic.
    hero = page.locator(".kpi-hero .kpi-value").first.inner_text()
    m = re.search(r"[\d,]+", hero)
    assert m is not None and m.group(0).replace(",", "") == "2937965", hero
    # 00919 has no price -> a 缺價 badge appears in the holdings table.
    assert page.get_by_text("缺價").count() > 0

    assert not console_errors and not page_errors, (
        f"E1: console={console_errors!r} page={page_errors!r}"
    )


@pytest.mark.e2e
def test_e2_manual_buy_commit_grows_position(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """E2: a manual BUY through the form commits (201) and the position grows in the API.

    Subset golden holds 2330 = 1000 sh; buying 1000 @ 612.5 -> 2000 sh.
    """
    base = flow_server(_seed_golden)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    before = _shares_of(_get_json(base, "/api/dashboard"), "2330")
    assert before is not None and Decimal(before) == Decimal("1000")

    page.goto(base + "/input.html", wait_until="load")
    page.wait_for_selector("#m-account option", state="attached")
    page.select_option("#m-account", "tw_broker")
    page.fill("#m-symbol", "2330")
    page.fill("#m-shares", "1000")
    with page.expect_response("**/api/input/manual/preview") as pv:
        page.fill("#m-price", "612.5")
    assert pv.value.status == 200
    # confirm enables only once the server preview lands with no hard issues.
    page.wait_for_function(
        "() => { const b = document.querySelector('#m-confirm'); return b && !b.disabled; }"
    )
    with page.expect_response("**/api/input/manual/commit") as cm:
        page.click("#m-confirm")
    assert cm.value.status == 201

    after = _shares_of(_get_json(base, "/api/dashboard"), "2330")
    assert after is not None and Decimal(after) == Decimal("2000")

    assert not console_errors and not page_errors, (
        f"E2: console={console_errors!r} page={page_errors!r}"
    )


@pytest.mark.e2e
def test_e4_oversell_soft_warning_gates_confirm_until_ack(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """E4: selling more than held raises a soft warning + ack checkbox; the confirm button
    stays disabled until the ack is checked, then the write is allowed (201)."""
    base = flow_server(_seed_golden)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    page.goto(base + "/input.html", wait_until="load")
    page.wait_for_selector("#m-account option", state="attached")
    page.select_option("#m-account", "tw_broker")
    page.click("#m-side-sell")
    page.fill("#m-symbol", "2330")
    page.fill("#m-shares", "1500")  # > 1000 held -> oversell
    with page.expect_response("**/api/input/manual/preview") as pv:
        page.fill("#m-price", "600")
    assert pv.value.status == 200
    # The oversell soft issue renders an ack checkbox; confirm is gated until it is ticked.
    page.wait_for_selector("#m-ack")
    assert page.locator("#m-confirm").is_disabled()
    page.check("#m-ack")
    page.wait_for_function(
        "() => { const b = document.querySelector('#m-confirm'); return b && !b.disabled; }"
    )
    with page.expect_response("**/api/input/manual/commit") as cm:
        page.click("#m-confirm")
    assert cm.value.status == 201  # spec E4: "ack 後可寫"

    assert not console_errors and not page_errors, (
        f"E4: console={console_errors!r} page={page_errors!r}"
    )


@pytest.mark.e2e
def test_oversell_position_renders_badge_no_crash(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """An oversold (賣超) ledger renders the dashboard with a 賣超 badge and ZERO console/
    page errors — the lightweight degradation (decided 2026-06-18) holds end-to-end: the
    negative-share / null-value row does not throw in the browser."""
    base = flow_server(_seed_oversold)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    page.goto(base + "/index.html", wait_until="load")
    page.wait_for_selector(".kpi-card")
    page.wait_for_selector("#holdings-body tr")
    assert page.get_by_text("賣超").count() > 0

    assert not console_errors and not page_errors, (
        f"oversell display: console={console_errors!r} page={page_errors!r}"
    )


@pytest.mark.e2e
def test_e6_login_loop_protected_mode(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """E6: in protected mode a wrong password 401s and stays on /login.html; the correct
    password lands on the dashboard. (A 401 emits an expected console error, so the
    zero-console-error assertion is intentionally omitted for this flow.)"""
    base = flow_server(_seed_golden, users=[("kevin", "pw-123456")])
    page = fresh_page

    page.goto(base + "/login.html", wait_until="load")
    page.fill("#login-user", "kevin")
    page.fill("#login-pass", "wrong-pass")
    with page.expect_response("**/api/auth/login") as bad:
        page.click("#login-btn")
    assert bad.value.status == 401
    page.wait_for_selector("#login-error:not([hidden])")
    assert page.url.endswith("login.html"), f"unexpected redirect to {page.url}"

    page.fill("#login-pass", "pw-123456")
    with page.expect_response("**/api/auth/login") as ok:
        page.click("#login-btn")
    assert ok.value.status == 200
    page.wait_for_url("**/index.html")
    page.wait_for_selector(".kpi-card")  # the session cookie carried -> dashboard renders


@pytest.mark.e2e
def test_rebalance_dual_account_single_row_with_chips(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """Combined cross-account rebalance: a symbol held in TWO accounts (AAPL: schwab +
    moomoo_my_us) renders as EXACTLY ONE drawer row with account chips; editing its target
    fires the preview and populates its OWN action cell (the pre-fix orphan bug is gone) —
    and one row means the footer counts the symbol once. ZERO console + page errors."""
    base = flow_server(_seed_dual_account)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    page.goto(base + "/index.html", wait_until="load")
    page.wait_for_selector(".kpi-card")
    page.wait_for_selector(".rb-open-btn")
    page.click(".rb-open-btn")
    page.wait_for_selector(".rb-drawer .rb-table tbody tr")

    aapl_rows = page.locator(
        ".rb-drawer .rb-table tbody tr",
        has=page.locator(".sym-code", has_text="AAPL"),
    )
    assert aapl_rows.count() == 1  # ONE row for the dual-account symbol (no duplicate)
    # the account chips list BOTH constituents (schwab 30 + moomoo_my_us 10)
    assert aapl_rows.locator(".rb-acct-chip").count() == 2

    # editing AAPL's target fires the debounced preview; its OWN action cell then computes
    inp = aapl_rows.locator(".rb-input")
    inp.wait_for(state="attached")
    with page.expect_response("**/api/rebalance/preview") as resp_info:
        inp.fill("40")
    assert resp_info.value.status == 200
    aapl_rows.locator(".rb-leg").first.wait_for(state="attached")  # its cells populated

    assert not console_errors and not page_errors, (
        f"dual-account rebalance: console={console_errors!r} page={page_errors!r}"
    )


@pytest.mark.e2e
def test_rebalance_export_report_download(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """匯出執行報告: the drawer's export button triggers a browser download of the current
    plan as `rebalance-plan-YYYYMMDD-HHMM.html`. ZERO console + page errors."""
    base = flow_server(_seed_dual_account)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    page.goto(base + "/index.html", wait_until="load")
    page.wait_for_selector(".kpi-card")
    page.wait_for_selector(".rb-open-btn")
    page.click(".rb-open-btn")
    page.wait_for_selector(".rb-drawer .rb-table tbody tr")
    page.wait_for_selector(".rb-export-btn")

    with page.expect_download() as dl_info:
        page.click(".rb-export-btn")
    download = dl_info.value
    assert re.match(r"rebalance-plan-\d{8}-\d{4}\.html$", download.suggested_filename), (
        f"unexpected export filename: {download.suggested_filename!r}"
    )

    assert not console_errors and not page_errors, (
        f"rebalance export: console={console_errors!r} page={page_errors!r}"
    )


@pytest.mark.e2e
def test_holdings_export_report_download(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """匯出報告 (持倉報告): the holdings panel's report button triggers a browser download of
    `holdings-report-YYYYMMDD-HHMM.html`. ZERO console + page errors."""
    base = flow_server(_seed_dual_account)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    page.goto(base + "/index.html", wait_until="load")
    page.wait_for_selector(".kpi-card")
    page.wait_for_selector(".pd-holdings-report-btn")

    with page.expect_download() as dl_info:
        page.click(".pd-holdings-report-btn")
    download = dl_info.value
    assert re.match(r"holdings-report-\d{8}-\d{4}\.html$", download.suggested_filename), (
        f"unexpected export filename: {download.suggested_filename!r}"
    )

    assert not console_errors and not page_errors, (
        f"holdings export: console={console_errors!r} page={page_errors!r}"
    )


@pytest.mark.e2e
def test_target_weights_section_renders_sum_and_saves(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """D8: the 目標配置 section renders per-symbol rows via the real settings nav, the live
    sum indicator reacts to input, and 儲存 PUTs the ratios (guest write path, like the
    existing alert-rules editor). ZERO console + page errors on the whole flow."""
    base = flow_server(seed_full)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    page.goto(base + "/settings.html#alerts", wait_until="load")
    # one row per REGISTERED symbol (rendered after GET /api/target-weights resolves)
    page.wait_for_selector("#target-weights-wrap .tw-row")
    # the live sum indicator reacts to a % input
    page.locator("#target-weights-wrap .tw-input").first.fill("12.5")
    page.wait_for_function(
        "() => (document.querySelector('#tw-sum').textContent || '').includes('12.5')"
    )
    # 儲存 PUTs the ratio and the backend accepts it (200) — the alerts editor's guest path
    with page.expect_response("**/api/target-weights") as resp:
        page.click("#target-weights-save")
    assert resp.value.status == 200

    assert not console_errors and not page_errors, (
        f"target-weights: console={console_errors!r} page={page_errors!r}"
    )
