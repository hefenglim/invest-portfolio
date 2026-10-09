"""E2E: 設定 › AI 提示詞 › 預覽 — the 代入標的 picker lists the REAL registry (owner 2026-09-30).

Item 14a: ``web/settings-prompts.js`` hard-coded the picker as
``['2330', '0056', '00919', 'AAPL', 'MSFT', 'NVDA', '1155.KL']`` — symbols most ledgers never
registered, and ``1155.KL`` is not even the registry's spelling (``1155``), so previewing it
rendered a symbol no task could ever substitute. The picker now lists what a per_symbol task
can substitute: the non-archived registry rows from ``GET /api/instruments`` (holdings first,
then the watchlist — the same set as a task's 「持倉＋觀察清單」 universe, DEF-059).

Three flows, each against a real server:

* the options ARE the server's list (derived from the same GET in the test, never restated),
  held first, archived out, and the preview posts the chosen symbol;
* an empty registry shows a zh empty state instead of a picker, and the preview still renders
  — for a per-symbol body (no symbol substituted) and for a portfolio body (no picker at all);
* a failed registry read shows ONE toast (the file's boot pattern) and an empty picker that
  says the read failed — not that nothing is registered — and the preview keeps working.
"""

import json
import sqlite3
from collections.abc import Iterator
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import Page, Route, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import set_instrument_archived, upsert_instrument
from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory

NOW = datetime(2026, 9, 30, 10, 0, tzinfo=ZoneInfo("Asia/Taipei"))
_EMPTY = "尚無可代入的標的"


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _strategies(conn: sqlite3.Connection) -> None:
    cs.create_strategy(conn, name="個股健檢", body="看 {{symbol_detail_json}}", now=NOW)
    cs.create_strategy(conn, name="組合總覽", body="看全局", now=NOW)


def _seed_registry(conn: sqlite3.Connection) -> None:
    """Golden (2330 + AAPL held) + one watch-only symbol + one ARCHIVED symbol."""
    _seed_golden(conn)
    upsert_instrument(conn, Instrument(symbol="0056", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Financials", name="元大高股息"))
    upsert_instrument(conn, Instrument(symbol="2884", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Financials", name="玉山金"))
    set_instrument_archived(conn, "2884", True)
    _strategies(conn)
    conn.commit()


def _seed_empty(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    _strategies(conn)
    conn.commit()


def _capture_previews(page: Page) -> list[dict[str, Any]]:
    """Record every preview body, then let the REAL server answer it (no LLM on this path)."""
    bodies: list[dict[str, Any]] = []

    def _handler(route: Route) -> None:
        bodies.append(json.loads(route.request.post_data or "{}"))
        route.continue_()

    page.route("**/api/prompts/preview", _handler)
    return bodies


def _open_preview(page: Page, base: str, strategy: str) -> Any:
    page.goto(f"{base}/settings.html#prompts", wait_until="load")
    card = page.locator(".tpl-card", has=page.locator(".tpl-name", has_text=strategy))
    card.wait_for()
    card.locator(".tpl-head").click()  # a card opens collapsed
    card.get_by_role("button", name="預覽提示詞").click()
    box = page.locator(".pv-box").last
    expect(box.locator(".pv-rendered")).to_be_visible()
    return box


def _console_errors(page: Page) -> list[str]:
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    return errors


@pytest.mark.e2e
def test_the_picker_lists_the_server_registry_and_previews_the_choice(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_registry)
    page = fresh_page
    errors = _console_errors(page)
    previews = _capture_previews(page)

    box = _open_preview(page, base, "個股健檢")
    options = box.locator(".pv-fields select option")
    got = [options.nth(i).get_attribute("value") for i in range(options.count())]

    registry = page.request.get(f"{base}/api/instruments").json()["list"]
    live = [r for r in registry if not r["archived"]]
    want = (sorted(r["symbol"] for r in live if r["held"])
            + sorted(r["symbol"] for r in live if not r["held"]))
    assert got == want, (got, want)
    assert got == ["2330", "AAPL", "0056"]            # held first, archived 2884 out
    assert "1155.KL" not in got and "NVDA" not in got  # the retired hard-coded list
    expect(box.locator(".pv-fields optgroup[label='持倉'] option")).to_have_count(2)
    expect(box.locator(".pv-fields optgroup[label='觀察清單'] option")).to_have_count(1)

    assert previews[0]["symbol"] == "2330" and previews[0]["scope"] == "per_symbol"
    box.locator(".pv-fields select").select_option("0056")
    for _ in range(50):  # the change re-posts the preview; wait for it to be captured
        if len(previews) >= 2:
            break
        page.wait_for_timeout(100)
    assert previews[-1]["symbol"] == "0056", previews
    expect(box.locator(".pv-rendered")).to_be_visible()
    assert not errors, errors


@pytest.mark.e2e
def test_an_empty_registry_shows_the_empty_state_and_the_preview_still_works(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_empty)
    page = fresh_page
    errors = _console_errors(page)
    previews = _capture_previews(page)

    box = _open_preview(page, base, "個股健檢")
    expect(box.get_by_text(_EMPTY)).to_be_visible()
    expect(box.locator(".pv-fields select")).to_have_count(0)
    assert previews[-1]["symbol"] is None and previews[-1]["scope"] == "per_symbol"
    box.locator(".sd-close").click()

    box = _open_preview(page, base, "組合總覽")         # a portfolio body: no picker at all
    expect(box.locator(".pv-fields")).to_have_count(0)
    expect(box.get_by_text(_EMPTY)).to_have_count(0)
    assert previews[-1]["scope"] == "portfolio" and previews[-1]["symbol"] is None
    assert not errors, errors


@pytest.mark.e2e
def test_a_failed_registry_read_is_one_toast_and_the_page_keeps_working(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_registry)
    page = fresh_page
    page.route("**/api/instruments", lambda route: route.fulfill(
        status=500, content_type="application/json",
        body=json.dumps({"error": {"code": "internal_error", "message": "暫時無法讀取"}})))

    box = _open_preview(page, base, "個股健檢")
    toasts = page.locator(".toast", has_text="標的清單載入失敗")
    expect(toasts).to_have_count(1)
    # the picker says the READ failed — never that the registry is empty, which it is not
    expect(box.locator(".pv-sym-empty")).to_contain_text("標的清單載入失敗")
    expect(box.get_by_text(_EMPTY)).to_have_count(0)
    expect(box.locator(".pv-fields select")).to_have_count(0)
    expect(box.locator(".pv-rendered")).to_be_visible()
