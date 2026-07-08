"""Contract tests for the chips/sentiment external variables going live (spec 20.2).

Uses the shared golden_db + api_client (the golden DB creates external_snapshots but
leaves it EMPTY, so every external var degrades to ``{"unavailable": true}``). Then
seeds snapshots to assert the router reads them, derives via portfolio.external_signals,
and feeds the value into the rendered prompt. No LLM is called (preview path).
"""

import json
import sqlite3
from datetime import date, datetime
from typing import Any

from fastapi.testclient import TestClient

from portfolio_dash.api.deps import get_conn
from portfolio_dash.pricing import datasources_store, snapshots_store


def _conn_of(client: TestClient) -> sqlite3.Connection:
    return client.app.dependency_overrides[get_conn]()  # type: ignore[attr-defined,no-any-return]


def _preview(client: TestClient, body: str, *, scope: str = "portfolio",
             symbol: str | None = None) -> dict[str, Any]:
    r = client.post(
        "/api/prompts/preview", json={"body": body, "scope": scope, "symbol": symbol}
    )
    assert r.status_code == 200
    return r.json()  # type: ignore[no-any-return]


# --- (a) prompt-vars: 7 external vars + 3 date vars available (27 total) -------


def test_prompt_vars_external_now_available(api_client: TestClient) -> None:
    rows = api_client.get("/api/prompt-vars").json()
    by_token = {r["token"]: r for r in rows}
    for token in (
        "institutional_json", "margin_json", "monthly_revenue_json", "valuation_json",
        "financials_json", "market_sentiment_json", "index_quotes_json",
    ):
        assert by_token[token]["available"] is True, token
    # 29 previously live + 1 batch-④ news (symbol_news_json) = 30.
    assert sum(1 for r in rows if r["available"]) == 30
    assert by_token["technical_signals_json"]["available"] is True
    assert by_token["fear_greed_json"]["available"] is True
    assert by_token["symbol_news_json"]["available"] is True
    # The spec-04 'ai' vars stay unavailable this round.
    assert by_token["backtest_json"]["available"] is False
    assert by_token["calibration_gap_json"]["available"] is False


# --- (a2) prompt-vars carry tier metadata (spec 20.15.3) ----------------------


def test_prompt_vars_carry_tier_fields_free_token(api_client: TestClient) -> None:
    by_token = {r["token"]: r for r in api_client.get("/api/prompt-vars").json()}
    # 5 FinMind chips vars require the "free" tier; with an unset/free finmind tier
    # they are tier_ok and carry no tier_label.
    for token in (
        "institutional_json", "margin_json", "monthly_revenue_json", "valuation_json",
        "financials_json",
    ):
        v = by_token[token]
        assert v["required_tier"] == "free", token
        assert v["tier_ok"] is True, token
        assert v["tier_label"] is None, token
    # sentiment/index have no tier requirement -> always ok, no label.
    assert by_token["market_sentiment_json"]["required_tier"] is None
    assert by_token["market_sentiment_json"]["tier_ok"] is True
    assert by_token["index_quotes_json"]["required_tier"] is None
    # non-external vars also carry the fields (null requirement, ok).
    assert by_token["holdings_json"]["required_tier"] is None
    assert by_token["holdings_json"]["tier_ok"] is True


def test_prompt_vars_tier_not_ok_when_token_below_required(api_client: TestClient) -> None:
    conn = _conn_of(api_client)
    # Raise the institutional dataset's requirement to backer; mark finmind tier free.
    from portfolio_dash.pricing import finmind_datasets

    finmind_datasets.DATASET_TIER["institutional"] = "backer"
    datasources_store.set_tier(conn, "finmind", "free")
    try:
        by_token = {r["token"]: r for r in api_client.get("/api/prompt-vars").json()}
        v = by_token["institutional_json"]
        assert v["required_tier"] == "backer"
        assert v["tier_ok"] is False
        assert v["tier_label"] and "Backer" in v["tier_label"]
    finally:
        finmind_datasets.DATASET_TIER["institutional"] = "free"


# --- (b) empty golden_db -> external vars degrade -----------------------------


def test_institutional_degrades_with_empty_snapshots(api_client: TestClient) -> None:
    out = _preview(api_client, "{{institutional_json}}", scope="per_symbol", symbol="2330")
    assert json.loads(out["rendered"]) == {"unavailable": True}


def test_market_sentiment_degrades_with_empty_snapshots(api_client: TestClient) -> None:
    out = _preview(api_client, "{{market_sentiment_json}}")
    assert json.loads(out["rendered"]) == {"unavailable": True}


