"""Contract tests for the chips/sentiment external variables going live (spec 20.2).

Uses the shared golden_db + api_client (the golden DB creates external_snapshots but
leaves it EMPTY, so every external var degrades to ``{"unavailable": true}``). Then
seeds snapshots to assert the router reads them, derives via portfolio.external_signals,
and feeds the value into the rendered prompt. No LLM is called (preview path).
"""

import json
import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

from portfolio_dash.api.deps import get_conn
from portfolio_dash.pricing import datasources_store, snapshots_store
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.enums import Market
from tests.conftest import GOLDEN_NOW


def _conn_of(client: TestClient) -> sqlite3.Connection:
    return client.app.dependency_overrides[get_conn]()  # type: ignore[attr-defined,no-any-return]


def _seed_long_series(conn: sqlite3.Connection, symbol: str, n: int = 320) -> None:
    """Seed ``n`` ascending daily closes ending at the golden clock date so the rule engine
    evaluates fully (confirmed uptrend, positive momentum, a real composite)."""
    end = GOLDEN_NOW.date()
    rows = [
        PriceRow(
            instrument=symbol, market=Market.TW,
            as_of=end - timedelta(days=n - 1 - i),
            close=Decimal(100) + Decimal(i) * Decimal("2"), source="test",
        )
        for i in range(n)
    ]
    upsert_prices(conn, rows, fetched_at=GOLDEN_NOW)


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
    # 31 previously live + 1 P2-batch-3 rule_signals (rule_signals_json) = 32.
    assert sum(1 for r in rows if r["available"]) == 32
    assert by_token["technical_signals_json"]["available"] is True
    assert by_token["fear_greed_json"]["available"] is True
    assert by_token["symbol_news_json"]["available"] is True
    assert by_token["consensus_json"]["available"] is True
    assert by_token["rule_signals_json"]["available"] is True
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


# --- (e) consensus var: snapshot renders; absence degrades with reason (P1 batch 2) ---


def test_consensus_renders_from_snapshot(api_client: TestClient) -> None:
    conn = _conn_of(api_client)
    snapshots_store.add_snapshot(
        conn, source="yfinance", dataset="consensus", symbol="2330",
        as_of=date(2026, 7, 9),
        payload={
            "as_of": "2026-07-09",
            "price_targets": {"current": "2465.0", "mean": "2819.8484",
                              "high": "3800.0", "low": "2051.0"},
            "ratings": {"strong_buy": 9, "buy": 23, "hold": 1, "sell": 0,
                        "strong_sell": 0, "total": 33},
            "rating_score": "1.76", "upside_vs_mean_pct": "0.1440",
            "source": "yfinance",
        },
        fetched_at=datetime(2026, 7, 9, 9, 10),
    )
    out = _preview(api_client, "{{consensus_json}}", scope="per_symbol", symbol="2330")
    value = json.loads(out["rendered"])
    assert value.get("unavailable") is not True
    assert value["as_of"] == "2026-07-09"
    assert value["price_targets"]["mean"] == "2819.8484"
    assert value["ratings"]["total"] == 33
    assert value["rating_score"] == "1.76"


def test_consensus_degrades_with_no_coverage_reason(api_client: TestClient) -> None:
    # No snapshot for an uncovered symbol -> unavailable + honest "no coverage" reason.
    out = _preview(api_client, "{{consensus_json}}", scope="per_symbol", symbol="ZZZ")
    value = json.loads(out["rendered"])
    assert value["unavailable"] is True
    assert "無分析師覆蓋" in value["reason"]


# --- (f) rule_signals var: computed-on-read via the SAME path as /api/signals (P2 b3) ---


def test_rule_signals_renders_from_series(api_client: TestClient) -> None:
    # A seeded long series → the var carries the full /api/signals wire (TechScore +
    # four rules + held flag), computed via signals_service (single source of truth).
    conn = _conn_of(api_client)
    _seed_long_series(conn, "2330")
    out = _preview(api_client, "{{rule_signals_json}}", scope="per_symbol", symbol="2330")
    value = json.loads(out["rendered"])
    assert value.get("unavailable") is not True
    assert value["composite"]["coverage"] == "4/4"
    assert value["composite"]["tech_score"]                  # a real TechScore (STRING)
    assert value["rules"]["trend_filter"]["state"] == "above_confirmed"
    assert value["held"] is True                             # 2330 is a golden position
    # identical display quantization as /api/signals (reused, not duplicated).
    api_wire = api_client.get("/api/signals/2330").json()
    assert value["composite"]["tech_score"] == api_wire["composite"]["tech_score"]


