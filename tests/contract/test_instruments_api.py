from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.api import instrument_service
from portfolio_dash.api.routers import instruments as instruments_router
from portfolio_dash.pricing.results import PriceRow, RefreshSummary
from portfolio_dash.pricing.store import upsert_prices

_NOW = datetime(2026, 7, 2, 12, 0, tzinfo=UTC)


def _stub_pricing(
    monkeypatch: pytest.MonkeyPatch,
    *,
    quote_ok: bool = True,
    price: str = "185.50",
    name: str | None = None,
    record: list[str] | None = None,
) -> None:
    """Stub the instrument_service provider seams (hermetic: no sockets).

    A successful stub ALSO upserts a real price row so the response element's
    ``last`` reflects what the UI would see after a live fetch.
    """

    def fake_quotes(conn: Any, registry: Any, instruments: list[Any],
                    fx_pairs: Any, *, now: datetime) -> RefreshSummary:
        syms = [r.symbol for r in instruments]
        if record is not None:
            record.extend(syms)
        if not quote_ok:
            return RefreshSummary(ok={}, failed=syms, fetched_at=now)
        rows = [PriceRow(instrument=r.symbol, market=r.market, as_of=now.date(),
                         close=Decimal(price), source="stub") for r in instruments]
        upsert_prices(conn, rows, fetched_at=now)
        return RefreshSummary(ok={s: "stub" for s in syms}, failed=[], fetched_at=now)

    def fake_history(conn: Any, registry: Any, instruments: list[Any],
                     start: date, *, now: datetime) -> RefreshSummary:
        return RefreshSummary(ok={r.symbol: "stub" for r in instruments}, failed=[],
                              fetched_at=now)

    monkeypatch.setattr(instrument_service, "refresh_quotes", fake_quotes)
    monkeypatch.setattr(instrument_service, "refresh_history", fake_history)
    monkeypatch.setattr(instrument_service, "lookup_name",
                        lambda sym, market, *, board=None: name)
    monkeypatch.setattr(instrument_service, "probe_tw_board", lambda s, **k: "TWSE")


def test_instruments_list_shape_and_enrichment(api_client: TestClient) -> None:
    r = api_client.get("/api/instruments")
    assert r.status_code == 200
    body = r.json()
    assert "as_of" in body
    by_symbol = {i["symbol"]: i for i in body["list"]}
    tsmc = by_symbol["2330"]
    assert tsmc["name"] == "TSMC" and tsmc["market"] == "TW" and tsmc["board"] == "TWSE"
    assert tsmc["ccy"] == "TWD" and tsmc["held"] is True
    assert tsmc["last"] == "600"
    assert tsmc["target_low"] is None
    aapl = by_symbol["AAPL"]
    assert aapl["board"] == "" and aapl["held"] is True and aapl["last"] == "120"


