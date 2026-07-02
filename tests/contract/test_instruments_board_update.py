"""Contract: PUT /api/instruments/{symbol} with a board resolves board_status.

The 重新探測-and-save flow (2026-07-02): the frontend probes, then persists the
result via PUT. An unresolved TW board wires ``board: null``; after the PUT the
board must wire as the saved value (board_status resolved).
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.api import instrument_service
from portfolio_dash.pricing.results import RefreshSummary


def _register_unresolved_tw(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch, symbol: str
) -> None:
    def no_quotes(conn: Any, *a: Any, **kw: Any) -> RefreshSummary:
        kw_now = kw.get("now")
        assert kw_now is not None
        return RefreshSummary(ok={}, failed=[symbol], fetched_at=kw_now)

    monkeypatch.setattr(instrument_service, "refresh_quotes", no_quotes)
    monkeypatch.setattr(instrument_service, "refresh_history", no_quotes)
    monkeypatch.setattr(instrument_service, "lookup_name",
                        lambda sym, market, *, board=None: None)
    monkeypatch.setattr(instrument_service, "probe_tw_board", lambda s, **k: None)
    r = api_client.post("/api/instruments", json={"symbol": symbol, "market": "TW"})
    assert r.status_code == 201
    assert r.json()["board"] is None  # unresolved wires as null


def test_put_board_resolves_unresolved_tw(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register_unresolved_tw(api_client, monkeypatch, "5999")
    r = api_client.put("/api/instruments/5999", json={"board": "TPEx"})
    assert r.status_code == 200
    assert r.json()["board"] == "TPEx"  # no longer wired as null: status resolved
