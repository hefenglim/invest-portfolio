"""DEF-068 (E-11): the 更新報價 toast says what the refresh ACTUALLY did.

3be67db, every provider down: 「! 報價更新部分完成　9 檔持倉未更新：0056、2330、…。其餘已更新，
重新整理頁面即可看到新價。」 — 0 instruments had been updated, and the two lost FX pairs were
never named. The partial branch of ``web/shell.js`` read ``held_failed`` alone and ended on a
fixed 「其餘已更新」.

Why nothing caught it: M10-02 pinned the API's ``results`` block
(``tests/contract/test_m10_02_refresh_quotes_results.py``) but no test ever rendered the toast
— the sentence the owner reads was never asserted.

The browser's POST is fulfilled with a body produced by the REAL route (in-process
``TestClient`` against the golden DB, providers stubbed hermetically), not a hand-written
JSON: the toast is exercised against the shape the API actually serves. The live server
itself is never asked to refresh (its providers would reach the network).
"""

import json
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Page, Route, expect

from portfolio_dash.api.routers.actions import held_symbols
from portfolio_dash.pricing.registry import Registry
from portfolio_dash.pricing.results import RefreshSummary
from portfolio_dash.scheduler import jobs
from portfolio_dash.scheduler.jobs import register_held_symbols_fn

pytestmark = pytest.mark.e2e


@pytest.fixture
def _held_seam() -> Iterator[None]:
    register_held_symbols_fn(held_symbols)
    yield
    register_held_symbols_fn(None)


def _all_fail_body(api_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(
        jobs, "default_registry", lambda conn=None: Registry(providers={}, order={})
    )
    body: dict[str, Any] = api_client.post("/api/actions/refresh-quotes", json={}).json()
    return body


def _partial_body(api_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    def refresh(conn: Any, registry: Any, instruments: list[Any], fx_pairs: list[Any],
                **kw: Any) -> RefreshSummary:
        ok = {r.symbol: "twse" for r in instruments if r.symbol == "2330"}
        ok["USDTWD"] = "yfinance"
        return RefreshSummary(ok=ok, failed=["AAPL", "USDMYR"], fetched_at=kw["now"])

    monkeypatch.setattr(jobs, "refresh_quotes", refresh)
    body: dict[str, Any] = api_client.post("/api/actions/refresh-quotes", json={}).json()
    return body


def _click_refresh(page: Page, live_server: str, body: dict[str, Any]) -> None:
    def fulfil(route: Route) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route("**/api/actions/refresh-quotes", fulfil)
    page.goto(f"{live_server}/index.html")
    page.locator("button.btn-refresh[title='更新報價或重建統計']").click()
    page.locator(".refresh-opt", has_text="更新報價").click()


def test_every_provider_down_reads_as_a_failure_naming_the_fx_pairs(
    fresh_page: Page, live_server: str, api_client: TestClient, golden_db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch, _held_seam: None,
) -> None:
    body = _all_fail_body(api_client, monkeypatch)
    assert body["summary"]["all_failed"] is True  # the real route said so — not our fixture
    _click_refresh(fresh_page, live_server, body)
    toast = fresh_page.locator(".toast.toast-fail")
    expect(toast).to_contain_text("報價更新失敗")
    expect(toast).to_contain_text("2 檔報價都沒有更新：2330、AAPL")
    expect(toast).to_contain_text("匯率未更新：USDMYR、USDTWD")
    expect(fresh_page.locator(".toast", has_text="其餘已更新")).to_have_count(0)


def test_a_partial_refresh_counts_both_sides(
    fresh_page: Page, live_server: str, api_client: TestClient, golden_db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch, _held_seam: None,
) -> None:
    body = _partial_body(api_client, monkeypatch)
    # AAPL is the only US instrument: that market lost everything → ``error`` WITH counts
    # (DEF-067 ①); overall one of two instruments updated, so the toast is the partial one.
    assert body["results"]["quotes_us"]["status"] == "error"
    _click_refresh(fresh_page, live_server, body)
    toast = fresh_page.locator(".toast.toast-warn")
    expect(toast).to_contain_text("報價更新部分完成")
    expect(toast).to_contain_text("1 檔已更新、1 檔未更新：AAPL；匯率未更新：USDMYR")
    expect(fresh_page.locator(".toast", has_text="其餘已更新")).to_have_count(0)


def _one_market_lost_body(api_client: TestClient, monkeypatch: pytest.MonkeyPatch
                          ) -> dict[str, Any]:
    def refresh(conn: Any, registry: Any, instruments: list[Any], fx_pairs: list[Any],
                **kw: Any) -> RefreshSummary:
        ok = {r.symbol: "yfinance" for r in instruments if r.symbol == "AAPL"}
        ok["USDTWD"] = ok["USDMYR"] = "yfinance"
        return RefreshSummary(ok=ok, failed=[r.symbol for r in instruments if r.symbol not in ok],
                              fetched_at=kw["now"])

    monkeypatch.setattr(jobs, "refresh_quotes", refresh)
    body: dict[str, Any] = api_client.post("/api/actions/refresh-quotes", json={}).json()
    return body


def test_one_market_losing_everything_reads_as_partial_not_as_a_crash(
    fresh_page: Page, live_server: str, api_client: TestClient, golden_db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch, _held_seam: None,
) -> None:
    """DEF-067 ① (owner ruling 2026-09-26): quotes_tw lost its only instrument → ``error``
    WITH counts. Overall one of two instruments updated, so the toast is the partial one —
    not the crash branch that prints 「quotes_tw：…」."""
    body = _one_market_lost_body(api_client, monkeypatch)
    assert body["results"]["quotes_tw"]["status"] == "error"
    assert body["summary"]["all_failed"] is False
    _click_refresh(fresh_page, live_server, body)
    toast = fresh_page.locator(".toast.toast-warn")
    expect(toast).to_contain_text("報價更新部分完成")
    expect(toast).to_contain_text("1 檔已更新、1 檔未更新：2330")
    expect(fresh_page.locator(".toast", has_text="quotes_tw")).to_have_count(0)