def test_probe_returns_board(api_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(instruments_router, "probe_tw_board", lambda s, **k: "TPEx")
    r = api_client.post("/api/instruments/probe", json={"symbol": "6488"})
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "6488" and body["board"] == "TPEx"
    assert body["board_label"] == "TPEx 上櫃"


def test_probe_unresolved(api_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(instruments_router, "probe_tw_board", lambda s, **k: None)
    r = api_client.post("/api/instruments/probe", json={"symbol": "9999"})
    assert r.status_code == 200
    assert r.json()["board"] is None and r.json()["board_label"] == "未解析"


def test_probe_blank_symbol_400(api_client: TestClient) -> None:
    r = api_client.post("/api/instruments/probe", json={"symbol": "  "})
    assert r.status_code == 400


def test_register_new_instrument(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pricing(monkeypatch)
    r = api_client.post("/api/instruments", json={
        "symbol": "6488", "market": "TW", "name": "環球晶", "sector": "Semis",
        "board": "TPEx", "quote_ccy": "TWD", "target_low": "450"})
    assert r.status_code == 201
    body = r.json()
    assert body["symbol"] == "6488" and body["board"] == "TPEx" and body["target_low"] == "450"


def test_register_triggers_initial_quote_fetch(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Registration immediately fetches the symbol's quote (2026-07-02) so the user
    is not price-less until the market's next post-close cron."""
    calls: list[str] = []
    _stub_pricing(monkeypatch, record=calls, price="245.90")
    r = api_client.post("/api/instruments", json={
        "symbol": "TSLA", "market": "US", "name": "Tesla"})
    assert r.status_code == 201
    assert calls == ["TSLA"]
    assert r.json()["last"] == "245.90"


def test_register_survives_quote_fetch_failure(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The classic register path is best-effort (force=True): a provider failure
    NEVER fails an explicit registration — it just leaves the price missing."""
    def _boom(conn: Any, *a: Any, **kw: Any) -> RefreshSummary:
        raise RuntimeError("provider down")

    monkeypatch.setattr(instrument_service, "refresh_quotes", _boom)
    monkeypatch.setattr(instrument_service, "refresh_history", _boom)
    monkeypatch.setattr(instrument_service, "lookup_name",
                        lambda sym, market, *, board=None: None)
    r = api_client.post("/api/instruments", json={
        "symbol": "TSLA", "market": "US", "name": "Tesla"})
    assert r.status_code == 201
    assert r.json()["symbol"] == "TSLA" and r.json()["last"] is None


def test_quick_add_one_step(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /instruments/quick: probe + real quote + auto name + history, one call."""
    _stub_pricing(monkeypatch, price="185.50", name="長榮")
    r = api_client.post("/api/instruments/quick", json={"symbol": "2603", "market": "TW"})
    assert r.status_code == 201
    body = r.json()
    assert body["symbol"] == "2603" and body["name"] == "長榮"
    assert body["board"] == "TWSE" and body["board_label"] == "TWSE 上市"
    assert body["last"] == "185.50"
    assert body["name_source"] == "provider" and body["history_backfilled"] is True


def test_quick_add_no_quote_422_then_force(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No source supplies a quote -> 422 quote_not_found (typo guard); an explicit
    force=true registers anyway (price-less, shown as 缺價)."""
    _stub_pricing(monkeypatch, quote_ok=False)
    r = api_client.post("/api/instruments/quick", json={"symbol": "FAKE9", "market": "US"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "quote_not_found"
    # nothing was registered by the refused attempt
    listed = {i["symbol"] for i in api_client.get("/api/instruments").json()["list"]}
    assert "FAKE9" not in listed

    r2 = api_client.post("/api/instruments/quick",
                         json={"symbol": "FAKE9", "market": "US", "force": True})
    assert r2.status_code == 201 and r2.json()["last"] is None


def test_quick_add_duplicate_409(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pricing(monkeypatch)
    r = api_client.post("/api/instruments/quick", json={"symbol": "2330", "market": "TW"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "duplicate_symbol"


def test_quick_add_uppercases_symbol(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pricing(monkeypatch, price="245.90", name="Tesla")
    r = api_client.post("/api/instruments/quick", json={"symbol": " tsla ", "market": "US"})
    assert r.status_code == 201 and r.json()["symbol"] == "TSLA"


def test_quick_add_market_enum_and_ccy(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pricing(monkeypatch, price="1.230", name="MYEG")
    r = api_client.post("/api/instruments/quick", json={"symbol": "0138", "market": "MY"})
    assert r.status_code == 201
    body = r.json()
    assert body["ccy"] == "MYR" and body["board"] == ".KL"
    assert body["board_label"] == "馬股 .KL"


def test_register_duplicate_409(api_client: TestClient) -> None:
    r = api_client.post("/api/instruments", json={"symbol": "2330", "market": "TW",
                                                  "name": "x", "sector": "y", "board": "TWSE"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "duplicate_symbol"


def test_register_us_with_tw_board_400(api_client: TestClient) -> None:
    r = api_client.post("/api/instruments", json={"symbol": "TSLA", "market": "US",
                                                  "name": "Tesla", "board": "TWSE"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "validation_error"


def test_put_updates_target_low(api_client: TestClient) -> None:
    r = api_client.put("/api/instruments/2330", json={"target_low": "550"})
    assert r.status_code == 200 and r.json()["target_low"] == "550"


def test_put_missing_404(api_client: TestClient) -> None:
    r = api_client.put("/api/instruments/NOPE", json={"sector": "z"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_put_explicit_null_clears_target_low(api_client: TestClient) -> None:
    """Regression (2026-07-03): PUT {"target_low": null} must CLEAR the alert —
    the old exclude_none dump silently dropped explicit nulls (clear never worked)."""
    r = api_client.put("/api/instruments/2330", json={"target_low": "550"})
    assert r.status_code == 200 and r.json()["target_low"] == "550"
    r2 = api_client.put("/api/instruments/2330", json={"target_low": None})
    assert r2.status_code == 200 and r2.json()["target_low"] is None


def test_list_and_update_carry_is_etf(api_client: TestClient) -> None:
    lst = api_client.get("/api/instruments").json()["list"]
    assert all(isinstance(i["is_etf"], bool) for i in lst)
    r = api_client.put("/api/instruments/2330", json={"is_etf": True})
    assert r.status_code == 200 and r.json()["is_etf"] is True
