"""E2E (real browser) — DEF-053: the 新增洞察任務 wizard's toast says 「預警觸發」, not `on_alert`.

The verifier's path: 洞察管線 › 新增洞察任務 → 觸發選「預警觸發」→ 建立 → the toast's sub-line
read 「<名稱>：on_alert 任務預設停用，確認監聽規則後再啟用」. This drives that path click by click
and reads the toast the owner sees, then the whole visible page (the dry-run panel the wizard
opens right after) for any scope identifier.
"""

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import Page
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.llm_insight import composer_store as cs
from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory

NOW = datetime(2026, 9, 25, 10, 0, tzinfo=ZoneInfo("Asia/Taipei"))
_IDENTS = ("on_alert", "per_symbol", "per_market", "all_registered")


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _seed(conn: sqlite3.Connection) -> None:
    _seed_golden(conn)
    cs.create_strategy(conn, name="預警解讀", body="解讀 {{kpis_json}}", now=NOW)


@pytest.mark.e2e
def test_creating_an_alert_task_toasts_in_words(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed)
    page = fresh_page
    page.goto(base + "/pipeline-hub.html", wait_until="networkidle")
    page.locator("#pp-add").click()
    page.locator(".wz-opt", has_text="預警觸發").click()
    page.get_by_role("button", name="下一步 →").click()
    page.get_by_role("button", name="下一步 →").click()
    page.locator(".wz-tpl-row", has_text="預警解讀").locator("input[type=checkbox]").check()
    page.get_by_role("button", name="下一步 →").click()
    page.locator(".pv-field input.input").fill("風險解讀")
    page.get_by_role("button", name="乾跑預檢並建立").click()
    page.wait_for_function(
        "() => Array.from(document.querySelectorAll('.toast'))"
        ".some((t) => (t.innerText || '').indexOf('已建立洞察任務') !== -1)")
    toast = page.evaluate(
        "() => Array.from(document.querySelectorAll('.toast')).map((t) => t.innerText)"
        ".find((t) => t.indexOf('已建立洞察任務') !== -1)")
    assert "風險解讀：預警觸發任務預設停用，確認監聽規則後再啟用" in toast, toast
    assert "on_alert" not in toast, toast
    # the dry-run panel the wizard opens next, and the refreshed task list behind it
    page.wait_for_timeout(800)
    visible = page.evaluate("() => document.body.innerText")
    leaked = [w for w in _IDENTS if w in visible]
    assert not leaked, f"scope identifiers visible on the page: {leaked}"