def test_rule_signals_degrades_thin_series_with_reason(api_client: TestClient) -> None:
    # The golden DB stores ONE price for 2330 → every rule None, composite None →
    # honest unavailable + the "price history insufficient" reason.
    out = _preview(api_client, "{{rule_signals_json}}", scope="per_symbol", symbol="2330")
    value = json.loads(out["rendered"])
    assert value["unavailable"] is True
    assert "價格歷史不足" in value["reason"]


def test_rule_signals_feeds_preview_and_generation(api_client: TestClient) -> None:
    # BOTH the preview seam (prompts._external_vars, via _build_context) AND the generation
    # seam (insight_service._per_symbol_ctx) resolve rule_signals_json through the SAME
    # shared _external_vars — so a card and its preview see identical rule signals.
    from portfolio_dash.api.insight_service import _per_symbol_ctx
    from portfolio_dash.api.routers.prompts import _external_vars
    from portfolio_dash.portfolio.dashboard import build_dashboard
    from portfolio_dash.shared.enums import Currency

    conn = _conn_of(api_client)
    _seed_long_series(conn, "2330")
    ext = _external_vars(conn, "2330", now=GOLDEN_NOW)
    assert "rule_signals_json" in ext
    assert ext["rule_signals_json"].get("unavailable") is not True
    data = build_dashboard(conn, now=GOLDEN_NOW, reporting=Currency.TWD)
    ctx = _per_symbol_ctx(conn, data, "2330", now=GOLDEN_NOW, reporting=Currency.TWD)
    assert "rule_signals_json" in ctx.external_vars
    assert ctx.external_vars["rule_signals_json"]["composite"] is not None


def test_rule_signals_partial_coverage_not_degraded(api_client: TestClient) -> None:
    # Deep review 2026-07-11 (batch-3 adequacy gap): a series with ≥1 evaluable rule but too
    # few for a composite must NOT degrade to unavailable — it carries the partial coverage
    # honestly. Kills a degrade `AND`→`OR` mutation in prompts._rule_signals_var, which only
    # returns {"unavailable": true} when composite is None AND EVERY rule is None; with `OR`
    # this partial case (composite None, one rule non-null) would be wrongly hidden.
    #
    # Session-count math (verified vs strategy/rules/params.py): trend_filter needs ma=200
    # closes, ma_cross needs slow=200, momentum_12_1 needs lookback_sessions+1=253 — all None
    # at 20 closes; rsi_regime needs only period+1=15 (technicals.rsi) and week52 works on any
    # window → the ONLY evaluable rule at 20 closes ⇒ 1 rule < 2 ⇒ composite None, rules
    # partially non-null.
    from portfolio_dash.api.routers.prompts import (
        _external_reasons,
        _external_vars,
        _rule_signals_var,
    )

    conn = _conn_of(api_client)
    _seed_long_series(conn, "2330", n=20)
    var = _rule_signals_var(conn, "2330", now=GOLDEN_NOW)
    # NOT degraded: the full wire, never the {"unavailable": true} shape.
    assert var.get("unavailable") is not True
    # composite is too thin (1 evaluable rule < 2) → honest None, NOT unavailable.
    assert var["composite"] is None
    # the one evaluable rule is carried; the three too-short rules are honest nulls.
    assert var["rules"]["rsi_regime"] is not None
    assert var["rules"]["trend_filter"] is None
    assert var["rules"]["ma_cross"] is None
    assert var["rules"]["momentum_12_1"] is None
    # and NO degrade reason attaches (the var is available, so _external_reasons stays silent).
    ext = _external_vars(conn, "2330", now=GOLDEN_NOW)
    assert ext["rule_signals_json"].get("unavailable") is not True
    assert "rule_signals_json" not in _external_reasons(conn, ext)


def test_rule_signals_var_equals_full_api_signals_dict(api_client: TestClient) -> None:
    # Deep review 2026-07-11 (batch-3 adequacy gap): the rule_signals_json var MUST equal the
    # FULL GET /api/signals/{sym} dict byte-for-byte — pinning `held` AND every display
    # quantization (score 2dp, tech_score 1dp, ratio evidence 4dp) against drift, since both
    # are the ONE source of truth (signals_service.to_wire). The existing
    # test_rule_signals_renders_from_series only pins tech_score; this pins the whole wire.
    from portfolio_dash.api.routers.prompts import _rule_signals_var

    conn = _conn_of(api_client)
    _seed_long_series(conn, "2330")  # 320 rows → full coverage, non-degraded wire
    var = _rule_signals_var(conn, "2330", now=GOLDEN_NOW)
    api_wire = api_client.get("/api/signals/2330").json()
    # Both go through signals_service.to_wire with the SAME injected clock (GOLDEN_NOW) and
    # the SAME held check → identical evaluated_at/as_of/held/scores/composite.
    assert var == api_wire
