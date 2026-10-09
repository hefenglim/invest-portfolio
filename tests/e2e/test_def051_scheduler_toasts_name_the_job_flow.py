"""E2E (real browser) — DEF-051: a scheduler toast names the job the way its row does.

Measured on the demo (R2 observation, R3 reproduction): 系統設定 › 排程中心 → the 市場週報
row's 「立即執行」 → toast 「✓ 已排入執行 insight:7 #180」; the alert_scan row →
「✓ 已排入執行 alert_scan #185」 — while the same rows read 「AI 洞察任務「市場週報」」 /
「風險警示掃描＋AI 派發」.
Root cause ``web/settings-scheduler.js:548`` ``_toast('已排入執行', 'ok', j.id + ' #' + …)``;
the class scan found the same ``j.id`` / ``jobId`` in the enable-toggle and cron toasts of that
page and in all three toasts of the 每日摘要 card (``web/settings-digest.js``), where a bare
``digest_daily`` sub-line was even swallowed by shell.js's DIAG_CODE filter, leaving 「已啟用」
with no subject at all.

Every write is intercepted (``page.route``) so the test is deterministic — the run numbers are
the verifier's own #180 / #185 — and starts no real job. The assertion is on the toast the
owner reads, never on the source text.
"""

import json
import sqlite3
from collections.abc import Iterator
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import Page, Route
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.scheduler.jobs import bind_insight_schedule, create_scheduler_tables
from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory

NOW = datetime(2026, 9, 25, 10, 0, tzinfo=ZoneInfo("Asia/Taipei"))


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _seed(conn: sqlite3.Connection) -> None:
    _seed_golden(conn)
    create_scheduler_tables(conn)
    s = cs.create_strategy(conn, name="週報策略", body="{{kpis_json}}", now=NOW)
    it = cs.create_insight_type(conn, name="市場週報", scope="portfolio", now=NOW)
    cs.set_strategies(conn, it.id, [(s.id, 0)])
    cs.set_job_id(conn, it.id, bind_insight_schedule(conn, it.id, cron="0 8 * * 1"))


_TOASTS = "() => Array.from(document.querySelectorAll('.toast')).map((t) => t.innerText)"


def _toast_with(page: Page, head: str) -> str:
    page.wait_for_function(
        "(h) => Array.from(document.querySelectorAll('.toast'))"
        ".some((t) => (t.innerText || '').indexOf(h) !== -1)", arg=head)
    texts: list[str] = page.evaluate(_TOASTS)
    return next(t for t in reversed(texts) if head in t)


@pytest.mark.e2e
def test_every_scheduler_and_digest_toast_names_the_job_not_its_id(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed)
    page = fresh_page

    def fake_writes(route: Route) -> None:
        req = route.request
        url = req.url
        if req.method == "POST" and url.endswith("/run") and "/api/scheduler/jobs/" in url:
            run_id = 185 if "alert_scan" in url else 180
            route.fulfill(status=202, content_type="application/json",
                          body=json.dumps({"run_id": run_id}))
        elif req.method == "PUT" and "/api/scheduler/jobs/" in url:
            route.fulfill(status=200, content_type="application/json", body="{}")
        elif req.method == "POST" and url.endswith("/api/digest/run"):
            route.fulfill(status=202, content_type="application/json",
                          body=json.dumps({"run_id": 12}))
        else:
            route.continue_()

    page.route("**/api/**", fake_writes)
    page.goto(base + "/settings.html#scheduler", wait_until="networkidle")
    weekly = page.locator("#jobs-body tr", has_text="AI 洞察任務「市場週報」")
    weekly.wait_for()
    alert = page.locator("#jobs-body tr", has_text="風險警示掃描＋AI 派發")

    # --- 立即執行: the verifier's two rows -------------------------------------------------
    weekly.get_by_role("button", name="立即執行").click()
    t = _toast_with(page, "已排入執行")
    assert "AI 洞察任務「市場週報」（#180）" in t, t
    assert "insight:" not in t, t
    page.evaluate("() => document.querySelectorAll('.toast').forEach((t) => t.remove())")
    alert.get_by_role("button", name="立即執行").click()
    t = _toast_with(page, "已排入執行")
    assert "風險警示掃描＋AI 派發（#185）" in t, t
    assert "alert_scan" not in t, t

    # --- the same row's enable toggle and cron editor ------------------------------------
    page.evaluate("() => document.querySelectorAll('.toast').forEach((t) => t.remove())")
    weekly.locator("button.toggle").click()
    t = _toast_with(page, "已更新")
    assert "AI 洞察任務「市場週報」" in t and "insight:" not in t, t
    page.evaluate("() => document.querySelectorAll('.toast').forEach((t) => t.remove())")
    page.wait_for_selector("#jobs-body tr")
    weekly = page.locator("#jobs-body tr", has_text="AI 洞察任務「市場週報」")
    cron = weekly.locator("td.col-text input.input")
    cron.fill("0 9 * * 1")
    cron.dispatch_event("change")
    t = _toast_with(page, "排程已更新")
    assert "AI 洞察任務「市場週報」 · 0 9 * * 1" in t and "insight:" not in t, t

    # --- 每日摘要 card (same class, settings-digest.js) ----------------------------------
    page.evaluate("() => document.querySelectorAll('.toast').forEach((t) => t.remove())")
    daily = page.locator(".digest-cfg-row", has_text="每日收盤摘要")
    daily.get_by_role("button", name="立即產生").click()
    t = _toast_with(page, "已開始產生摘要")
    assert "每日收盤摘要（#12）" in t and "digest_" not in t, t
    page.evaluate("() => document.querySelectorAll('.toast').forEach((t) => t.remove())")
    daily.locator("button.toggle").click()
    t = _toast_with(page, "已")
    assert "每日收盤摘要" in t and "digest_" not in t, t
