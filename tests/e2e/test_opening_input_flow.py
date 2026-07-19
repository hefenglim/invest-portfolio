"""E2E: the 期初庫存 (opening inventory) input pane, driven against the REAL stack (FU-D21).

Drives the opening-inventory form (which lives in the tab renamed 換匯＋期初 → 期初庫存 after the
FX card was removed in FU-D22) through the one-row-CSV import seam, then verifies the DOWNSTREAM
holding via /api/dashboard and the row via the 期初 ledger tab.

Covers the o-avg / o-total interplay explicitly (both fields are editable):
  * total PROVIDED  → original_cost_total wins server-side; the holding's original_avg is
    recomputed on read as total / shares (NOT the entered avg).
  * total OMITTED   → original_cost_total is computed as avg × shares.

ZERO console / page errors throughout.
"""

import json
import urllib.request
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from playwright.sync_api import Page
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.data_ingestion.store import upsert_instrument
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
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


def _seed_openings(conn: Any) -> None:
    """Golden scenario + two REGISTERED-but-never-traded TW instruments, so an opening is the
    holding's ONLY source (its computed-on-read avg is unambiguous)."""
    _seed_golden(conn)
    upsert_instrument(conn, Instrument(symbol="2454", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Semiconductors", name="MediaTek", board="TWSE"))
    upsert_instrument(conn, Instrument(symbol="2317", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Electronics", name="Hon Hai", board="TWSE"))
    conn.commit()


def _get_json(base_url: str, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(base_url + path, timeout=5) as r:  # noqa: S310 (loopback)
        data: dict[str, Any] = json.loads(r.read().decode("utf-8"))
        return data


def _holding(base: str, symbol: str, account_id: str) -> dict[str, Any] | None:
    rows: list[dict[str, Any]] = _get_json(base, "/api/dashboard")["holdings"]
    for h in rows:
        if h["symbol"] == symbol and h["account_id"] == account_id:
            return h
    return None


def _sink(page: Page) -> tuple[list[str], list[str]]:
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(getattr(m, "text", ""))
            if getattr(m, "type", None) == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    return console_errors, page_errors


def _open_opening_tab(page: Page, base: str) -> None:
    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#o-account option", state="attached")   # initFxOpen() has run
    page.click("#tab-fxopen")
    page.wait_for_selector("#o-symbol", state="visible")


def _commit_opening(page: Page) -> None:
    with page.expect_response("**/api/import/commit") as cm:
        page.click("#o-confirm")
    assert cm.value.status == 200, f"opening commit status {cm.value.status}"
    page.wait_for_selector(".toast-ok")


@pytest.mark.e2e
def test_opening_inventory_downstream_and_ledger(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_openings)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    _open_opening_tab(page, base)

    # ===== (A) total PROVIDED (12,000) ≠ avg×shares (50×200=10,000): total wins server-side ==
    page.select_option("#o-account", "tw_broker")
    page.fill("#o-symbol", "2454")
    page.fill("#o-shares", "200")
    page.fill("#o-avg", "50")
    page.fill("#o-total", "12000")
    page.fill("#o-date", "2025-06-01")
    _commit_opening(page)

    a = _holding(base, "2454", "tw_broker")
    assert a is not None
    assert Decimal(a["shares"]) == Decimal("200")
    assert Decimal(a["original_cost_total"]) == Decimal("12000")   # the provided total wins
    assert Decimal(a["adjusted_cost_total"]) == Decimal("12000")
    assert Decimal(a["original_avg"]) == Decimal("60")             # 12000 / 200, computed on read

    # ===== (B) total OMITTED: original_cost_total computed as avg × shares ===================
    page.select_option("#o-account", "tw_broker")
    page.fill("#o-symbol", "2317")
    page.fill("#o-shares", "100")
    page.fill("#o-avg", "100")
    page.fill("#o-total", "")                                      # omitted -> computed
    page.fill("#o-date", "2025-06-02")
    _commit_opening(page)

    b = _holding(base, "2317", "tw_broker")
    assert b is not None
    assert Decimal(b["shares"]) == Decimal("100")
    assert Decimal(b["original_cost_total"]) == Decimal("10000")   # 100 × 100 computed
    assert Decimal(b["original_avg"]) == Decimal("100")

    # ===== the 期初 ledger tab lists both openings (reload so the ledger re-fetches) =========
    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#open-body tr", state="attached")
    page.click("#tab-lopen")
    page.wait_for_selector("#pane-lopen.active", state="attached")
    page.wait_for_function(
        "() => { const t = document.querySelector('#open-body');"
        " return t && t.textContent.includes('2454') && t.textContent.includes('2317'); }"
    )
    # the LEDGER keeps the AS-ENTERED avg (50) even though the holding recomputed it to 60.
    openings = _get_json(base, "/api/ledgers/openings?symbol=2454")["rows"]
    assert len(openings) == 1
    assert Decimal(openings[0]["avg"]) == Decimal("50")           # stored as entered
    assert Decimal(openings[0]["total"]) == Decimal("12000")
    assert Decimal(openings[0]["shares"]) == Decimal("200")

    assert not console_errors and not page_errors, (
        f"opening input flow: console={console_errors!r} page={page_errors!r}"
    )
