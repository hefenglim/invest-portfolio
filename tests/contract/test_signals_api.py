"""Contract: GET /api/signals + GET /api/signals/{symbol}.

The golden DB stores ONE price per held symbol, so the full held view degrades HONESTLY
(every rule None, composite None) — the API must pass that through, never pad. A seeded
long-series variant exercises the full path (all four rules + composite). Money/score/ratio
values are Decimal STRINGS; the frontend never computes.
"""

import sqlite3
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

from portfolio_dash.api.signals_service import required_calendar_days, required_sessions
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.enums import Market
from portfolio_dash.strategy.rules.params import default_params
from tests.conftest import GOLDEN_NOW

_RULE_NAMES = {"trend_filter", "ma_cross", "momentum_12_1", "rsi_regime"}


def _assert_wire_discipline(node: object) -> None:
    """No bare float / Decimal anywhere — every number is a STRING or a plain int/bool."""
    if isinstance(node, dict):
        for v in node.values():
            _assert_wire_discipline(v)
    elif isinstance(node, list):
        for v in node:
            _assert_wire_discipline(v)
    else:
        assert not isinstance(node, float), f"float leaked to the wire: {node!r}"
        assert not isinstance(node, Decimal), f"raw Decimal leaked to the wire: {node!r}"


def _seed_long_series(conn: sqlite3.Connection, symbol: str, n: int = 320) -> None:
    """Seed ``n`` consecutive daily ascending closes ending at the golden clock date."""
    end = GOLDEN_NOW.date()
    rows = [
        PriceRow(
            instrument=symbol, market=Market.TW,
            as_of=end - timedelta(days=n - 1 - i),
            close=Decimal(100) + Decimal(i) * Decimal("2"),
            volume=Decimal(1000) + Decimal(i), source="test",
        )
        for i in range(n)
    ]
    upsert_prices(conn, rows, fetched_at=GOLDEN_NOW)


# --- window helper (THE known trap) --------------------------------------------


def test_window_derived_from_params_covers_all_rules() -> None:
    p = default_params()
    # max(momentum 253, cross 260, rsi 253, trend 200) == 260 sessions.
    assert required_sessions(p) == 260
    # ceil(260 × 1.4 × 1.6) == 583 calendar days — NOT the 400d technicals constant.
    assert required_calendar_days(p) == 583
    assert required_calendar_days(p) > 400


def test_window_moves_with_params() -> None:
    p = default_params()
    longer = replace(p, momentum=replace(p.momentum, lookback_sessions=500))
    assert required_sessions(longer) == 501  # 500 + 1 now dominates
    assert required_calendar_days(longer) > required_calendar_days(p)


# --- full held view: honest degrade on the golden DB ---------------------------


def test_signals_shape_and_honest_degrade(api_client: TestClient) -> None:
    body = api_client.get("/api/signals").json()
    assert set(body) == {"as_of", "evaluated_at", "signals"}
    assert body["as_of"] == "2026-06-11"
    syms = {s["symbol"] for s in body["signals"]}
    assert {"2330", "AAPL"} <= syms
    for entry in body["signals"]:
        assert entry["params_version"] == "rules-v1"
        assert set(entry["rules"]) == _RULE_NAMES
        # One stored price per symbol → every rule too thin to judge → None, composite None.
        assert all(v is None for v in entry["rules"].values())
        assert entry["composite"] is None
    _assert_wire_discipline(body)


def test_single_symbol_endpoint_degrades(api_client: TestClient) -> None:
    body = api_client.get("/api/signals/2330").json()
    assert body["symbol"] == "2330"
    assert body["params_version"] == "rules-v1"
    assert set(body["rules"]) == _RULE_NAMES
    assert all(v is None for v in body["rules"].values())
    assert body["composite"] is None
    _assert_wire_discipline(body)


def test_single_symbol_unknown_is_honest_not_500(api_client: TestClient) -> None:
    body = api_client.get("/api/signals/NOPE").json()
    assert body["symbol"] == "NOPE"
    assert all(v is None for v in body["rules"].values())
    assert body["composite"] is None


# --- full path over a seeded long series ---------------------------------------


def test_single_symbol_full_path(
    golden_db: sqlite3.Connection, api_client: TestClient
) -> None:
    _seed_long_series(golden_db, "2330")
    body = api_client.get("/api/signals/2330").json()

    rules = body["rules"]
    # All four rules evaluable now (ascending series → confirmed uptrend, positive momentum).
    assert all(rules[name] is not None for name in _RULE_NAMES)
    assert rules["trend_filter"]["state"] == "above_confirmed"
    assert rules["momentum_12_1"]["state"] == "positive"

    comp = body["composite"]
    assert comp is not None
    assert comp["coverage"] == "4/4"
    assert comp["missing"] == []
    # Display quantization: tech_score 1 dp, scores 2 dp, ratio evidence 4 dp — all STRINGS.
    assert isinstance(comp["tech_score"], str)
    assert len(comp["tech_score"].split(".")[1]) == 1  # exactly 1 dp
    assert isinstance(rules["trend_filter"]["score"], str)
    assert len(rules["trend_filter"]["score"].split(".")[1]) == 2  # exactly 2 dp
    assert len(rules["trend_filter"]["evidence"]["price_vs_ma"].split(".")[1]) == 4
    # window_days is a per-rule int (NOT aggregated) — differs across rules.
    assert rules["ma_cross"]["window_days"] == 260
    assert rules["trend_filter"]["window_days"] == 200
    _assert_wire_discipline(body)


def test_full_held_view_full_path(
    golden_db: sqlite3.Connection, api_client: TestClient
) -> None:
    _seed_long_series(golden_db, "2330")
    body = api_client.get("/api/signals").json()
    entry = next(s for s in body["signals"] if s["symbol"] == "2330")
    assert entry["composite"] is not None
    assert entry["composite"]["coverage"] == "4/4"
