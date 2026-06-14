"""Tests for the FinMind Free-tier multi-dataset client (spec 20.6).

Hermetic: ``requests.get`` is monkeypatched to return a recorded FinMind envelope
(``{"msg": "success", "status": 200, "data": [...]}``); the DB-backed token is
seeded via ``datasources_store``. No network is touched.
"""

import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest

from portfolio_dash.pricing import datasources_store
from portfolio_dash.pricing import finmind_datasets as F


class _FakeResp:
    def __init__(self, payload: dict[str, Any], *, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict[str, Any]:
        return self._payload


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    datasources_store.create_tables(c)
    yield c
    c.close()


def test_dataset_id_mapping_complete() -> None:
    assert F.FINMIND_DATASETS["institutional"] == "TaiwanStockInstitutionalInvestorsBuySell"
    assert F.FINMIND_DATASETS["margin"] == "TaiwanStockMarginPurchaseShortSale"
    assert F.FINMIND_DATASETS["valuation"] == "TaiwanStockPER"
    assert F.FINMIND_DATASETS["monthly_revenue"] == "TaiwanStockMonthRevenue"
    assert F.FINMIND_DATASETS["financials"] == "TaiwanStockFinancialStatements"


def test_fetch_dataset_returns_data_and_passes_params(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    datasources_store.set_api_key(conn, "finmind", "tok-123")
    captured: dict[str, Any] = {}

    def _fake_get(
        url: str, *, params: dict[str, Any], headers: dict[str, str], timeout: int
    ) -> _FakeResp:
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        captured["timeout"] = timeout
        return _FakeResp(
            {"msg": "success", "status": 200, "data": [{"date": "2026-06-11", "buy": 5}]}
        )

    monkeypatch.setattr(F.requests, "get", _fake_get)
    data = F.fetch_dataset(
        conn, dataset="institutional", data_id="2330", start_date="2026-05-01"
    )
    assert data == [{"date": "2026-06-11", "buy": 5}]
    assert captured["params"]["dataset"] == "TaiwanStockInstitutionalInvestorsBuySell"
    assert captured["params"]["data_id"] == "2330"
    assert captured["params"]["start_date"] == "2026-05-01"
    # Bearer auth header (spec 20.15.1) — token is no longer a query param.
    assert captured["headers"]["Authorization"] == "Bearer tok-123"
    assert "token" not in captured["params"]


def test_fetch_dataset_empty_data_is_empty_list(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    datasources_store.set_api_key(conn, "finmind", "tok-123")
    monkeypatch.setattr(
        F.requests, "get",
        lambda url, *, params, headers, timeout: _FakeResp({"msg": "success", "status": 200}),
    )
    assert F.fetch_dataset(conn, dataset="margin", data_id="2330", start_date="2026-05-01") == []


def test_fetch_dataset_no_token_raises(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No key seeded for finmind -> MissingTokenError before any network call.
    def _no_call(*a: Any, **k: Any) -> _FakeResp:  # pragma: no cover - must not run
        raise AssertionError("requests.get must not be called without a token")

    monkeypatch.setattr(F.requests, "get", _no_call)
    with pytest.raises(F.MissingTokenError):
        F.fetch_dataset(conn, dataset="valuation", data_id="2330", start_date="2026-05-01")


def test_fetch_dataset_unknown_dataset_raises(conn: sqlite3.Connection) -> None:
    datasources_store.set_api_key(conn, "finmind", "tok-123")
    with pytest.raises(KeyError):
        F.fetch_dataset(conn, dataset="nope", data_id="2330", start_date="2026-05-01")


def test_fetch_dataset_http_error_propagates(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    datasources_store.set_api_key(conn, "finmind", "tok-123")
    monkeypatch.setattr(
        F.requests, "get",
        lambda url, *, params, headers, timeout: _FakeResp({"msg": "fail"}, status=500),
    )
    with pytest.raises(RuntimeError):
        F.fetch_dataset(conn, dataset="financials", data_id="2330", start_date="2026-05-01")
