"""E2E — session idle expiry (owner ruling 2026-10-22, review finding L5).

Real server, real browser, protected mode. The owner logs in, then the session goes 31 days
without use (its recorded `last_seen_at` is moved back in the database file — the server runs
on the real clock). Opening the dashboard again must land on the existing login page with
the reason in plain words, and the API itself must refuse the cookie with the same sentence.
Then the row disappears altogether, as the boot purge removes it: the login page still says
why the form is back. Logging in again lands on the dashboard.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import Page, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory

_USER, _PASS = "owner", "pw-123456"
_EXPIRED = "登入已逾期（30 天未使用），請重新登入"
_INVALID = "先前的登入已失效，請重新登入"


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _db_write(path: str, sql: str, *params: object) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _log_in(page: Page, base: str) -> None:
    page.goto(base + "/login.html", wait_until="load")
    page.fill("#login-user", _USER)
    page.fill("#login-pass", _PASS)
    with page.expect_response("**/api/auth/login") as ok:
        page.click("#login-btn")
    assert ok.value.status == 200
    page.wait_for_url("**/index.html")
    page.wait_for_selector(".kpi-card")


@pytest.mark.e2e
def test_an_expired_session_lands_on_the_login_page_with_the_reason(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    db_paths: list[str] = []

    def seed(conn: sqlite3.Connection) -> None:
        _seed_golden(conn)
        db_paths.append(str(conn.execute("PRAGMA database_list").fetchone()[2]))

    base = flow_server(seed, users=[(_USER, _PASS)])
    (db_path,) = db_paths
    page = fresh_page

    page.goto(base + "/login.html", wait_until="load")
    expect(page.locator("#login-notice")).to_be_hidden()  # a first visit carries no notice
    _log_in(page, base)

    # 31 days without use, on the server's own clock (Asia/Taipei, the app's day anchor)
    idle = datetime.now(ZoneInfo("Asia/Taipei")) - timedelta(days=31)
    _db_write(db_path, "UPDATE auth_sessions SET last_seen_at = ?", idle.isoformat())

    r = page.request.get(base + "/api/accounts")  # the page's own cookie
    assert r.status == 401
    assert r.json()["error"]["message"] == _EXPIRED

    page.goto(base + "/index.html", wait_until="load")
    page.wait_for_url("**/login.html")
    notice = page.locator("#login-notice")
    expect(notice).to_be_visible()
    expect(notice).to_have_text(_EXPIRED)
    expect(page.locator("#login-hint")).to_be_hidden()  # the guest-mode hint stays out of it

    # The boot purge removes the row; the cookie is still in the browser.
    _db_write(db_path, "DELETE FROM auth_sessions")
    page.reload(wait_until="load")
    expect(page.locator("#login-notice")).to_have_text(_INVALID)

    # Logging in again issues a fresh session and lands on the dashboard.
    page.fill("#login-user", _USER)
    page.fill("#login-pass", _PASS)
    with page.expect_response("**/api/auth/login") as ok:
        page.click("#login-btn")
    assert ok.value.status == 200
    page.wait_for_url("**/index.html")
    page.wait_for_selector(".kpi-card")