def test_index_quotes_degrades_with_empty_snapshots(api_client: TestClient) -> None:
    out = _preview(api_client, "{{index_quotes_json}}")
    assert json.loads(out["rendered"]) == {"unavailable": True}


# --- (c) seeded chips snapshot -> derived value rendered ----------------------


def test_institutional_renders_derived_from_snapshot(api_client: TestClient) -> None:
    conn = _conn_of(api_client)
    # 3 trailing days of net foreign buying (buy > sell each day) -> consecutive=3.
    rows = [
        {"date": "2026-06-09", "name": "Foreign_Investor", "buy": 100, "sell": 200},
        {"date": "2026-06-10", "name": "Foreign_Investor", "buy": 300, "sell": 100},
        {"date": "2026-06-11", "name": "Foreign_Investor", "buy": 400, "sell": 100},
    ]
    snapshots_store.add_snapshot(
        conn, source="finmind", dataset="institutional", symbol="2330",
        as_of=date(2026, 6, 11), payload={"rows": rows},
        fetched_at=datetime(2026, 6, 11, 18, 0),
    )
    out = _preview(api_client, "{{institutional_json}}", scope="per_symbol", symbol="2330")
    value = json.loads(out["rendered"])
    assert value.get("unavailable") is not True
    assert "consecutive_buy_days" in value
    # last two days net-positive; 3rd-from-newest is negative and breaks the run.
    assert value["consecutive_buy_days"] == 2
    assert value["last_as_of"] == "2026-06-11"


def test_chips_var_degrade_carries_health_reason(api_client: TestClient) -> None:
    """No snapshot + a finmind health error -> degrade payload includes the reason
    (fed by the router from data_source_health; llm_insight never reads health)."""
    conn = _conn_of(api_client)
    datasources_store.upsert_health(
        conn, "finmind", status="error", last_test="2026-06-11T18:00:00",
        latency_ms=None, detail="finmind_chips_daily: 需要 Backer 方案",
    )
    out = _preview(api_client, "{{institutional_json}}", scope="per_symbol", symbol="2330")
    value = json.loads(out["rendered"])
    assert value["unavailable"] is True
    assert "Backer" in value["reason"]


def test_valuation_renders_from_snapshot(api_client: TestClient) -> None:
    conn = _conn_of(api_client)
    snapshots_store.add_snapshot(
        conn, source="finmind", dataset="valuation", symbol="2330",
        as_of=date(2026, 6, 11),
        payload={"rows": [{"date": "2026-06-11", "PER": "24.1", "PBR": "6.2",
                           "dividend_yield": "1.8"}]},
        fetched_at=datetime(2026, 6, 11, 18, 0),
    )
    out = _preview(api_client, "{{valuation_json}}", scope="per_symbol", symbol="2330")
    value = json.loads(out["rendered"])
    assert value.get("unavailable") is not True
    assert value["per"] == "24.1" and value["pbr"] == "6.2"


# --- (d) seeded sentiment snapshot -> vix/zone/fng rendered -------------------


def test_market_sentiment_renders_from_snapshot(api_client: TestClient) -> None:
    conn = _conn_of(api_client)
    snapshots_store.add_snapshot(
        conn, source="sentiment", dataset="vix", symbol=None, as_of=date(2026, 6, 11),
        payload={"close": "14.2"}, fetched_at=datetime(2026, 6, 11, 18, 0),
    )
    snapshots_store.add_snapshot(
        conn, source="sentiment", dataset="fng", symbol=None, as_of=date(2026, 6, 11),
        payload={"score": "62", "rating": "greed"},
        fetched_at=datetime(2026, 6, 11, 18, 0),
    )
    out = _preview(api_client, "{{market_sentiment_json}}")
    value = json.loads(out["rendered"])
    assert value["vix"] == "14.2"
    assert value["vix_zone"] == "low"
    assert value["fear_greed"] == "62" and value["fear_greed_rating"] == "greed"


def test_index_quotes_renders_from_snapshot(api_client: TestClient) -> None:
    conn = _conn_of(api_client)
    snapshots_store.add_snapshot(
        conn, source="index", dataset="index_quotes", symbol=None, as_of=date(2026, 6, 11),
        payload={"quotes": {"^TWII": "22150.5", "^GSPC": "5980.12", "^KLSE": "1612.0"}},
        fetched_at=datetime(2026, 6, 11, 18, 0),
    )
    out = _preview(api_client, "{{index_quotes_json}}")
    value = json.loads(out["rendered"])
    assert value["TAIEX"] == "22150.5"
    assert value["SPX"] == "5980.12"
    assert value["KLCI"] == "1612.0"
