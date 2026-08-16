"""E2E: the AI preview shows a 當沖 marker when the model flagged one (W1 / AI-1).

Drives the REAL stack (uvicorn subprocess + SQLite + served web/) with the AI-parse seam
stubbed by ``page.route``, because the flow server has no LLM configured.

**Why this needs a browser test at all.** ``daytrade`` used to be dropped between the preview
and the write, so whatever the model inferred was harmless. Now it rides all the way to the
ledger and halves the TW sell tax (0.3% -> 0.15%), which makes a wrong inference a money error
the owner can only catch by LOOKING at the parsed row. A marker nobody proved is rendered is
the same as no marker, so the visibility half of the fix is pinned here rather than assumed.

The negative case shares the flow deliberately: a chip that renders on every row would be
worse than none, since it would stop carrying information.
"""

import json
from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory

_ACCOUNT = "tw_broker"
_SYMBOL = "2330"


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    """Re-enable loopback sockets PER TEST (pytest-socket re-bans before every test); each flow
    spawns a fresh isolated uvicorn (free-port probe + readiness poll need loopback TCP)."""
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _preview_body(daytrade: str) -> str:
    """One clean SELL row whose ``daytrade`` is whatever the model is pretending to have said.

    The payload mirrors the real wire shape: ``daytrade`` arrives as the STRING "1"/"0"
    (``csv_import`` renders the flag that way in the preview payload), which is exactly the
    detail a JS truthiness check would get wrong — "0" is a non-empty string.
    """
    return json.dumps({
        "rows": [{
            "n": 0, "status": "ok", "reason": None, "code": None,
            "data": {"account_id": _ACCOUNT, "symbol": _SYMBOL, "side": "sell",
                     "trade_date": "2026-06-02", "quantity": "1000", "price": "600",
                     "fee": "855", "tax": "900" if daytrade == "1" else "1800",
                     "daytrade": daytrade},
        }],
        "summary": {"total": 1, "ok": 1, "warn": 0, "error": 0},
        "meta": {"model": "mock", "via": "litellm", "cost_usd": None},
        "csv_text": (
            "account,symbol,side,date,shares,price,daytrade,note\n"
            f"{_ACCOUNT},{_SYMBOL},SELL,2026-06-02,1000,600,{daytrade},\n"
        ),
    })


def _open_ai_pane(page: Page, base: str, daytrade: str) -> None:
    page.route("**/api/input/ai/preview",
               lambda route: route.fulfill(status=200, content_type="application/json",
                                           body=_preview_body(daytrade)))
    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#csv-kinds .chip", state="attached")
    page.click("#tab-ai")
    page.wait_for_selector("#ai-dropzone", state="visible")
    page.fill("#ai-text", "當沖賣出 2330 一張 600")
    page.click("#ai-parse")
    page.wait_for_selector("#ai-body tr", state="visible")


@pytest.mark.e2e
def test_a_daytrade_row_is_marked_in_the_ai_preview(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_golden)
    page = fresh_page
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(getattr(m, "text", ""))
            if getattr(m, "type", None) == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    _open_ai_pane(page, base, "1")

    chip = page.locator("#ai-body .dir-daytrade")
    expect(chip).to_have_count(1)
    expect(chip).to_have_text("當沖")
    # The tooltip states the consequence, not just the fact — a reader who does not already
    # know the TW tax schedule cannot judge the flag from the word alone.
    assert "0.15%" in (chip.get_attribute("title") or "")

    assert not console_errors and not page_errors, (
        f"console={console_errors} page={page_errors}")


@pytest.mark.e2e
def test_an_ordinary_row_carries_no_marker(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """``daytrade`` arrives as the string "0", which is TRUTHY in JS — the check must compare
    values, not test truthiness, or every row would wear the chip and it would mean nothing."""
    base = flow_server(_seed_golden)
    page = fresh_page

    _open_ai_pane(page, base, "0")

    expect(page.locator("#ai-body tr")).to_have_count(1)  # the row rendered...
    expect(page.locator("#ai-body .dir-daytrade")).to_have_count(0)  # ...without the chip
