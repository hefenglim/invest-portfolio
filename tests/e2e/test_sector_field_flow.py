"""E2E (Playwright, real server + real frontend): the FU-D31 / R6 canonical sector field.

Drives the REAL stack against a guest DB with one never-traded instrument. Verifies, through
the UI, that the instruments EDIT form (Wave A1: now the shared pdInstQuickAdd builder in
mode:'edit') shows the canonical GICS sector <select> (dual-text options) + the EDIT-only
「重新偵測產業」 button, and that clicking it — the UNIFIED POST /api/instruments/ai-resolve
(sector_only) — degrades gracefully (no LLM on the flow server → 402/409/503) WITHOUT blocking
the form or throwing; the user's selection is preserved. A second test proves the refactored
edit form SAVES via PUT through the shared builder. ZERO console errors (bar the intentional
degrade status) + ZERO page errors.
"""

import sqlite3
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import ConsoleMessage, Page, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import upsert_instrument
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from tests.e2e.conftest import FlowServerFactory

_TAIPEI = ZoneInfo("Asia/Taipei")


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    """Re-enable loopback sockets PER TEST (pytest-socket re-bans before every test); each
    flow spawns a fresh isolated uvicorn (free-port probe + readiness poll need loopback)."""
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _seed(conn: sqlite3.Connection) -> None:
    """One never-traded watch-only US instrument with an OFF-vocabulary sector, so the edit
    form must preserve it as an off-list option while offering the canonical dropdown."""
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="WATCH", market=Market.US, quote_ccy=Currency.USD,
                                       sector="Electronics", name="Watchy"))
    upsert_prices(conn, [PriceRow(instrument="WATCH", market=Market.US, as_of=date(2026, 6, 9),
                                  close=Decimal("50"), source="test")],
                  fetched_at=datetime(2026, 6, 9, 15, 0, tzinfo=_TAIPEI))
    conn.commit()


@pytest.mark.e2e
def test_edit_form_sector_select_and_ai_detect(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base_url = flow_server(_seed)  # guest mode (no users) → no LLM activated
    page = fresh_page

    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(getattr(m, "text", ""))
            if isinstance(m, ConsoleMessage) and m.type == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    page.goto(base_url + "/instruments.html", wait_until="load")
    page.wait_for_selector("#inst-body tr")

    # Open the edit form for WATCH.
    row = page.locator("#inst-body tr").filter(has=page.locator(".sym-code", has_text="WATCH"))
    row.get_by_role("button", name="編輯").click()
    modal = page.locator(".modal-backdrop").last

    # (1) the sector field is now a <select> (not a plain input) + a 重新偵測產業 button.
    sel = modal.locator(".sector-select")
    expect(sel).to_be_visible()
    ai_btn = modal.get_by_role("button", name="重新偵測產業")
    expect(ai_btn).to_be_visible()

    # (2) the canonical GICS vocabulary populated the dropdown (dual-text labels), and the
    #     off-vocabulary stored value 'Electronics' is preserved as a current-value option.
    expect(sel.locator("option", has_text="Information Technology（資訊科技）")).to_have_count(1)
    expect(sel).to_have_value("Electronics")  # off-list value preserved, not destroyed

    # (3) clicking 重新偵測產業 — the unified /api/instruments/ai-resolve (sector_only) — with NO
    #     LLM on the flow server degrades gracefully (402/409/503); the form is not blocked and
    #     the selection is unchanged.
    with page.expect_response("**/api/instruments/ai-resolve") as resp:
        ai_btn.click()
    assert resp.value.status in (402, 409, 503)
    expect(sel).to_have_value("Electronics")  # unchanged after the failed detect
    expect(ai_btn).to_be_enabled()  # the rest of the form still works

    # The intentional 402/409/503 emits ONE benign browser "Failed to load resource" line
    # (same precedent as the AI-input + delete flows); any OTHER console error or ANY
    # uncaught page error still fails the flow.
    def _benign(msg: str) -> bool:
        return "Failed to load resource" in msg and any(
            code in msg for code in ("402", "409", "503"))

    real_console = [e for e in console_errors if not _benign(e)]
    assert not real_console and not page_errors, (
        f"sector field flow: console={real_console!r} page={page_errors!r}"
    )


@pytest.mark.e2e
def test_edit_form_saves_via_shared_builder(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """Wave A1: the EDIT form is now the shared pdInstQuickAdd builder (mode:'edit'). Opening it
    for WATCH locks 代號 (read-only) and DROPS 記一筆買入; setting 目標價下限 and clicking 儲存
    PUTs /api/instruments/{symbol} through the REAL endpoint (no LLM needed). ZERO console/page
    errors — everything here is a 200."""
    base_url = flow_server(_seed)  # guest mode
    page = fresh_page

    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(getattr(m, "text", ""))
            if isinstance(m, ConsoleMessage) and m.type == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    page.goto(base_url + "/instruments.html", wait_until="load")
    page.wait_for_selector("#inst-body tr")

    row = page.locator("#inst-body tr").filter(has=page.locator(".sym-code", has_text="WATCH"))
    row.get_by_role("button", name="編輯").click()
    modal = page.locator(".modal-backdrop").last

    # 代號 is locked read-only; 記一筆買入 is absent in edit mode; 儲存 replaces 確認.
    sym_in = modal.locator("input.qa-symbol")
    expect(sym_in).to_have_value("WATCH")
    expect(sym_in).not_to_be_editable()
    expect(modal.get_by_role("button", name="記一筆買入")).to_have_count(0)

    # Set a target-low bound and save → PUT /api/instruments/WATCH fires and returns 200.
    modal.locator("#edit-target-low").fill("42")
    with page.expect_response(
        lambda r: r.url.endswith("/api/instruments/WATCH") and r.request.method == "PUT"
    ) as put_info:
        modal.get_by_role("button", name="儲存").click()
    assert put_info.value.status == 200

    # The dialog closes on a successful save.
    expect(page.locator(".modal-backdrop")).to_have_count(0)

    assert not console_errors and not page_errors, (
        f"edit save flow: console={console_errors!r} page={page_errors!r}"
    )
