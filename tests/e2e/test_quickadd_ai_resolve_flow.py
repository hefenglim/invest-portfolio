"""E2E (Playwright, real server + real frontend): FU-D42 quick-add dialog behaviors.

Drives the REAL stack (uvicorn subprocess + SQLite + served web/) from the watchlist add
flow (instruments.html — the shared dialog, so the AI-row 立即註冊 entry gets the identical
behavior). The two network seams are stubbed with ``page.route`` (the flow server has no
LLM and must not touch providers):

  * ``GET /api/instruments/lookup`` — canned: UMC/ZZZZ/YYYY → found:false, 2303 → found,
  * ``POST /api/instruments/ai-resolve`` — canned suggestion (2303/聯電, or YYYY for the
    still-unfound branch).

Asserts: (a) auto-lookup fires on open with the prefilled symbol (FU-D42b), (b) the symbol
field is EDITABLE and editing re-fires the lookup + re-fills suggestions (FU-D42a), (c) the
查無報價 state offers 「AI 判讀代號」 whose suggestion is re-verified by the REAL lookup —
found ⇒ confirm enabled; still unfound ⇒ the honest 「AI 判讀後仍查無報價」 notice (FU-D42c).
ZERO console / page errors throughout (all stubbed responses are 200s).
"""

import json
import sqlite3
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
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
    """Canned lookup: 2303 is found (name 聯電); everything else is 查無報價."""

    def _handler(route: Route) -> None:
        params = parse_qs(urlparse(route.request.url).query)
        sym = (params.get("symbol") or [""])[0].upper()
        seen_symbols.append(sym)
        if sym == "2303":
            body = {"found": True, "registered": False, "archived": False,
                    "name": "聯電", "sector": "", "board": "TWSE", "is_etf": False}
        else:
            body = {"found": False, "registered": False, "archived": False,
                    "name": "", "sector": "", "board": None, "is_etf": False}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route("**/api/instruments/lookup**", _handler)


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
def test_quickadd_auto_lookup_on_open_and_editable_symbol_refires(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """FU-D42a/b: the lookup fires on open WITHOUT typing (查無報價 for UMC), the symbol
    field is editable, and editing it re-fires the lookup which re-fills the name."""
    base = flow_server(_seed)
    page = fresh_page
    console_errors, page_errors = _collect_errors(page)
    seen: list[str] = []
    _route_lookup(page, seen)

    _open_dialog(page, base, "UMC")
    dialog = page.locator(".modal-backdrop").last
    sym_input = dialog.locator("input.qa-symbol")
    expect(sym_input).to_have_value("UMC")
    expect(sym_input).to_be_editable()  # FU-D42a: never readonly

    # (b) auto-lookup on open: the not-found notice + AI fallback appear with zero typing.
    expect(dialog.get_by_text("查無報價")).to_be_visible()
    expect(dialog.get_by_role("button", name="AI 判讀代號")).to_be_visible()
    expect(dialog.get_by_role("button", name="確認", exact=True)).to_be_disabled()
    assert "UMC" in seen  # the open itself queried the real lookup endpoint

    # (a) editing the symbol re-fires the (debounced) lookup and re-fills the name.
    sym_input.fill("2303")
    expect(dialog.locator("input.qa-name")).to_have_value("聯電")
    expect(dialog.get_by_text("已找到")).to_be_visible()
    expect(dialog.get_by_role("button", name="確認", exact=True)).to_be_enabled()
    expect(dialog.get_by_role("button", name="AI 判讀代號")).to_be_hidden()
    assert seen[-1] == "2303"

    assert not console_errors and not page_errors, (
        f"editable-symbol flow: console={console_errors!r} page={page_errors!r}"
    )


@pytest.mark.e2e
def test_quickadd_ai_resolve_verifies_via_real_lookup(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """FU-D42c: 查無報價 → 「AI 判讀代號」 fills the suggestion and AUTO re-runs the lookup
    (the authority): found ⇒ confirm enabled; a suggestion the lookup still cannot find ⇒
    the honest 「AI 判讀後仍查無報價」 notice with the confirm still blocked."""
    base = flow_server(_seed)
    page = fresh_page
    console_errors, page_errors = _collect_errors(page)
    seen: list[str] = []
    _route_lookup(page, seen)

    def _ai_route(route: Route) -> None:
        post = route.request.post_data or ""
        if "ZZZZ" in post:  # phase 2: a suggestion the lookup cannot verify either
            body = {"symbol": "YYYY", "name": "", "verified": False}
        else:  # phase 1 (the owner's bug): UMC/聯電 → the TW local code
            body = {"symbol": "2303", "name": "聯電", "verified": False}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route("**/api/instruments/ai-resolve", _ai_route)

    _open_dialog(page, base, "UMC")
    dialog = page.locator(".modal-backdrop").last
    sym_input = dialog.locator("input.qa-symbol")
    ai_btn = dialog.get_by_role("button", name="AI 判讀代號")
    confirm = dialog.get_by_role("button", name="確認", exact=True)

    # Phase 1: not found → AI resolve → suggestion filled → real lookup verifies → enabled.
    expect(ai_btn).to_be_visible()
    ai_btn.click()
    expect(sym_input).to_have_value("2303")
    expect(dialog.locator("input.qa-name")).to_have_value("聯電")
    expect(dialog.get_by_text("已找到")).to_be_visible()
    expect(confirm).to_be_enabled()
    assert seen[-1] == "2303"  # the AI suggestion was re-verified by the REAL lookup

    # Phase 2: an unverifiable suggestion stays blocked with the honest notice.
    sym_input.fill("ZZZZ")
    expect(dialog.get_by_text("查無報價")).to_be_visible()
    expect(ai_btn).to_be_visible()
    ai_btn.click()
    expect(sym_input).to_have_value("YYYY")
    expect(dialog.get_by_text("AI 判讀後仍查無報價")).to_be_visible()
    expect(confirm).to_be_disabled()
    assert seen[-1] == "YYYY"  # still verified against the authority, which refused

    assert not console_errors and not page_errors, (
        f"AI-resolve flow: console={console_errors!r} page={page_errors!r}"
    )
