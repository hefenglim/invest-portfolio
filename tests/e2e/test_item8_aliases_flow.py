"""E2E (Playwright, real server + real frontend): instrument aliases — owner 2026-09-30, item 8.

「登錄名稱＋中文別名」: the watchlist's shared instrument dialog carries a 別名 field.

* EDIT (real PUT, real server): typing 「大立光，Largan Precision、大立光」 saves the list the
  server normalized, the watchlist row re-renders 「亦稱 大立光、Largan Precision」, reopening
  the dialog shows it, the search box finds 3008 by its alias, and a second instrument claiming
  the same alias is refused with the server's zh sentence while the dialog stays open.
* ADD (the two provider seams stubbed with ``page.route`` — the flow server must not reach an
  exchange or an LLM; the register POST is captured): the lookup's exchange short name and a
  resolved AI reply's common names pre-fill the field, and 確認 sends them as ``aliases``.

ZERO page errors; the only console error allowed is the browser's line for the deliberate 422.
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
from playwright.sync_api import ConsoleMessage, Page, Route, expect
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
    """Two never-traded TW instruments registered under English names, as on the demo."""
    seed_accounts(conn)
    for symbol, name in (("3008", "LARGAN"), ("2603", "Evergreen")):
        upsert_instrument(conn, Instrument(symbol=symbol, market=Market.TW,
                                           quote_ccy=Currency.TWD,
                                           sector="Information Technology", name=name,
                                           board="TWSE"))
        upsert_prices(conn, [PriceRow(instrument=symbol, market=Market.TW,
                                      as_of=date(2026, 6, 9), close=Decimal("100"),
                                      source="test")],
                      fetched_at=datetime(2026, 6, 9, 15, 0, tzinfo=_TAIPEI))
    conn.commit()


def _collect_errors(page: Page) -> tuple[list[str], list[str]]:
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(getattr(m, "text", ""))
            if isinstance(m, ConsoleMessage) and m.type == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    return console_errors, page_errors


def _row(page: Page, symbol: str) -> Any:
    return page.locator("#inst-body tr").filter(
        has=page.locator(".sym-code", has_text=symbol))


@pytest.mark.e2e
def test_edit_dialog_saves_and_rerenders_aliases(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base_url = flow_server(_seed)
    page = fresh_page
    console_errors, page_errors = _collect_errors(page)

    page.goto(base_url + "/instruments.html", wait_until="load")
    page.wait_for_selector("#inst-body tr")
    expect(_row(page, "3008").locator(".sym-alias")).to_have_count(0)

    # (1) the field + its hint, empty for an instrument with no aliases yet.
    _row(page, "3008").get_by_role("button", name="編輯").click()
    modal = page.locator(".modal-backdrop").last
    alias_in = modal.locator("input.qa-aliases")
    expect(alias_in).to_have_value("")
    expect(modal.locator(".qa-alias-hint")).to_have_text(
        "卡片提到這檔標的時可用的其他名稱，例如 大立光、長榮")

    # (2) save: the text is split on ， and 、 and the server's normalized list comes back.
    alias_in.fill("大立光，Largan Precision、大立光")
    with page.expect_response(
        lambda r: r.url.endswith("/api/instruments/3008") and r.request.method == "PUT"
    ) as put_info:
        modal.get_by_role("button", name="儲存").click()
    put = put_info.value
    assert put.status == 200
    sent = put.request.post_data_json or {}
    assert sent["aliases"] == ["大立光", "Largan Precision", "大立光"]  # split on ， and 、
    assert put.json()["aliases"] == ["大立光", "Largan Precision"]
    expect(page.locator(".modal-backdrop")).to_have_count(0)

    # (3) the row re-renders with them, and reopening the dialog shows the stored list.
    expect(_row(page, "3008").locator(".sym-alias")).to_have_text("亦稱 大立光、Largan Precision")
    _row(page, "3008").get_by_role("button", name="編輯").click()
    modal = page.locator(".modal-backdrop").last
    expect(modal.locator("input.qa-aliases")).to_have_value("大立光、Largan Precision")
    modal.get_by_role("button", name="取消").click()
    expect(page.locator(".modal-backdrop")).to_have_count(0)

    # (4) the search box finds the row by its alias.
    page.fill("#inst-search", "大立光")
    expect(page.locator("#inst-body tr")).to_have_count(1)
    expect(_row(page, "3008")).to_have_count(1)
    page.fill("#inst-search", "")
    expect(_row(page, "2603")).to_have_count(1)

    # (5) another instrument may not claim the same name: 422, the server's sentence in the
    #     toast, the dialog stays open, nothing stored.
    _row(page, "2603").get_by_role("button", name="編輯").click()
    modal = page.locator(".modal-backdrop").last
    modal.locator("input.qa-aliases").fill("長榮、大立光")
    with page.expect_response(
        lambda r: r.url.endswith("/api/instruments/2603") and r.request.method == "PUT"
    ) as bad_info:
        modal.get_by_role("button", name="儲存").click()
    assert bad_info.value.status == 422
    expect(page.locator(".toast-fail .msg").last).to_contain_text(
        "別名「大立光」已屬於 3008（LARGAN）")
    expect(page.locator(".modal-backdrop")).to_have_count(1)
    modal.get_by_role("button", name="取消").click()
    expect(_row(page, "2603").locator(".sym-alias")).to_have_count(0)

    real_console = [e for e in console_errors
                    if not ("Failed to load resource" in e and "422" in e)]
    assert not real_console and not page_errors, (
        f"alias edit flow: console={real_console!r} page={page_errors!r}")


def _route_lookup(page: Page) -> None:
    """2454 → found with its exchange short name; MSFT → found; anything else → 查無報價."""

    def _handler(route: Route) -> None:
        sym = (parse_qs(urlparse(route.request.url).query).get("symbol") or [""])[0].upper()
        found: dict[str, dict[str, Any]] = {
            "2454": {"name": "MEDIATEK", "board": "TWSE", "aliases": ["聯發科"]},
            "MSFT": {"name": "Microsoft", "board": "TWSE", "aliases": []},
        }
        body: dict[str, Any] = {"found": False, "registered": False, "archived": False,
                                "name": "", "sector": "", "board": None, "is_etf": False,
                                "aliases": []}
        if sym in found:
            body.update(found=True, **found[sym])
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route("**/api/instruments/lookup**", _handler)


def _route_ai_resolve(page: Page) -> None:
    def _handler(route: Route) -> None:
        body = {"status": "resolved", "symbol": "MSFT", "name": "Microsoft",
                "aliases": ["微軟"], "sector": "Information Technology",
                "industry": "Software", "confidence": "high", "verified": True}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route("**/api/instruments/ai-resolve", _handler)


def _capture_register(page: Page, posts: list[dict[str, Any]]) -> None:
    """Capture POST /api/instruments (the register) and answer it; every other request to the
    path (the list GET after 確認) goes to the real server."""

    def _handler(route: Route) -> None:
        if route.request.method != "POST":
            route.continue_()
            return
        sent = route.request.post_data_json or {}
        posts.append(sent)
        route.fulfill(status=201, content_type="application/json", body=json.dumps(
            {"symbol": sent.get("symbol"), "name": sent.get("name"),
             "aliases": sent.get("aliases"), "restored": False}))

    page.route("**/api/instruments", _handler)


@pytest.mark.e2e
def test_add_dialog_carries_lookup_and_ai_aliases_into_register(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base_url = flow_server(_seed)
    page = fresh_page
    console_errors, page_errors = _collect_errors(page)
    posts: list[dict[str, Any]] = []
    _route_lookup(page)
    _route_ai_resolve(page)
    _capture_register(page, posts)

    page.goto(base_url + "/instruments.html", wait_until="load")
    page.wait_for_selector("#inst-body tr")

    # (A) the lookup's exchange short name pre-fills the field and rides into the register.
    page.fill("#new-symbol", "2454")
    page.click("#quick-add-btn")
    dialog = page.locator(".modal-backdrop").last
    expect(dialog.get_by_text("已找到")).to_be_visible()
    expect(dialog.locator("input.qa-aliases")).to_have_value("聯發科")
    dialog.get_by_role("button", name="確認", exact=True).click()
    expect(page.locator(".modal-backdrop")).to_have_count(0)
    assert posts[-1]["symbol"] == "2454" and posts[-1]["aliases"] == ["聯發科"]

    # (B) a resolved AI reply's common names pre-fill it too (the manual 「AI 辨識」: a
    #     ≤6-character code-like input does not auto-fire, L13).
    page.fill("#new-symbol", "MSFTX")
    page.click("#quick-add-btn")
    dialog = page.locator(".modal-backdrop").last
    expect(dialog.get_by_text("查無報價").first).to_be_visible()
    dialog.locator("button.qa-ai-resolve").click()
    expect(dialog.locator("input.qa-symbol")).to_have_value("MSFT")
    expect(dialog.get_by_text("已找到")).to_be_visible()
    expect(dialog.locator("input.qa-aliases")).to_have_value("微軟")
    dialog.get_by_role("button", name="確認", exact=True).click()
    expect(page.locator(".modal-backdrop")).to_have_count(0)
    assert posts[-1]["symbol"] == "MSFT" and posts[-1]["aliases"] == ["微軟"]

    assert not console_errors and not page_errors, (
        f"alias add flow: console={console_errors!r} page={page_errors!r}")
