"""E2E: a name typed into 觀察清單 › 加入 goes straight to AI 辨識 — no quote lookup first.

Owner 2026-09-30 (verifier R8 F-01): typing 台積電 used to run ``GET /api/instruments/lookup``,
whose provider quote lookup took 25–30 s to answer 「查無報價」, and only then fired AI 辨識. A
string with CJK characters can never be a ticker, so the dialog skips the lookup for it (the
backend skips the provider too — tests/contract/test_quickadd_cjk_skips_provider_lookup.py).

Driven through the real watchlist page against a real server. Both network seams are stubbed
with ``page.route`` and COUNTED, so the assertions are about what the dialog asked for:

* switch ON (default): 台積電 → zero lookup calls for the name, ONE ai-resolve, then the one
  re-validation lookup of the code the AI returned (2330);
* switch OFF: the 「…（自動辨識已在設定關閉）」 hint at once — zero lookups, zero ai-resolve —
  even when ``/api/ui-prefs`` answers AFTER the dialog opened (the prefilled name no longer
  waits on a slow lookup, so the switch must be read before the miss branch decides);
* a pure code (F-02's ``ZZZZ9``) keeps today's path exactly: one lookup, ZERO ai-resolve.

Nothing leaves the loopback: every request the page makes is to the flow server (the webfont
stylesheet is answered offline by the e2e context's third-party stub).
"""

import json
import sqlite3
import time
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import Page, Request, Route, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import upsert_instrument
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from tests.e2e.conftest import ALLOWED_REMOTE_HOSTS, FlowServerFactory

_TAIPEI = ZoneInfo("Asia/Taipei")
_OFF_HINT = "自動辨識已在設定關閉"


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
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


class _Seams:
    """Counted stubs for the two network seams + a record of every URL the page asked for."""

    def __init__(self, page: Page, base: str) -> None:
        self.lookups: list[str] = []
        self.resolves: list[str] = []
        self.urls: list[str] = []
        self.base = base
        page.on("request", self._on_request)
        page.route("**/api/instruments/lookup**", self._lookup)
        page.route("**/api/instruments/ai-resolve", self._resolve)

    def _on_request(self, req: Request) -> None:
        self.urls.append(req.url)

    def _lookup(self, route: Route) -> None:
        sym = (parse_qs(urlparse(route.request.url).query).get("symbol") or [""])[0]
        self.lookups.append(sym)
        found = sym == "2330"
        body = {"found": found, "registered": False, "archived": False,
                "name": "台積電" if found else "", "sector": "",
                "board": "TWSE" if found else None, "is_etf": False}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    def _resolve(self, route: Route) -> None:
        self.resolves.append(route.request.post_data or "")
        body = {"status": "resolved", "symbol": "2330", "name": "台積電",
                "sector": "Information Technology", "industry": "Semiconductors",
                "confidence": "high", "verified": True}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    def assert_loopback_only(self) -> None:
        """The webfont host is the one remote the pages name, and the context's third-party
        stub answers it offline (tests/e2e/conftest.py) — anything else is a leak."""
        stray = [u for u in self.urls
                 if not (u.startswith(self.base) or u.startswith("data:")
                         or urlparse(u).netloc in ALLOWED_REMOTE_HOSTS)]
        assert not stray, f"requests left the loopback: {stray}"


def _prefs_off(page: Page, delay_s: float = 0.0) -> None:
    """Serve 自動辨識 = OFF, optionally LATE — after the dialog has already opened."""

    def _handler(route: Route) -> None:
        if route.request.method != "GET":
            route.continue_()
            return
        if delay_s:
            time.sleep(delay_s)
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"page_size": 50, "auto_ai_resolve": False}))

    page.route("**/api/ui-prefs", _handler)


def _open_dialog(page: Page, base: str, symbol: str) -> None:
    page.goto(base + "/instruments.html", wait_until="load")
    page.wait_for_selector("#inst-body tr")
    page.fill("#new-symbol", symbol)
    page.click("#quick-add-btn")  # market select defaults to TW
    page.wait_for_selector(".modal-backdrop")


def _collect_errors(page: Page) -> list[str]:
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    return errors


@pytest.mark.e2e
def test_a_name_goes_straight_to_ai_resolve_without_a_lookup(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed)
    page = fresh_page
    errors = _collect_errors(page)
    seams = _Seams(page, base)

    _open_dialog(page, base, "台積電")
    dialog = page.locator(".modal-backdrop").last
    sym_input = dialog.locator("input.qa-symbol")
    expect(sym_input).to_have_value("2330")               # the AI's code, re-validated
    expect(dialog.get_by_text("已找到")).to_be_visible()
    expect(dialog.get_by_role("button", name="確認", exact=True)).to_be_enabled()

    assert "台積電" not in seams.lookups, (
        f"a name must never reach the quote lookup: {seams.lookups}")
    assert seams.lookups == ["2330"], seams.lookups       # only the re-validation
    assert len(seams.resolves) == 1 and "台積電" in json.loads(seams.resolves[0])["query"]
    seams.assert_loopback_only()
    assert not errors, errors


@pytest.mark.e2e
def test_a_code_then_a_name_typed_over_it(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """F-02 then F-01 in one dialog: the pure code is looked up and never auto-resolved;
    typing a name over it fires AI 辨識 with no lookup for the name."""
    base = flow_server(_seed)
    page = fresh_page
    errors = _collect_errors(page)
    seams = _Seams(page, base)

    _open_dialog(page, base, "ZZZZ9")
    dialog = page.locator(".modal-backdrop").last
    expect(dialog.get_by_text("若是用名稱找").first).to_be_visible()
    assert seams.lookups == ["ZZZZ9"] and seams.resolves == [], (seams.lookups,
                                                                seams.resolves)

    dialog.locator("input.qa-symbol").fill("鴻海")
    expect(dialog.locator("input.qa-symbol")).to_have_value("2330")
    assert "鴻海" not in seams.lookups, seams.lookups
    assert seams.lookups == ["ZZZZ9", "2330"], seams.lookups
    assert len(seams.resolves) == 1 and "鴻海" in json.loads(seams.resolves[0])["query"]
    seams.assert_loopback_only()
    assert not errors, errors


@pytest.mark.e2e
@pytest.mark.parametrize("prefs_delay_s", [0.0, 1.0], ids=["prefs-first", "prefs-late"])
def test_with_the_switch_off_a_name_shows_the_hint_at_once(
    flow_server: FlowServerFactory, fresh_page: Page, prefs_delay_s: float
) -> None:
    base = flow_server(_seed)
    page = fresh_page
    errors = _collect_errors(page)
    seams = _Seams(page, base)
    _prefs_off(page, delay_s=prefs_delay_s)

    _open_dialog(page, base, "台積電")
    dialog = page.locator(".modal-backdrop").last
    expect(dialog.get_by_text(_OFF_HINT).first).to_be_visible()
    expect(dialog.locator("button.qa-ai-resolve")).to_be_visible()   # the manual path stays
    page.wait_for_timeout(300)  # a late auto-fire would land here
    assert seams.lookups == [], seams.lookups
    assert seams.resolves == [], "the switch is off — nothing may auto-fire"
    seams.assert_loopback_only()
    assert not errors, errors
