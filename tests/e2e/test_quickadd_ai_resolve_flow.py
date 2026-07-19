"""E2E (Playwright, real server + real frontend): the R6-B unified quick-add AI resolve.

Drives the REAL stack (uvicorn subprocess + SQLite + served web/) from the watchlist add flow
(instruments.html — the shared dialog, so every quick-add entry inherits the identical
behavior). The two network seams are stubbed with ``page.route`` (the flow server has no LLM
and must not touch providers):

  * ``GET /api/instruments/lookup`` — canned: 2303/2330 → found, everything else → 查無報價,
  * ``POST /api/instruments/ai-resolve`` — the UNIFIED endpoint, canned by query:
      - default (UMC/聯電) → status:"resolved" (2303 聯電 + GICS sector + industry),
      - "MULTI" → status:"candidates" (2303 + 2330),
      - "NOPE"  → status:"not_found".

Asserts the NEW automatic behavior: (a) the AI resolve fires AUTOMATICALLY on a lookup miss and
auto-fills 代號/名稱/產業/產業細分, then the real lookup re-validates the code (verified ⇒ 確認
enabled) — the owner's 聯電→UMC bug is fixed with ZERO clicks; (b) a candidates reply renders
clickable rows whose pick fills + re-validates; (c) a not_found reply shows 「查無此標的」 and the
manual 「AI 判讀代號」 retry stays available. ZERO console / page errors (all stubs are 200s).
"""

import json
import sqlite3
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import Page, Route, expect
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
    """One watch-only instrument so instruments.html boots with a rendered row."""
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="WATCH", market=Market.US,
                                       quote_ccy=Currency.USD, sector="Tech", name="Watchy"))
    upsert_prices(conn, [PriceRow(instrument="WATCH", market=Market.US,
                                  as_of=date(2026, 6, 9), close=Decimal("50"),
                                  source="test")],
                  fetched_at=datetime(2026, 6, 9, 15, 0, tzinfo=_TAIPEI))
    conn.commit()


def _route_lookup(page: Page, seen_symbols: list[str]) -> None:
    """Canned lookup: 2303 (聯電) + 2330 (台積電) are found; everything else is 查無報價."""

    def _handler(route: Route) -> None:
        params = parse_qs(urlparse(route.request.url).query)
        sym = (params.get("symbol") or [""])[0].upper()
        seen_symbols.append(sym)
        names = {"2303": "聯電", "2330": "台積電"}
        if sym in names:
            body = {"found": True, "registered": False, "archived": False,
                    "name": names[sym], "sector": "", "board": "TWSE", "is_etf": False}
        else:
            body = {"found": False, "registered": False, "archived": False,
                    "name": "", "sector": "", "board": None, "is_etf": False}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route("**/api/instruments/lookup**", _handler)


def _route_ai_resolve(page: Page) -> None:
    """The UNIFIED resolve, canned by the query in the POST body (status-based replies)."""

    def _handler(route: Route) -> None:
        post = route.request.post_data or ""
        body: dict[str, Any]
        if "MULTI" in post:
            body = {"status": "candidates", "confidence": "medium", "candidates": [
                {"symbol": "2303", "name": "聯電", "sector": "Information Technology",
                 "verified": False},
                {"symbol": "2330", "name": "台積電", "sector": "Information Technology",
                 "verified": False}]}
        elif "NOPE" in post:
            body = {"status": "not_found", "message": "查無此標的 — 請確認名稱與市場是否正確"}
        else:  # the owner's bug: UMC/聯電 → the TW local code, sector + industry in one reply.
            body = {"status": "resolved", "symbol": "2303", "name": "聯電",
                    "sector": "Information Technology", "industry": "Semiconductors",
                    "confidence": "high", "verified": True}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route("**/api/instruments/ai-resolve", _handler)


def _open_dialog(page: Page, base_url: str, symbol: str) -> None:
    page.goto(base_url + "/instruments.html", wait_until="load")
    page.wait_for_selector("#inst-body tr")
    page.fill("#new-symbol", symbol)
    page.click("#quick-add-btn")  # market select defaults to TW
    page.wait_for_selector(".modal-backdrop")


def _collect_errors(page: Page) -> tuple[list[str], list[str]]:
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(getattr(m, "text", ""))
            if getattr(m, "type", None) == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    return console_errors, page_errors


@pytest.mark.e2e
def test_quickadd_auto_resolve_fills_and_verifies(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """R6-B: opening with a lookup-miss symbol AUTOMATICALLY fires the unified resolve, which
    fills 代號/名稱/產業/產業細分, then the real lookup re-validates → 確認 enabled — no clicks."""
    base = flow_server(_seed)
    page = fresh_page
    console_errors, page_errors = _collect_errors(page)
    seen: list[str] = []
    _route_lookup(page, seen)
    _route_ai_resolve(page)

    _open_dialog(page, base, "UMC")  # the owner's bug input
    dialog = page.locator(".modal-backdrop").last
    sym_input = dialog.locator("input.qa-symbol")
    expect(sym_input).to_be_editable()

    # AUTOMATIC: no button click — the miss auto-resolves UMC → 2303 and re-validates it.
    expect(sym_input).to_have_value("2303")
    expect(dialog.locator("input.qa-name")).to_have_value("聯電")
    expect(dialog.locator("input.qa-industry")).to_have_value("Semiconductors")
    # the GICS sector rode in from the resolve and survived the re-validation lookup.
    expect(dialog.locator(".sector-select")).to_have_value("Information Technology")
    expect(dialog.get_by_text("已找到")).to_be_visible()
    expect(dialog.get_by_role("button", name="確認", exact=True)).to_be_enabled()
    assert seen[-1] == "2303"  # the AI suggestion was re-verified by the REAL lookup

    assert not console_errors and not page_errors, (
        f"auto-resolve flow: console={console_errors!r} page={page_errors!r}"
    )


@pytest.mark.e2e
def test_quickadd_candidates_pick_then_not_found_retry(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """R6-B: a candidates reply renders clickable rows whose pick fills + re-validates; a
    not_found reply shows 「查無此標的」 with the manual 「AI 判讀代號」 retry available."""
    base = flow_server(_seed)
    page = fresh_page
    console_errors, page_errors = _collect_errors(page)
    seen: list[str] = []
    _route_lookup(page, seen)
    _route_ai_resolve(page)

    # Phase A — candidates: open with MULTI → auto-resolve → 2 clickable rows → pick 2303.
    _open_dialog(page, base, "MULTI")
    dialog = page.locator(".modal-backdrop").last
    sym_input = dialog.locator("input.qa-symbol")
    cands = dialog.locator("button.qa-cand")
    expect(cands).to_have_count(2)
    cands.first.click()
    expect(sym_input).to_have_value("2303")
    expect(dialog.locator("input.qa-name")).to_have_value("聯電")
    expect(dialog.get_by_text("已找到")).to_be_visible()
    expect(dialog.get_by_role("button", name="確認", exact=True)).to_be_enabled()

    # Phase B — not_found: edit to NOPE → auto-resolve → 查無此標的, confirm blocked, retry shown.
    sym_input.fill("NOPE")
    expect(dialog.get_by_text("查無此標的")).to_be_visible()
    expect(dialog.get_by_role("button", name="確認", exact=True)).to_be_disabled()
    ai_btn = dialog.get_by_role("button", name="AI 判讀代號")
    expect(ai_btn).to_be_visible()  # the manual retry affordance for the same call
    expect(cands).to_have_count(0)  # stale candidates cleared

    assert not console_errors and not page_errors, (
        f"candidates/not_found flow: console={console_errors!r} page={page_errors!r}"
    )
