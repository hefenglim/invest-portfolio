"""E2E flow (Playwright, real server + real frontend) — FU-D36 run-now status feedback.

Drives the REAL stack (uvicorn subprocess + on-disk SQLite + StaticFiles web/) against a
guest DB seeded with golden holdings. Verifies the 排程中心 needs-七 loop end-to-end:
  * every jobs-table row renders a 狀態 chip (idle rows show — via .run-status),
  * clicking 立即執行 on a NETWORK-FREE job (``snapshot_monthly`` — writes the current-month
    KPI snapshot from the DB, no provider calls) advances that row's chip to a terminal
    成功 WITHOUT a page reload (the poll → 執行中 → 成功 loop), and the run button re-enables,
  * ZERO console errors + ZERO uncaught page errors throughout.

snapshot_monthly is chosen because run-now executes on the flow server via a daemon thread
even with the scheduler disabled (PD_DISABLE_SCHEDULER=1), and its runner reads only the DB
— so the outcome is deterministic and needs no network (which the digest/news flows show the
subprocess technically has, but we avoid for determinism).
"""

import sqlite3
from collections.abc import Iterator

import pytest
from playwright.sync_api import ConsoleMessage, Page
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    """Re-enable loopback sockets PER TEST (flow_server's port probe + readiness poll)."""
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _seed_scheduler(conn: sqlite3.Connection) -> None:
    """Golden holdings so the snapshot job has a portfolio to snapshot. Scheduler job rows
    are seeded by the app's own lifespan on boot (ensure_scheduler_seeded)."""
    _seed_golden(conn)


_ROW_STATUS_JS = """
(want) => {
    for (const tr of document.querySelectorAll('#jobs-body tr')) {
        const code = tr.querySelector('.cron-code');
        if (code && code.textContent.trim() === 'snapshot_monthly') {
            const s = tr.querySelector('.run-status');
            const txt = (s && s.textContent) || '';
            return want === 'terminal' ? /成功|失敗/.test(txt) : txt.indexOf(want) !== -1;
        }
    }
    return false;
}
"""


def test_run_now_status_advances_to_success_inplace(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base_url = flow_server(_seed_scheduler)  # guest mode (no users)
    page = fresh_page

    console_errors: list[str] = []
    page_errors: list[str] = []

    def _on_console(msg: ConsoleMessage) -> None:
        if getattr(msg, "type", None) == "error":
            console_errors.append(getattr(msg, "text", repr(msg)))

    def _on_pageerror(exc: object) -> None:
        page_errors.append(str(exc))

    page.on("console", _on_console)
    page.on("pageerror", _on_pageerror)

    page.goto(base_url + "/settings.html#scheduler", wait_until="load")
    page.wait_for_selector("#jobs-body tr")

    # The snapshot_monthly row renders a status slot; idle (never run) → em-dash chip.
    row = page.locator("#jobs-body tr").filter(has_text="snapshot_monthly")
    row.wait_for(state="attached")
    assert row.locator(".run-status").count() == 1, "row missing its 狀態 slot"

    # Trigger the run; the row must advance to a terminal state (成功) without a reload.
    row.get_by_role("button", name="立即執行").click()
    page.wait_for_function(_ROW_STATUS_JS, arg="terminal", timeout=30_000)

    final = row.locator(".run-status").inner_text()
    assert "成功" in final, f"snapshot_monthly run-now expected 成功, got {final!r}"

    # Polling stops when idle → the run button is re-enabled (never left stuck disabled).
    page.wait_for_function(
        """() => {
            for (const tr of document.querySelectorAll('#jobs-body tr')) {
                const code = tr.querySelector('.cron-code');
                if (code && code.textContent.trim() === 'snapshot_monthly') {
                    const b = tr.querySelector('button.btn');
                    return !!b && !b.disabled;
                }
            }
            return false;
        }""",
        timeout=30_000,
    )

    assert not console_errors and not page_errors, (
        f"scheduler run-now status: console errors={console_errors!r}; "
        f"page errors={page_errors!r}"
    )
