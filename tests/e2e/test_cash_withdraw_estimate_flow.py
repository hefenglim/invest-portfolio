"""E2E: 出金 balance guard + max-fill + FX estimate + 換匯中心 ledger (FU-D40 / FU-D43).

Driven against the REAL stack (fresh uvicorn + on-disk golden DB + headless chromium):

* FU-D43a — kind=出金 shows 「賬戶現金：{balance} {ccy}」 for the selected account+ccy;
  an over-balance amount shows the inline error and disables 確認; the exact-balance
  withdraw round-trips 201 and drains the pool (server-authoritative readback).
* FU-D43b — clicking the balance FIGURE (both the FX 可用餘額 line and the withdraw
  賬戶現金 line) fills the amount field with the full raw balance.
* FU-D43c — entering/filling the sell amount auto-fills the buy amount from
  GET /api/cash/fx-estimate (server-computed; caption 「以 {date} 匯率 {rate} 試算…」);
  once the buy field is edited manually the auto-fill STOPS (a later sell-amount change
  must not overwrite) and the 重新試算 affordance re-runs it on demand.
* FU-D40 — the fx_conversions ledger renders under the 換匯中心 tab (golden schwab row).
* R6-D — FX-form UX hardening: an #cfx-account switch clears both amount fields; a
  single-currency account (tw_broker, TWD-only) disables the whole FX form and shows
  #cfx-single-ccy; picking 賣出幣別 == 買入幣別 auto-flips the OTHER select so the two
  ccy fields can never read equal.

Scenario: moomoo_my starts with a clean MYR 50,000 pool (no golden flow touches it).
Stored USD/MYR 4.4 → MYR→USD inverse rate 0.227273 (6-dp cap); 50,000 × 0.227273 =
11,363.65 and 40,000 × 0.227273 = 9,090.92 — both SERVER-computed figures the page only
places. ZERO console / page errors throughout.
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


def _seed_cash(conn: Any) -> None:
    """Golden scenario + a clean, KNOWN moomoo_my MYR pool (funding ccy)."""
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
def test_cash_withdraw_guard_maxfill_estimate_and_fx_ledger(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_cash)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    # ===== 換匯中心 first (the estimate/click-fill part needs the still-full MYR pool) ====
    page.goto(base + "/cash.html#fx", wait_until="load")
    page.wait_for_selector("#cfx-account option", state="attached")
    page.select_option("#cfx-account", "moomoo_my")
    page.wait_for_function(
        "() => { const n = document.querySelector('#cfx-balance');"
        " return n && n.textContent.includes('可用餘額') && n.textContent.includes('50,000')"
        " && n.textContent.includes('MYR'); }"
    )

    # ---- FU-D40: the fx ledger renders under the tab (golden schwab 32,000 TWD → USD) ----
    page.wait_for_function(
        "() => { const b = document.querySelector('#cfx-ledger-body');"
        " return b && b.textContent.includes('32,000') && b.textContent.includes('TWD'); }"
    )

    # ---- FU-D43b: clicking the 可用餘額 figure fills the sell amount with the raw value --
    page.click("#cfx-balance .can-fill")
    assert page.input_value("#cfx-from-amt") == "50000"

    # ---- FU-D43c: the buy amount auto-fills with the SERVER estimate + caption ----------
    # MYR→USD has no direct row; the inverse USD/MYR 4.4 gives 0.227273 (6-dp cap):
    # 50,000 × 0.227273 = 11,363.65 — computed by the server, only PLACED by the page.
    page.wait_for_function(
        "() => document.querySelector('#cfx-to-amt').value === '11363.65'")
    page.wait_for_function(
        "() => { const c = document.querySelector('#cfx-estimate');"
        " return c && !c.hidden && c.textContent.includes('匯率')"
        " && c.textContent.includes('試算'); }"
    )

    # ---- manual edit wins: a later sell-amount change must NOT overwrite ----------------
    page.fill("#cfx-to-amt", "11000")
    page.wait_for_selector("#cfx-reestimate", state="visible")
    page.fill("#cfx-from-amt", "40000")
    page.wait_for_timeout(800)  # > the 250ms debounce + request time
    assert page.input_value("#cfx-to-amt") == "11000", "manual buy amount was overwritten"

    # ---- 重新試算 re-runs the estimate on demand (40,000 × 0.227273 = 9,090.92) ---------
    page.click("#cfx-reestimate")
    page.wait_for_function(
        "() => document.querySelector('#cfx-to-amt').value === '9090.92'")

    # ===== 出金入金: the withdraw ceiling + max-fill + hard guard + real round-trip ======
    page.goto(base + "/cash.html#flows", wait_until="load")
    page.wait_for_selector("#cm-account option", state="attached")
    page.select_option("#cm-account", "moomoo_my")
    page.click("#cm-kind-out")
    page.select_option("#cm-ccy", "MYR")
    page.wait_for_function(
        "() => { const n = document.querySelector('#cm-balance');"
        " return n && n.textContent.includes('賬戶現金') && n.textContent.includes('50,000')"
        " && n.textContent.includes('MYR'); }"
    )

    # ---- FU-D43b: clicking the 賬戶現金 figure fills the amount ---------------------------
    page.click("#cm-balance .can-fill")
    assert page.input_value("#cm-amount") == "50000"

    # ---- (A) over-balance amount -> inline error + 確認 disabled -------------------------
    page.fill("#cm-amount", "60000")
    page.wait_for_selector("#cm-amt-err", state="visible")
    assert page.is_disabled("#cm-confirm"), "確認 must be disabled while amount > 賬戶現金"

    # ---- (B) exact-balance amount -> error cleared + 確認 enabled + real round-trip -----
    page.fill("#cm-amount", "50000")   # == the MYR pool; must NOT be blocked
    page.wait_for_selector("#cm-amt-err", state="hidden")
    page.wait_for_function("() => !document.querySelector('#cm-confirm').disabled")
    with page.expect_response("**/api/cash/movements") as cm:
        page.click("#cm-confirm")
    assert cm.value.status == 201, f"withdraw status {cm.value.status}"
    page.wait_for_selector(".toast-ok")

    # ---- downstream (server-authoritative): the MYR pool drained to 0 -------------------
    assert _cash_balance(base, "moomoo_my", "MYR") == "0"

    assert not console_errors and not page_errors, (
        f"cash withdraw/estimate flow: console={console_errors!r} page={page_errors!r}"
    )


@pytest.mark.e2e
def test_cash_fx_form_account_switch_single_ccy_never_equal(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """R6-D: FX-form UX hardening (owner-signed 2026-07-19).

    (a) schwab selected -> fill both amounts -> switch account -> both amount fields
        empty (a stale amount must not carry over and re-validate against the new
        account's pool).
    (b) tw_broker (TWD-only, single currency) -> the whole FX form (both ccy selects,
        both amounts, 確認) is disabled and #cfx-single-ccy shows the TWD reason.
    (c) schwab (TWD/USD) -> setting 賣出幣別=USD auto-flips 買入幣別 off USD (the
        never-equal invariant) to schwab's only alternate, TWD.

    Also regression-guards a real ordering defect found in review: updCeiling() ends
    with an UNCONDITIONAL `confirm.disabled = over`, so ANY updFxBalance() call that
    runs AFTER the single-ccy gate disabled #cfx-confirm (init's own trailing call at
    page load, boot()'s async tail, a tab-switch re-boot, post-commit/delete refreshes)
    would silently RE-ENABLE it while the rest of the form stayed disabled. The forced
    re-boot while tw_broker (single-ccy) stays selected reproduces that leak below, each
    time explicitly settling the page's async boot/balances refresh (network-idle) before
    asserting — a bare post-click assertion would race ahead of the call path that leaked.

    Batch B note: the default first account (accounts load `ORDER BY account_id`) is now the
    merged `moomoo_my` — a DUAL-currency (USD/MYR) account, so the plain initial load is NOT
    single-ccy-gated; the single-ccy gate + its confirm-re-enable leak are exercised through
    tw_broker (TWD-only) instead.
    """
    base = flow_server(_seed_cash)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    # ---- plain initial load, no select_option: accounts load ORDER BY account_id, so the
    # default first option is the merged moomoo_my (USD/MYR dual-currency). A dual-ccy account
    # is NOT single-ccy-gated: the ccy selects are enabled and #cfx-single-ccy is hidden.
    # Settle the page's async boot() before asserting. -----------------------------
    page.goto(base + "/cash.html#fx", wait_until="load")
    page.wait_for_selector("#cfx-account option", state="attached")
    page.wait_for_load_state("networkidle")
    assert page.input_value("#cfx-account") == "moomoo_my", \
        "expected moomoo_my as the default first account (ORDER BY account_id)"
    assert not page.is_disabled("#cfx-from-ccy"), \
        "a dual-ccy account must not trip the single-ccy gate on initial load"
    assert page.is_hidden("#cfx-single-ccy")

    # ---- (a) schwab: fill both amounts, then switch account -> both fields clear ----
    page.select_option("#cfx-account", "schwab")
    page.wait_for_function(
        "() => !document.querySelector('#cfx-from-ccy').disabled")
    page.fill("#cfx-from-amt", "1000")
    page.fill("#cfx-to-amt", "31000")
    page.select_option("#cfx-account", "moomoo_my")
    page.wait_for_function(
        "() => document.querySelector('#cfx-from-amt').value === ''"
        " && document.querySelector('#cfx-to-amt').value === ''")

    # ---- (b) tw_broker: single-currency account -> whole form disabled + message ----
    page.select_option("#cfx-account", "tw_broker")
    page.wait_for_function(
        "() => document.querySelector('#cfx-from-ccy').disabled === true")
    for sel in ("#cfx-from-ccy", "#cfx-to-ccy", "#cfx-from-amt", "#cfx-to-amt", "#cfx-confirm"):
        assert page.is_disabled(sel), f"{sel} must be disabled for a single-ccy account"
    assert page.is_visible("#cfx-single-ccy")
    msg = page.text_content("#cfx-single-ccy") or ""
    assert "TWD" in msg and "單一幣別" in msg and "換匯需帳戶具備兩種以上幣別" in msg, msg

    # ---- ordering-leak regression: force a FRESH boot() while tw_broker stays
    # selected -- a tab click re-dispatches pd-cash-tab, whose 'fx' branch re-fetches
    # balances and re-runs updFxBalance() in its tail (the exact call path the review
    # flagged: updCeiling()'s unconditional confirm.disabled = over would otherwise
    # re-enable 確認 once the fresh balances read "not over ceiling"). Settle it, then
    # confirm the single-ccy gate held. -------------------------------------------
    page.click('.cash-tab[data-tab="fx"]')
    page.wait_for_load_state("networkidle")
    assert page.input_value("#cfx-account") == "tw_broker"
    for sel in ("#cfx-from-ccy", "#cfx-to-ccy", "#cfx-from-amt", "#cfx-to-amt", "#cfx-confirm"):
        assert page.is_disabled(sel), f"{sel} must stay disabled after a re-boot while selected"

    # ---- switching back to a two-currency account fully re-enables + hides the msg --
    page.select_option("#cfx-account", "schwab")
    page.wait_for_function(
        "() => !document.querySelector('#cfx-from-ccy').disabled")
    for sel in ("#cfx-from-ccy", "#cfx-to-ccy", "#cfx-from-amt", "#cfx-to-amt", "#cfx-confirm"):
        assert not page.is_disabled(sel), f"{sel} must re-enable for a two-ccy account"
    assert page.is_hidden("#cfx-single-ccy")

    # ---- (c) schwab: 賣出幣別=USD must never equal 買入幣別 -> auto-flips to TWD -----
    assert page.input_value("#cfx-from-ccy") == "TWD"  # default preferred = funding_ccy
    assert page.input_value("#cfx-to-ccy") == "USD"    # default preferred = settlement_ccy
    page.select_option("#cfx-from-ccy", "USD")
    page.wait_for_function(
        "() => document.querySelector('#cfx-to-ccy').value !== 'USD'")
    assert page.input_value("#cfx-to-ccy") == "TWD"

    assert not console_errors and not page_errors, (
        f"cash fx-form hardening flow: console={console_errors!r} page={page_errors!r}"
    )
