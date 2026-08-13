"""E2E (Playwright, real server + real frontend): the US **CASH** dividend door (P1b).

Before this release ``#d-drip`` assumed every US payout is reinvested: the pane showed the
reinvest pair and a 預扣 box that was ``readonly`` and hard-computed as ``gross × 0.30``. Two
consequences, both only reachable through the browser:

* a plain-cash US distribution had **no manual door at all** — the only way in was a CSV row
  that then needed a per-row ``dividend_type_mismatch`` confirmation;
* the withholding could not be made to match a statement. A broker rounds each payout its own
  way, so the real number sits a cent from ``gross × 0.30``, and a readonly field cannot
  reproduce a figure it must reconcile to.

This file drives the type switch and the pencil override through a real browser and then reads
the value back OUT of the ledger, because the override is only worth anything if it SURVIVES:
``input.js`` sends the withholding explicitly and ``apply_dividend_model`` keys on the dividend
TYPE, so an override dropped anywhere between the box and the row silently reverts to 30% (or
to 0) and the position's adjusted cost is wrong for the rest of its life.

ZERO console / page errors throughout; the browser context comes from the shared ``fresh_page``
fixture (issue #67's third-party stub).
"""

import json
import urllib.request
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from playwright.sync_api import Page, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory

#: NOT ``gross × 0.30`` (which would be 29.10), and not a rounding of it either — the whole
#: point of the override is that the ledger keeps a number the app would never have computed.
GROSS = "97.00"
WITHHOLDING = "14.55"
NET = Decimal("82.45")


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    """Re-enable loopback sockets PER TEST (pytest-socket re-bans before every test); each
    flow spawns a fresh isolated uvicorn (free-port probe + readiness poll need loopback)."""
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _get_json(base_url: str, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(base_url + path, timeout=10) as r:  # noqa: S310 (loopback)
        data: dict[str, Any] = json.loads(r.read().decode("utf-8"))
        return data


def _sink(page: Page) -> tuple[list[str], list[str]]:
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(getattr(m, "text", ""))
            if getattr(m, "type", None) == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    return console_errors, page_errors


def _holding(base: str, symbol: str, account_id: str) -> dict[str, Any]:
    for h in _get_json(base, "/api/dashboard")["holdings"]:
        if h["symbol"] == symbol and h["account_id"] == account_id:
            row: dict[str, Any] = h
            return row
    raise AssertionError(f"{symbol}/{account_id} not in the dashboard holdings")


@pytest.mark.e2e
def test_us_cash_dividend_with_an_overridden_withholding_reaches_the_ledger(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """現金股利 + a hand-typed 預扣 → the ledger stores the typed number, not 30%.

    Breaks if: the 現金股利 button disappears or stops switching the pane (the row commits as
    DRIP and books $0-cost shares instead of reducing cost); the pencil stops unlocking the
    field (``readOnly`` again — the fill would throw); the override is recomputed away by
    ``recomputeDripAmounts``; or ``input.js`` stops sending the withholding on a CASH row (it
    would book 0, net == gross, and over-reduce the adjusted cost forever).
    """
    base = flow_server(_seed_golden)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    before = _holding(base, "AAPL", "schwab")
    assert Decimal(before["shares"]) == Decimal("10")
    assert Decimal(before["adjusted_cost_total"]) == Decimal("1000")

    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#d-account option", state="attached")
    page.click("#tab-div")
    page.select_option("#d-account", "schwab")
    page.wait_for_selector("#d-drip", state="visible")      # the US (drip_us) pane
    page.fill("#d-symbol", "AAPL")
    page.fill("#d-date", "2026-07-10")

    # --- the type switch: 現金股利 hides the reinvest pair -------------------------------
    # Hiding is not cosmetic — a CASH commit ignores those two fields, and a visible field the
    # commit ignores is how a user comes to believe a reinvestment was recorded.
    expect(page.locator("#d-drip-shares-field")).to_be_visible()
    page.click("#d-us-cash")
    expect(page.locator("#d-us-cash")).to_have_class("active")
    expect(page.locator("#d-drip-shares-field")).to_be_hidden()
    expect(page.locator("#d-drip-price-field")).to_be_hidden()
    expect(page.locator("#d-model-note")).to_contain_text("美股現金股利")

    # --- the override: the auto 30% first, then a number the app would never compute -----
    page.fill("#d-drip-gross", GROSS)
    expect(page.locator("#d-drip-wh")).to_have_value("29.10")     # 97.00 × 0.30, automatic
    expect(page.locator("#d-drip-wh")).to_have_attribute("readonly", "")
    page.click("#d-drip-wh-pencil")
    expect(page.locator("#d-drip-wh-pencil")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#d-drip-wh-ovr")).to_be_visible()        # 已覆寫 chip
    page.fill("#d-drip-wh", WITHHOLDING)
    # Net follows the OVERRIDE, not the 30% — the estimate the user reconciles against.
    expect(page.locator("#d-drip-net")).to_have_value("82.45")

    with page.expect_response("**/api/import/commit") as cm:
        page.click("#d-confirm")
    assert cm.value.status == 200, f"commit status {cm.value.status}"
    assert cm.value.json()["written"] == 1
    page.wait_for_selector(".toast-ok")

    # --- read it back OUT of the ledger --------------------------------------------------
    rows = _get_json(base, "/api/ledgers/dividends?account_id=schwab&symbol=AAPL")["rows"]
    assert len(rows) == 1, rows
    stored = rows[0]
    assert stored["type"] == "cash"
    assert Decimal(stored["gross"]) == Decimal(GROSS)
    assert Decimal(stored["withhold"]) == Decimal(WITHHOLDING)   # the override SURVIVED
    assert Decimal(stored["withhold"]) != Decimal(GROSS) * Decimal("0.30")
    assert Decimal(stored["net"]) == NET
    assert stored["reinvest_shares"] is None                     # cash moves no shares

    # --- and it lands as a CASH dividend (D35): cost reduced, share count untouched -------
    after = _holding(base, "AAPL", "schwab")
    assert Decimal(after["shares"]) == Decimal("10")
    assert Decimal(after["adjusted_cost_total"]) == Decimal("1000") - NET
    assert Decimal(after["original_cost_total"]) == Decimal("1000")

    # --- the pencil resets after a commit ------------------------------------------------
    # A pencil left pressed over an emptied field sends a BLANK withholding on the next
    # dividend, which books 0 on a US payout under W-8BEN.
    expect(page.locator("#d-drip-wh-pencil")).to_have_attribute("aria-pressed", "false")
    expect(page.locator("#d-drip-wh")).to_have_attribute("readonly", "")

    assert console_errors == [] and page_errors == []
