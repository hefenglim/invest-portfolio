"""E2E: 取得成本 field on a FOREIGN cash movement (spec 2026-07-30, acceptance 6/7).

Driven against the REAL stack (fresh uvicorn + on-disk golden DB + headless chromium):

* Acceptance 6 — the field appears ONLY when the movement's currency differs from the
  account's 資金幣別. A home-currency deposit (schwab / TWD) must look exactly as it did
  before this release: no extra field, nothing to fill in.
* Acceptance 7 — when no rate is stored on or before the chosen date, the field is left
  BLANK with a reason (never 0, never today's spot) and the movement is STILL submittable.
  The row then lands with no cost basis, which the dashboard discloses rather than hides.
* The happy path — a date WITH a stored rate pre-fills the reference rate, and submitting
  stores the derived HOME AMOUNT (spec F1: the amount is the authority, the rate is not).

ZERO console / page errors throughout.
"""

import json
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest
from playwright.sync_api import Page
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    """Re-enable loopback sockets PER TEST (pytest-socket re-bans before every test)."""
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _get_json(base_url: str, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(base_url + path, timeout=5) as r:  # noqa: S310 (loopback)
        data: dict[str, Any] = json.loads(r.read().decode("utf-8"))
        return data


def _sink(page: Page) -> tuple[list[str], list[str]]:
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(getattr(m, "text", ""))
            if getattr(m, "type", None) == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    return console_errors, page_errors


def _open_movement_form(page: Page, base: str, account: str, ccy: str, day: str) -> None:
    # The movement form lives under the 出金入金 tab (#flows); on the default #pools tab its
    # controls are attached but HIDDEN, so select_option would wait for actionability.
    page.goto(base + "/cash.html#flows", wait_until="load")
    page.wait_for_selector("#cm-account option", state="attached")
    page.select_option("#cm-account", account)
    page.wait_for_function(
        "(c) => Array.from(document.querySelectorAll('#cm-ccy option'))"
        ".some((o) => o.value === c)", arg=ccy)
    page.select_option("#cm-ccy", ccy)
    page.fill("#cm-date", day)
    page.dispatch_event("#cm-date", "change")


@pytest.mark.e2e
def test_acq_cost_field_is_conditional_and_degrades_without_a_rate(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_golden)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    # ---- acceptance 6: HOME currency -> the field must not exist on screen -------------
    _open_movement_form(page, base, "schwab", "TWD", "2026-06-10")
    assert page.locator("#cm-acq-field").is_hidden()

    # ---- happy path: a date WITH a stored rate pre-fills a reference value --------------
    _open_movement_form(page, base, "schwab", "USD", "2026-06-10")
    page.wait_for_function(
        "() => { const n = document.querySelector('#cm-acq-field');"
        " return n && !n.hidden; }")
    page.wait_for_function(
        "() => { const n = document.querySelector('#cm-acq-hint');"
        " return n && n.textContent.includes('參考值'); }")
    rate = page.input_value("#cm-acq")
    assert rate and rate != "0"
    page.fill("#cm-amount", "1000")
    page.click("#cm-confirm")
    page.wait_for_function(
        "() => { const n = document.querySelector('#cm-amount');"
        " return n && n.value === ''; }")
    rows = _get_json(base, "/api/cash")["movements"]["rows"]
    stored = next(r for r in rows if r["ccy"] == "USD" and r["amount"] == "1000")
    assert stored["acq_home_amount"] is not None      # the AMOUNT is what persists (F1)
    assert stored["acq_home_ccy"] == "TWD"

    # ---- acceptance 7: no stored rate that far back -> blank, explained, still writable --
    _open_movement_form(page, base, "schwab", "USD", "2001-01-01")
    page.wait_for_function(
        "() => { const n = document.querySelector('#cm-acq-hint');"
        " return n && n.textContent.includes('查無'); }")
    assert page.input_value("#cm-acq") == ""          # never 0, never today's spot
    page.fill("#cm-amount", "2500")
    page.click("#cm-confirm")
    page.wait_for_function(
        "() => { const n = document.querySelector('#cm-amount');"
        " return n && n.value === ''; }")
    rows2 = _get_json(base, "/api/cash")["movements"]["rows"]
    blank = next(r for r in rows2 if r["ccy"] == "USD" and r["amount"] == "2500")
    assert blank["acq_home_amount"] is None           # recorded as "cost unknown"

    # ---- the dashboard discloses the gap rather than silently scaling ------------------
    fx = _get_json(base, "/api/dashboard")["fx"]["by_account"]["schwab"]
    assert fx["fx_basis_gap"] not in (None, "0")
    assert fx["covered_ratio"] != "1"

    assert console_errors == [] and page_errors == []
