"""Tests for FinMind tier/quota guards + Bearer auth (spec 20.15.1/4).

All HTTP is monkeypatched on ``requests.get`` (the repo bans sockets). Covers:
- Bearer ``Authorization`` header (token still resolved from the DB).
- ``end_date`` forwarding.
- HTTP 402 OR JSON ``status==402`` -> ``FinMindQuotaError`` carrying the FinMind msg.
- ``DATASET_TIER`` map (all 5 datasets = "free") + local tier preflight that raises
  ``FinMindTierError`` WITHOUT a network call when the token tier is below the dataset's.
- ``fetch_quota`` parsing ``user_count``/``api_request_limit`` from ``user_info``.
"""

import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest

from portfolio_dash.pricing import datasources_store
from portfolio_dash.pricing import finmind_datasets as F

_QUOTA_BODY = {
    "msg": "Requests reach the upper limit. https://finmindtrade.com/",
    "status": 402,
}


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
    datasources_store.seed(c)
    yield c
    c.close()


# --- DATASET_TIER map ---------------------------------------------------------


def test_dataset_tier_all_free() -> None:
    assert set(F.DATASET_TIER) == set(F.FINMIND_DATASETS)
    assert all(t == "free" for t in F.DATASET_TIER.values())


# --- Bearer header + end_date -------------------------------------------------


def test_fetch_dataset_uses_bearer_and_forwards_end_date(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    datasources_store.set_api_key(conn, "finmind", "tok-xyz")
    captured: dict[str, Any] = {}

    def _fake_get(
        url: str, *, params: dict[str, Any], headers: dict[str, str], timeout: int
    ) -> _FakeResp:
        captured["params"] = params
        captured["headers"] = headers
        return _FakeResp({"msg": "success", "status": 200, "data": [{"x": 1}]})

    monkeypatch.setattr(F.requests, "get", _fake_get)
    data = F.fetch_dataset(
        conn, dataset="valuation", data_id="2330",
        start_date="2026-05-01", end_date="2026-06-01",
    )
    assert data == [{"x": 1}]
    assert captured["headers"]["Authorization"] == "Bearer tok-xyz"
    assert captured["params"]["end_date"] == "2026-06-01"
    assert "token" not in captured["params"]


def test_fetch_dataset_omits_end_date_when_absent(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    datasources_store.set_api_key(conn, "finmind", "tok-xyz")
    captured: dict[str, Any] = {}

    def _fake_get(url: str, *, params: dict[str, Any], headers: dict[str, str],
                  timeout: int) -> _FakeResp:
        captured["params"] = params
        return _FakeResp({"msg": "success", "status": 200, "data": []})

    monkeypatch.setattr(F.requests, "get", _fake_get)
    F.fetch_dataset(conn, dataset="margin", data_id="2330", start_date="2026-05-01")
    assert "end_date" not in captured["params"]


# --- Quota errors -------------------------------------------------------------


def test_http_402_raises_quota_error(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    datasources_store.set_api_key(conn, "finmind", "tok-xyz")
    monkeypatch.setattr(
        F.requests, "get",
        lambda url, *, params, headers, timeout: _FakeResp(_QUOTA_BODY, status=402),
    )
    with pytest.raises(F.FinMindQuotaError) as ei:
        F.fetch_dataset(conn, dataset="institutional", data_id="2330", start_date="2026-05-01")
    assert "upper limit" in str(ei.value)


def test_json_status_402_raises_quota_error(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # HTTP 200 but a quota body (FinMind sometimes returns 200 + status:402 in JSON).
    datasources_store.set_api_key(conn, "finmind", "tok-xyz")
    monkeypatch.setattr(
        F.requests, "get",
        lambda url, *, params, headers, timeout: _FakeResp(_QUOTA_BODY, status=200),
    )
    with pytest.raises(F.FinMindQuotaError) as ei:
        F.fetch_dataset(conn, dataset="margin", data_id="2330", start_date="2026-05-01")
    assert "upper limit" in str(ei.value)


# --- Tier preflight (no network) ----------------------------------------------


def test_tier_preflight_blocks_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dataset requiring a higher tier than the token's raises before any network call."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    datasources_store.create_tables(c)
    datasources_store.seed(c)
    datasources_store.set_api_key(c, "finmind", "tok-xyz")
    # Force a dataset to require "backer" so a free/unset token fails the preflight.
    monkeypatch.setitem(F.DATASET_TIER, "valuation", "backer")

    def _no_call(*a: Any, **k: Any) -> _FakeResp:  # pragma: no cover - must not run
        raise AssertionError("requests.get must not be called when the tier is too low")

    monkeypatch.setattr(F.requests, "get", _no_call)
    with pytest.raises(F.FinMindTierError) as ei:
        F.fetch_dataset(c, dataset="valuation", data_id="2330", start_date="2026-05-01")
    assert ei.value.required_tier == "backer"
    c.close()


def test_tier_preflight_passes_when_token_tier_sufficient(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    datasources_store.create_tables(c)
    datasources_store.seed(c)
    datasources_store.set_api_key(c, "finmind", "tok-xyz")
    datasources_store.set_tier(c, "finmind", "backer")
    monkeypatch.setitem(F.DATASET_TIER, "valuation", "backer")
    monkeypatch.setattr(
        F.requests, "get",
        lambda url, *, params, headers, timeout: _FakeResp(
            {"msg": "success", "status": 200, "data": [{"ok": 1}]}
        ),
    )
    assert F.fetch_dataset(
        c, dataset="valuation", data_id="2330", start_date="2026-05-01"
    ) == [{"ok": 1}]
    c.close()


def test_free_dataset_passes_with_unset_tier(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-regression: a free dataset + an unset (None) token tier still fetches."""
    datasources_store.set_api_key(conn, "finmind", "tok-xyz")
    monkeypatch.setattr(
        F.requests, "get",
        lambda url, *, params, headers, timeout: _FakeResp(
            {"msg": "success", "status": 200, "data": [{"ok": 1}]}
        ),
    )
    assert F.fetch_dataset(
        conn, dataset="institutional", data_id="2330", start_date="2026-05-01"
    ) == [{"ok": 1}]


# --- fetch_quota --------------------------------------------------------------


def test_fetch_quota_parses_user_info(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    datasources_store.set_api_key(conn, "finmind", "tok-xyz")
    captured: dict[str, Any] = {}

    def _fake_get(url: str, *, headers: dict[str, str], timeout: int) -> _FakeResp:
        captured["url"] = url
        captured["headers"] = headers
        return _FakeResp({"user_count": 123, "api_request_limit": 600})

    monkeypatch.setattr(F.requests, "get", _fake_get)
    quota = F.fetch_quota(conn)
    assert quota == {"user_count": 123, "api_request_limit": 600}
    assert captured["url"] == "https://api.web.finmindtrade.com/v2/user_info"
    assert captured["headers"]["Authorization"] == "Bearer tok-xyz"


def test_fetch_quota_no_token_raises(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _no_call(*a: Any, **k: Any) -> _FakeResp:  # pragma: no cover - must not run
        raise AssertionError("requests.get must not be called without a token")

    monkeypatch.setattr(F.requests, "get", _no_call)
    with pytest.raises(F.MissingTokenError):
        F.fetch_quota(conn)
