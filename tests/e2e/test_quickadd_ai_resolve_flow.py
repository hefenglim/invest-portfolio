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
manual 「AI 辨識」 retry stays available; (d) Wave A1: the single unified 「AI 辨識」 action ALWAYS
fills 產業 (the old standalone sector-detect else-branch left it unset). ZERO console / page
errors (all stubs are 200s).
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
    not_found reply shows 「查無此標的」 with the manual 「AI 辨識」 retry available."""
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
    ai_btn = dialog.get_by_role("button", name="AI 辨識")
    expect(ai_btn).to_be_visible()  # the manual retry affordance for the same call
    expect(cands).to_have_count(0)  # stale candidates cleared

    assert not console_errors and not page_errors, (
        f"candidates/not_found flow: console={console_errors!r} page={page_errors!r}"
    )


@pytest.mark.e2e
def test_candidate_pick_keeps_name_sector_when_revalidation_misses(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """Fix #1 (owner bug #4 — the candidate-pick wipe): picking a candidate whose LIVE
    re-validation quote MISSES must KEEP the AI-supplied 名稱/產業 (never blank them) and still let
    the user register — 確認 stays ENABLED because POST /api/instruments force-registers a
    quote-less symbol (A6, no dead-end). Before the fix, the miss wiped name+sector (they were
    programmatically filled, so still 'pristine') and left 確認 disabled."""
    base = flow_server(_seed)
    page = fresh_page
    console_errors, page_errors = _collect_errors(page)

    def _lookup(route: Route) -> None:
        params = parse_qs(urlparse(route.request.url).query)
        sym = (params.get("symbol") or [""])[0].upper()
        # 9998 (the picked candidate) is UNPRICED — the exact owner-bug condition.
        found = sym == "2330"
        body = {"found": found, "registered": False, "archived": False,
                "name": "台積電" if found else "", "sector": "",
                "board": "TWSE" if found else None, "is_etf": False}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    def _resolve(route: Route) -> None:
        body = {"status": "candidates", "confidence": "medium", "candidates": [
            {"symbol": "9998", "name": "測試電子", "sector": "Information Technology",
             "verified": False},
            {"symbol": "2330", "name": "台積電", "sector": "Information Technology",
             "verified": False}]}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route("**/api/instruments/lookup**", _lookup)
    page.route("**/api/instruments/ai-resolve", _resolve)

    _open_dialog(page, base, "PICKME")  # a miss → auto-resolve → candidates
    dialog = page.locator(".modal-backdrop").last
    cands = dialog.locator("button.qa-cand")
    expect(cands).to_have_count(2)

    # Pick the UNPRICED candidate 9998 → its live re-validation MISSES.
    cands.first.click()
    sym_input = dialog.locator("input.qa-symbol")
    expect(sym_input).to_have_value("9998")
    # THE FIX: name + sector are KEPT despite the re-validation miss (owner bug #4).
    expect(dialog.locator("input.qa-name")).to_have_value("測試電子")
    expect(dialog.locator(".sector-select")).to_have_value("Information Technology")
    # A6: no dead-end — 確認 is enabled so the user can register the quote-less symbol.
    expect(dialog.get_by_role("button", name="確認", exact=True)).to_be_enabled()
    expect(dialog.get_by_text("已保留 AI 判讀名稱")).to_be_visible()

    assert not console_errors and not page_errors, (
        f"candidate-keep flow: console={console_errors!r} page={page_errors!r}"
    )


@pytest.mark.e2e
def test_manual_ai_resolve_seeds_dedup_key_no_redundant_refire(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """A2: a MANUAL 「AI 辨識」 resolve seeds the dedup key, so re-checking the SAME settled
    (still-unpriced) input does not redundantly re-fire the automatic resolver — the LLM is
    consulted once for the manual action, not a second time on the benign re-lookup."""
    base = flow_server(_seed)
    page = fresh_page
    console_errors, page_errors = _collect_errors(page)

    resolve_posts: list[str] = []

    def _lookup(route: Route) -> None:
        params = parse_qs(urlparse(route.request.url).query)
        sym = (params.get("symbol") or [""])[0].upper()
        found = sym == "2330"  # opening symbol found (no auto-fire); resolved 9998 stays unpriced
        body = {"found": found, "registered": False, "archived": False,
                "name": "台積電" if found else "", "sector": "",
                "board": "TWSE" if found else None, "is_etf": False}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    def _resolve(route: Route) -> None:
        resolve_posts.append(route.request.post_data or "")
        body = {"status": "resolved", "symbol": "9998", "name": "測試電子",
                "sector": "Information Technology", "industry": "Semiconductors",
                "confidence": "high", "verified": True}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route("**/api/instruments/lookup**", _lookup)
    page.route("**/api/instruments/ai-resolve", _resolve)

    _open_dialog(page, base, "2330")  # FOUND → no auto-fire on open
    dialog = page.locator(".modal-backdrop").last
    expect(dialog.get_by_text("已找到")).to_be_visible()
    assert resolve_posts == []  # nothing auto-fired

    # MANUAL resolve → fills 9998 (unpriced); the re-validation MISS keeps fields + seeds the key.
    dialog.get_by_role("button", name="AI 辨識").click()
    sym_input = dialog.locator("input.qa-symbol")
    expect(sym_input).to_have_value("9998")
    expect(dialog.locator("input.qa-name")).to_have_value("測試電子")
    expect(dialog.get_by_role("button", name="確認", exact=True)).to_be_enabled()
    assert len(resolve_posts) == 1

    # The user reviews/touches the name (so a benign re-lookup preserves it → the settled key is
    # stable), then re-enters the SAME symbol. The dedup key seeded by the manual resolve must
    # suppress a redundant automatic re-fire. The sector clearing PROVES the re-lookup actually
    # ran (its pristine-only stale-clear fired), so count==1 is a real no-refire, not a no-op.
    dialog.locator("input.qa-name").dispatch_event("input")
    sym_input.fill("9998")
    page.wait_for_timeout(600)  # cover the 300ms symbol-input debounce + margin
    expect(dialog.locator("input.qa-name")).to_have_value("測試電子")  # kept (name is non-pristine)
    expect(dialog.locator(".sector-select")).to_have_value("")          # re-lookup DID run
    assert len(resolve_posts) == 1, f"redundant automatic re-fire: {resolve_posts!r}"

    assert not console_errors and not page_errors, (
        f"manual-seed flow: console={console_errors!r} page={page_errors!r}"
    )


@pytest.mark.e2e
def test_quickadd_unified_ai_button_fills_sector(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """Wave A1 regression: the SINGLE 「AI 辨識」 action ALWAYS fills 產業 through one path.

    The old standalone sector-detect button's else-branch set only a note and left 產業 unset
    (owner screenshot: 「AI 偵測完成，遺漏了產業自動帶入」). Here a symbol the lookup FINDS but with
    a blank sector, then a MANUAL 「AI 辨識」 click, must populate the sector <select> AND 產業細分
    via the unified applyResolved path — proving the else-branch dead end is gone."""
    base = flow_server(_seed)
    page = fresh_page
    console_errors, page_errors = _collect_errors(page)
    seen: list[str] = []
    _route_lookup(page, seen)  # 2330 → found with sector "" (blank); resolve returns the sector
    _route_ai_resolve(page)

    _open_dialog(page, base, "2330")  # lookup FINDS 2330 (sector blank) → no auto-resolve fires
    dialog = page.locator(".modal-backdrop").last
    # found, but the provider lookup carried NO sector → the select is unset (the bug's setup).
    expect(dialog.get_by_text("已找到")).to_be_visible()
    expect(dialog.locator(".sector-select")).to_have_value("")

    # ONE manual click on the unified action → 產業 (sector) + 產業細分 (industry) both fill.
    dialog.get_by_role("button", name="AI 辨識").click()
    expect(dialog.locator(".sector-select")).to_have_value("Information Technology")
    expect(dialog.locator("input.qa-industry")).to_have_value("Semiconductors")

    assert not console_errors and not page_errors, (
        f"unified-AI sector-fill flow: console={console_errors!r} page={page_errors!r}"
    )
