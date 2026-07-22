"""E2E: the 換匯中心 balance line + HARD oversell block, driven against the REAL stack (FU-D34).

Owner 需求五: on account/from-ccy selection the FX form shows the pool's current balance (the
sell ceiling); a conversion may NEVER drive the pool negative (no financing/overdraft). Double
protection = live frontend validation (inline error + disabled 確認) + a backend hard 422
(fx_insufficient_balance). This flow exercises BOTH the live guard and a real round-trip.

Scenario: moomoo_my starts with a clean MYR 50,000 pool (no golden flows touch it).
  * over-balance amount (60,000) → inline error visible + 確認 disabled;
  * exact-balance amount (50,000) → error cleared + 確認 enabled → POST /api/cash/fx 201;
  * downstream: the MYR pool drains to 0 and the USD pool receives the buy amount.

ZERO console / page errors throughout.
"""

import json
import urllib.request
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from playwright.sync_api import Page
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.data_ingestion.store import insert_cash_movement
from portfolio_dash.shared.enums import Currency
from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    """Re-enable loopback sockets PER TEST (pytest-socket re-bans before every test); each flow
    spawns a fresh isolated uvicorn (free-port probe + readiness poll need loopback TCP)."""
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _seed_fx(conn: Any) -> None:
    """Golden scenario + a clean, KNOWN moomoo_my MYR pool (funding ccy), so the balance
    ceiling is unambiguous (no golden flow touches moomoo_my)."""
    _seed_golden(conn)
    insert_cash_movement(conn, account_id="moomoo_my", move_date=date(2026, 1, 5),
                         kind="DEPOSIT", ccy=Currency.MYR, amount=Decimal("50000"))
    conn.commit()


def _get_json(base_url: str, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(base_url + path, timeout=5) as r:  # noqa: S310 (loopback)
        data: dict[str, Any] = json.loads(r.read().decode("utf-8"))
        return data


def _cash_balance(base: str, account_id: str, ccy: str) -> str | None:
    for b in _get_json(base, "/api/cash")["balances"]:
        if b["account_id"] == account_id and b["ccy"] == ccy:
            return str(b["amount"])
    return None


def _sink(page: Page) -> tuple[list[str], list[str]]:
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(getattr(m, "text", ""))
            if getattr(m, "type", None) == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    return console_errors, page_errors


@pytest.mark.e2e
def test_fx_center_balance_line_and_hard_oversell_block(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_fx)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    # ===== 換匯中心 tab: pick the account -> the 可用餘額 ceiling appears =====================
    page.goto(base + "/cash.html#fx", wait_until="load")
    page.wait_for_selector("#cfx-account option", state="attached")
    page.select_option("#cfx-account", "moomoo_my")
    # from-ccy defaults to funding (MYR); the balance line shows the pool ceiling once the
    # /api/cash balances have loaded (booted). MYR is 2dp -> "50,000.00".
    page.wait_for_function(
        "() => { const n = document.querySelector('#cfx-balance');"
        " return n && n.textContent.includes('可用餘額') && n.textContent.includes('50,000')"
        " && n.textContent.includes('MYR'); }"
    )

    # ===== (A) over-balance amount -> inline error + 確認 disabled ==========================
    # to-ccy already defaults to the settlement ccy (USD); only the from side has a ceiling.
    page.fill("#cfx-from-amt", "60000")
    page.wait_for_selector("#cfx-amt-err", state="visible")
    assert page.is_disabled("#cfx-confirm"), "確認 must be disabled while amount > 可用餘額"

    # ===== (B) exact-balance amount -> error cleared + 確認 enabled + real round-trip =======
    page.fill("#cfx-from-amt", "50000")   # == the MYR ceiling; must NOT be blocked
    page.fill("#cfx-to-amt", "10000")
    page.wait_for_selector("#cfx-amt-err", state="hidden")
    page.wait_for_function("() => !document.querySelector('#cfx-confirm').disabled")
    with page.expect_response("**/api/cash/fx") as cm:
        page.click("#cfx-confirm")
    assert cm.value.status == 201, f"fx convert status {cm.value.status}"
    page.wait_for_selector(".toast-ok")

    # ===== downstream (server-authoritative): MYR pool drained to 0, USD credited =========
    # The 201 already committed the row; read the pools straight from /api/cash.
    assert _cash_balance(base, "moomoo_my", "MYR") == "0"
    assert _cash_balance(base, "moomoo_my", "USD") == "10000"

    assert not console_errors and not page_errors, (
        f"fx balance guard flow: console={console_errors!r} page={page_errors!r}"
    )
