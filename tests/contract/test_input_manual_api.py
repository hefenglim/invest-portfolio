from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.api.instrument_service import QuickRegisterError, QuickRegisterOutcome
from portfolio_dash.api.routers import input_center
from portfolio_dash.shared.models.assets import Instrument


def test_manual_preview_buy_computes_fee_and_total(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-06-11", "shares": "1000", "price": "612.5"})
    assert r.status_code == 200
    b = r.json()
    assert b["fee"] == "873" and b["tax"] == "0"
    # Full source precision stays on the wire (canonical decimal_str, #2c/M1): 1000 * 612.5
    # is Decimal("612500.0") -- the trailing zero is preserved (the old _money_str
    # normalize() dropped it). The frontend quantizes for display.
    assert b["gross"] == "612500.0" and b["total"] == "-613373.0"
    assert b["fee_overridden"] is False and b["issues"] == []


def test_manual_preview_oversell_soft_issue(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "sell",
        "date": "2026-06-11", "shares": "5000", "price": "600"})
    b = r.json()
    codes = {i["code"]: i for i in b["issues"]}
    assert "sell_exceeds_holdings" in codes
    assert codes["sell_exceeds_holdings"]["sev"] == "warn"
    assert codes["sell_exceeds_holdings"]["field"] == "shares"


def test_manual_preview_fee_override(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-06-11", "shares": "1000", "price": "612.5",
        "fee_override": "500"})
    b = r.json()
    assert b["fee"] == "500" and b["fee_overridden"] is True


def test_manual_commit_writes(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/commit", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-06-11", "shares": "100", "price": "600"})
    assert r.status_code == 201
    b = r.json()
    assert isinstance(b["txn_id"], int) and b["total"].startswith("-")
    lg = api_client.get("/api/ledgers/transactions", params={"account_id": "tw_broker"}).json()
    assert lg["total_count"] == 2  # golden's 1 tw_broker txn + this one


def test_manual_commit_oversell_unacked_422(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/commit", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "sell",
        "date": "2026-06-11", "shares": "5000", "price": "600", "ack_oversell": False})
    assert r.status_code == 422 and r.json()["error"]["code"] == "oversell_unacknowledged"


def test_manual_commit_oversell_acked_writes(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/commit", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "sell",
        "date": "2026-06-11", "shares": "5000", "price": "600", "ack_oversell": True})
    assert r.status_code == 201


def test_manual_commit_hard_error_400(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/commit", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-06-11", "shares": "0", "price": "600"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "validation_error"


# --- unknown symbol: auto-register on commit (2026-07-02, round 2) ------------
# The data_ingestion hard block stands (a ledger row must always resolve to an
# Instrument), but the manual COMMIT path now resolves it ITSELF: it infers the
# market from the account's settlement ccy and runs the one-step quick_register
# (which requires a REAL quote — the typo guard). Preview shows an info-severity
# note (never gates the button); an unregistrable symbol still commits nothing.


def test_manual_preview_unregistered_symbol_is_info(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "GHOST", "side": "buy",
        "date": "2026-06-11", "shares": "100", "price": "10"})
    assert r.status_code == 200
    codes = {i["code"]: i for i in r.json()["issues"]}
    assert "symbol_auto_register" in codes
    assert codes["symbol_auto_register"]["sev"] == "info"  # notice, not a gate
    assert "自動查詢並註冊" in codes["symbol_auto_register"]["text"]


def _fake_quick_register_ok(symbol: str = "GHOST", name: str = "Ghost Corp") -> Any:
    def fake(conn: Any, **kw: Any) -> QuickRegisterOutcome:
        from portfolio_dash.data_ingestion.store import upsert_instrument
        from portfolio_dash.shared.enums import Currency, Market

        inst = Instrument(symbol=symbol, market=Market.TW, quote_ccy=Currency.TWD,
                          sector="", name=name, board="TWSE")
        upsert_instrument(conn, inst)
        return QuickRegisterOutcome(instrument=inst, board="TWSE", last=None,
                                    name_source="provider", history_points=True)
    return fake


def test_manual_commit_auto_registers_unknown_symbol(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(input_center, "quick_register", _fake_quick_register_ok())
    r = api_client.post("/api/input/manual/commit", json={
        "account_id": "tw_broker", "symbol": "GHOST", "side": "buy",
        "date": "2026-06-11", "shares": "100", "price": "10"})
    assert r.status_code == 201
    b = r.json()
    assert b["auto_registered"]["symbol"] == "GHOST"
    assert b["auto_registered"]["name"] == "Ghost Corp"
    # The trade itself landed in the ledger.
    lg = api_client.get("/api/ledgers/transactions", params={"account_id": "tw_broker"}).json()
    assert any(t["symbol"] == "GHOST" for t in lg["rows"])


def test_manual_commit_auto_register_fails_when_no_quote(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake(conn: Any, **kw: Any) -> QuickRegisterOutcome:
        raise QuickRegisterError("quote_not_found", "查無 GHOST 的報價", 422)

    monkeypatch.setattr(input_center, "quick_register", fake)
    r = api_client.post("/api/input/manual/commit", json={
        "account_id": "tw_broker", "symbol": "GHOST", "side": "buy",
        "date": "2026-06-11", "shares": "100", "price": "10"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "symbol_auto_register_failed"
    assert "查無" in r.json()["error"]["message"]
    # Nothing was written.
    lg = api_client.get("/api/ledgers/transactions", params={"account_id": "tw_broker"}).json()
    assert all(t["symbol"] != "GHOST" for t in lg["rows"])


def test_manual_commit_registered_symbol_skips_auto_register(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(conn: Any, **kw: Any) -> QuickRegisterOutcome:
        raise AssertionError("quick_register must not run for a registered symbol")

    monkeypatch.setattr(input_center, "quick_register", boom)
    r = api_client.post("/api/input/manual/commit", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-06-11", "shares": "100", "price": "600"})
    assert r.status_code == 201
    assert r.json()["auto_registered"] is None
