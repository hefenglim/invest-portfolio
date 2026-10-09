"""E2E M5-08 (P1) / M5-04: a stale FX estimate must never impersonate a fresh one.

The 換匯 form auto-fills the BUY amount from ``GET /api/cash/fx-estimate`` while the field is
pristine (FU-D43c). The estimate is a hint the user may overwrite — that part is deliberate,
because a conversion is booked at the price actually dealt. What was NOT deliberate is what
happened to the previous estimate when the SELL amount changed:

* clearing the sell amount cleared the caption and left the buy field holding the figure the
  deleted amount had produced;
* a new sell amount left that same figure sitting there for the whole request round trip
  (measured at 1.91 s against the live site) with nothing saying it was out of date.

Both windows end at a 確認 click that writes a conversion whose implied rate is a number no
provider quoted — the sell amount from one entry and the buy amount from another.

So the invariant asserted here is: **while the buy field is pristine, its content either
matches the sell amount currently in the form or is empty.** There is no third state in which
an auto-filled figure outlives the input it was computed from.

Every assertion below reads the DOM inside ONE synchronous JS turn that also fires the input
event, so the 250 ms debounce provably cannot have run yet — the test measures what the page
does at the instant of the change, not what it settles on, which is exactly where the defect
lived. The corresponding fixes have to be synchronous for the same reason.

Scenario mirrors ``test_cash_withdraw_estimate_flow`` so the FIGURES are already evidence:
moomoo_my MYR 50,000, stored USD/MYR 4.4 -> inverse 0.227273; 50,000 -> 11,363.65 and
40,000 -> 9,090.92, both SERVER-computed.
"""

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

_FIRST = "11363.65"   # 50,000 MYR
_SECOND = "9090.92"   # 40,000 MYR


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _seed_cash(conn: Any) -> None:
    _seed_golden(conn)
    insert_cash_movement(conn, account_id="moomoo_my", move_date=date(2026, 1, 5),
                         kind="DEPOSIT", ccy=Currency.MYR, amount=Decimal("50000"))
    conn.commit()


#: Set the sell amount, fire the input event, and read the form back — all in ONE JS turn,
#: so no timer and no response can have run between the change and the reading.
_SET_SELL_AND_READ = """
(amount) => {
  const a = document.querySelector('#cfx-from-amt');
  a.value = amount;
  a.dispatchEvent(new Event('input', { bubbles: true }));
  const cap = document.querySelector('#cfx-estimate');
  return {
    buy: document.querySelector('#cfx-to-amt').value,
    implied: document.querySelector('#cfx-implied').textContent,
    caption: cap.hidden ? '' : cap.textContent,
  };
}
"""


@pytest.mark.e2e
def test_pristine_buy_amount_never_outlives_its_sell_amount(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_cash)
    page = fresh_page
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(getattr(m, "text", ""))
            if getattr(m, "type", None) == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    page.goto(base + "/cash.html#fx", wait_until="load")
    page.wait_for_selector("#cfx-account option", state="attached")
    page.select_option("#cfx-account", "moomoo_my")
    page.wait_for_function(
        "() => { const n = document.querySelector('#cfx-balance');"
        " return n && n.textContent.includes('50,000'); }")

    # ---- baseline: the estimate lands and the caption explains it ----------------------
    page.fill("#cfx-from-amt", "50000")
    page.wait_for_function(
        f"() => document.querySelector('#cfx-to-amt').value === '{_FIRST}'")

    # ---- M5-08 step 1: CLEARING the sell amount must take its estimate with it ---------
    cleared = page.evaluate(_SET_SELL_AND_READ, "")
    assert cleared["buy"] == "", (
        "clearing 賣出金額 left the auto-filled 買入金額 behind: "
        f"{cleared['buy']!r} — it is now an estimate of nothing")

    # ---- M5-08 step 2: a NEW sell amount must not be shown the OLD estimate ------------
    # This is the reported P1 path: 可用餘額 fills 42,000 while 1,573.70 (the 50,000
    # estimate) still sits in the buy field, and only the 隱含匯率 line hints at it.
    changed = page.evaluate(_SET_SELL_AND_READ, "40000")
    assert changed["buy"] != _FIRST, (
        "the previous estimate survived a sell-amount change — 確認 here books a rate "
        "no provider quoted")
    assert changed["buy"] == "", f"a pristine buy field must be empty while asking: {changed}"
    # M5-04 (the feedback half): the page says it is working, rather than looking idle for
    # the ~1.9 s the endpoint takes.
    assert "試算中" in changed["caption"], (
        f"no in-progress disclosure while the estimate is pending: {changed['caption']!r}")
    # ...and the 隱含匯率 line — the ONLY tell the reporter had — cannot quote a rate built by
    # dividing a new sell amount by an old estimate. It reads 「—」 until both sides are real.
    assert "=" not in changed["implied"], (
        "隱含匯率 quoted a rate from 40,000 ÷ the 50,000 estimate: "
        f"{changed['implied']!r}")

    # ---- ...and the fresh estimate still arrives ---------------------------------------
    page.wait_for_function(
        f"() => document.querySelector('#cfx-to-amt').value === '{_SECOND}'")
    page.wait_for_function(
        "() => { const c = document.querySelector('#cfx-estimate');"
        " return c && !c.hidden && c.textContent.includes('匯率')"
        " && c.textContent.includes('試算'); }")

    # ---- counter-evidence: a MANUAL buy amount is still never touched -------------------
    # The user may always enter the amount actually dealt; the staleness fix must not have
    # turned that deliberate behaviour into an overwrite.
    page.fill("#cfx-to-amt", "9100")
    page.wait_for_selector("#cfx-reestimate", state="visible")
    manual = page.evaluate(_SET_SELL_AND_READ, "30000")
    assert manual["buy"] == "9100", "a hand-entered 買入金額 was cleared by the estimate"
    page.wait_for_timeout(800)  # > debounce + request
    assert page.input_value("#cfx-to-amt") == "9100", "manual buy amount was overwritten"

    # ---- M5-04 (the message half): 確認 before the estimate lands says WHY ---------------
    # One JS turn again: the click happens inside the debounce window by construction.
    page.click("#cfx-reestimate")          # back to pristine
    page.wait_for_function(
        "() => document.querySelector('#cfx-to-amt').value !== '9100'")
    page.evaluate("""() => {
      const a = document.querySelector('#cfx-from-amt');
      a.value = '5000';
      a.dispatchEvent(new Event('input', { bubbles: true }));
      document.querySelector('#cfx-confirm').click();
    }""")
    page.wait_for_selector(".toast-fail")
    toast = page.inner_text(".toast-fail")
    assert "試算" in toast, (
        "確認 during the estimate blamed the user for an empty field the page was about "
        f"to fill: {toast!r}")

    assert not console_errors and not page_errors, (
        f"fx estimate staleness flow: console={console_errors!r} page={page_errors!r}")
