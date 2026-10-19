"""PERF spec 2026-10-18 §13 (plan §6.3, package F): the frontend half of the transport work, in
a real browser — every page boots with the settlement chip and no console error, the boot
chains ask in parallel, and the duplicate reads are gone.

The settlement API is stubbed (``fresh_page`` only) exactly as in
``tests/e2e/settlement_stubs.py`` (E's real shapes); everything else is the session
``live_server``.

"In parallel" is measured, not inferred: the browser's own Resource Timing says when each
request LEFT and when each answer ARRIVED, so "B was asked before A's answer came back" is a
fact about the page, independent of how fast this machine happens to answer.
"""

from __future__ import annotations

from typing import Any

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.settlement_stubs import SettleStub, status, stubbed

_PAGES = [
    ("/index.html", ".kpi-card"),
    ("/trades.html", "#topbar"),
    ("/cash.html", "#topbar"),
    ("/dividend-inbox.html", "#topbar"),
    ("/instruments.html", "#topbar"),
    ("/insights.html", "#topbar"),
    ("/pipeline-hub.html", "#topbar"),
    ("/news.html", "#topbar"),
    ("/data-center.html", "#topbar"),
    ("/settings.html", "#topbar"),
]


def _timings(page: Page) -> list[dict[str, Any]]:
    """Resource Timing for every /api request: path, query, start and end (ms)."""
    out: list[dict[str, Any]] = page.evaluate(
        """() => performance.getEntriesByType('resource')
             .filter((e) => e.name.indexOf('/api/') >= 0)
             .map((e) => { const u = new URL(e.name);
               return { path: u.pathname, query: u.search, start: e.startTime,
                        end: e.responseEnd }; })""")
    return out


def _one(rows: list[dict[str, Any]], path: str) -> dict[str, Any]:
    hits = [r for r in rows if r["path"] == path]
    assert hits, f"{path} was never requested: {[r['path'] for r in rows]}"
    return hits[0]


@pytest.mark.e2e
def test_every_shell_page_boots_with_the_chip_and_no_console_error(
    live_server: str, fresh_page: Page
) -> None:
    errors: list[str] = []
    fresh_page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    fresh_page.on("pageerror", lambda e: errors.append(str(e)))
    stub = SettleStub(status("clean"))
    with stubbed(fresh_page, stub):
        fresh_page.set_viewport_size({"width": 1440, "height": 900})
        for path, root in _PAGES:
            fresh_page.goto(live_server + path, wait_until="load")
            fresh_page.wait_for_selector(root)
            expect(fresh_page.locator("#settle-chip")).to_be_visible()
            expect(fresh_page.locator("#settle-chip")).to_contain_text("已結算")
            fresh_page.wait_for_load_state("networkidle")
            assert not errors, f"{path}: {errors}"
        # the self-stamp: settlement.js carries shell.js's own ?v=
        src = fresh_page.evaluate(
            "() => Array.from(document.scripts).map((s) => s.src)"
            ".find((s) => s.indexOf('settlement.js') >= 0)")
        assert src and "settlement.js?v=" in src, src


@pytest.mark.e2e
def test_the_boot_chains_ask_in_parallel(live_server: str, fresh_page: Page) -> None:
    stub = SettleStub(status("clean"))
    with stubbed(fresh_page, stub):
        fresh_page.set_viewport_size({"width": 1440, "height": 900})
        # 資金管理: the pools and the 換匯中心 ledger leave before the context has answered
        fresh_page.goto(live_server + "/cash.html", wait_until="networkidle")
        t = _timings(fresh_page)
        ctx = _one(t, "/api/input/context")
        assert _one(t, "/api/cash")["start"] < ctx["end"]
        assert _one(t, "/api/ledgers/fx")["start"] < ctx["end"]
        # 儀表板: 各帳戶現金 and 月度成績 no longer wait for the dashboard payload
        fresh_page.goto(live_server + "/index.html", wait_until="networkidle")
        t = _timings(fresh_page)
        dash = _one(t, "/api/dashboard")
        assert _one(t, "/api/cash")["start"] < dash["end"]
        assert _one(t, "/api/snapshots")["start"] < dash["end"]
        # 系統設定: the scheduler's runs / status and the prompts page's reads leave together
        fresh_page.goto(live_server + "/settings.html", wait_until="networkidle")
        t = _timings(fresh_page)
        jobs = _one(t, "/api/scheduler/jobs")
        assert _one(t, "/api/scheduler/runs")["start"] < jobs["end"]
        assert _one(t, "/api/scheduler/status")["start"] < jobs["end"]
        pvars = _one(t, "/api/prompt-vars")
        for path in ("/api/system-prompt", "/api/strategy-prompts", "/api/prompt-templates",
                     "/api/evolution-config"):
            assert _one(t, path)["start"] < pvars["end"], path


@pytest.mark.e2e
def test_the_duplicate_reads_are_gone(live_server: str, fresh_page: Page) -> None:
    stub = SettleStub(status("clean"))
    with stubbed(fresh_page, stub):
        fresh_page.set_viewport_size({"width": 1440, "height": 900})
        count = lambda rows, p: sum(1 for r in rows if r["path"] == p)  # noqa: E731
        fresh_page.goto(live_server + "/trades.html", wait_until="networkidle")
        t = _timings(fresh_page)
        assert count(t, "/api/input/context") == 1, t
        assert count(t, "/api/llm/config") == 1, t
        fresh_page.goto(live_server + "/settings.html", wait_until="networkidle")
        assert count(_timings(fresh_page), "/api/ui-prefs") == 1
        # 收件匣: the badge reads the three lists the page already loaded — no /count at all
        fresh_page.goto(live_server + "/dividend-inbox.html", wait_until="networkidle")
        t = _timings(fresh_page)
        assert [r["path"] for r in t if r["path"].endswith("/count")] == []
        # … and still says what the /count routes would have said
        listed = fresh_page.evaluate("""async () => {
            const [d, r, e] = await Promise.all([
              window.pdApi.get('/api/dividend-inbox/count'),
              window.pdApi.get('/api/rebates/count'),
              window.pdApi.get('/api/option-expiry/count')]);
            return d.count + r.count + e.count; }""")
        badge = fresh_page.locator("#sidebar .sb-badge-alert")
        if listed:
            expect(badge).to_have_text(str(listed))
        else:
            assert badge.count() == 0
        # elsewhere the badge still asks the three counts
        fresh_page.goto(live_server + "/instruments.html", wait_until="networkidle")
        assert len([r for r in _timings(fresh_page) if r["path"].endswith("/count")]) == 3
