"""Contract: instrument ``industry`` (GICS 產業細分) register + update read-modify-write (R6).

The registration-extras helper and the PUT update path must persist ``industry`` WITHOUT
clobbering any sibling field, and vice versa: a PUT that omits industry preserves it; a PUT of a
sibling field never drops it. A BLANK industry at REGISTRATION never writes over a stored value;
an explicit null at UPDATE clears it. (The frontend forms submit the same fields.)
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.api import instrument_service
from portfolio_dash.pricing.results import RefreshSummary


def _mock_offline(monkeypatch: pytest.MonkeyPatch, symbol: str) -> None:
    """Force-register offline: the network seams degrade to 'no quote' (register uses force)."""
    def no_quotes(conn: Any, *a: Any, **kw: Any) -> RefreshSummary:
        now = kw.get("now")
        assert now is not None
        return RefreshSummary(ok={}, failed=[symbol], fetched_at=now)
    monkeypatch.setattr(instrument_service, "refresh_quotes", no_quotes)
    monkeypatch.setattr(instrument_service, "refresh_history", no_quotes)
    monkeypatch.setattr(instrument_service, "lookup_name",
                        lambda sym, market, *, board=None: None)
    monkeypatch.setattr(instrument_service, "probe_tw_board", lambda s, **k: None)


def _get(api_client: TestClient, symbol: str) -> dict[str, Any]:
    items = api_client.get("/api/instruments").json()["list"]
    return next(i for i in items if i["symbol"] == symbol)


def test_register_persists_industry(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_offline(monkeypatch, "AAAA")
    r = api_client.post("/api/instruments", json={
        "symbol": "AAAA", "market": "US", "name": "Alpha",
        "sector": "Information Technology", "industry": "Semiconductors"})
    assert r.status_code == 201
    assert r.json()["industry"] == "Semiconductors"
    assert _get(api_client, "AAAA")["industry"] == "Semiconductors"


def test_register_blank_industry_is_noop(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_offline(monkeypatch, "BBBB")
    r = api_client.post("/api/instruments", json={
        "symbol": "BBBB", "market": "US", "name": "Beta", "industry": ""})
    assert r.status_code == 201
    assert r.json()["industry"] is None  # blank ⇒ not written (stays null)


def test_update_industry_read_modify_write_no_clobber(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_offline(monkeypatch, "CCCC")
    api_client.post("/api/instruments", json={
        "symbol": "CCCC", "market": "US", "name": "Gamma", "sector": "Financials"})
    # (1) set industry via PUT; name + sector must survive.
    r = api_client.put("/api/instruments/CCCC", json={"industry": "Banks"})
    assert r.status_code == 200
    body = r.json()
    assert body["industry"] == "Banks" and body["name"] == "Gamma"
    assert body["sector"] == "Financials"
    # (2) a PUT of a SIBLING field (target_low) that omits industry PRESERVES it.
    r2 = api_client.put("/api/instruments/CCCC", json={"target_low": "10"})
    assert r2.json()["industry"] == "Banks"  # not clobbered by the sibling update
    # (3) an explicit null clears it.
    r3 = api_client.put("/api/instruments/CCCC", json={"industry": None})
    assert r3.json()["industry"] is None
