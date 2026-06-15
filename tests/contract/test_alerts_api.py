"""Contract tests: GET /api/alerts, the dashboard payload's embedded alerts, and the
PUT /api/alert-rules recompute. All three must reflect the SAME single rule engine."""

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from portfolio_dash.llm_insight import evaluations_store as es

_SEED_NOW = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))


def _seed_high_calibration_error(conn: sqlite3.Connection) -> None:
    """Seed >= min_samples (default 8) scored, non-shadow evals with a large gap.

    All confidence=80 but all misses -> claimed avg 80 vs actual hit rate 0% -> 80pp,
    comfortably above the 15pp calib_gap default. Exceeds the global min_samples gate so
    insight_service.calibration_gap returns a non-None value.
    """
    for i in range(8):
        es.add_evaluation(
            conn, insight_id=2000 + i, insight_type_id=10, calibration_version=None,
            is_shadow=False, status="scored", quant_hit=False, narrative_score=20,
            miss=True, actual_value=None, confidence=80, now=_SEED_NOW,
        )


def test_get_alerts(api_client: TestClient) -> None:
    r = api_client.get("/api/alerts")
    assert r.status_code == 200
    body = r.json()
    assert "as_of" in body and isinstance(body["alerts"], list)
    assert any(a["id"] == "single_weight:2330" for a in body["alerts"])


def test_dashboard_embeds_same_alerts(api_client: TestClient) -> None:
    dash = api_client.get("/api/dashboard").json()
    assert "alerts" in dash
    assert any(a["id"] == "single_weight:2330" for a in dash["alerts"])


def test_alerts_single_source_dashboard_equals_endpoint(api_client: TestClient) -> None:
    # Single source of truth: the dashboard-embedded alerts and GET /api/alerts must be
    # byte-for-byte identical (same compute_alerts_from over the same rules/quota reads).
    dash_alerts = api_client.get("/api/dashboard").json()["alerts"]
    endpoint_alerts = api_client.get("/api/alerts").json()["alerts"]
    assert sorted(dash_alerts, key=lambda a: a["id"]) == \
        sorted(endpoint_alerts, key=lambda a: a["id"])


def test_put_alert_rules_returns_recomputed_alerts(api_client: TestClient) -> None:
    r = api_client.put("/api/alert-rules",
                       json={"rules": [{"id": "single_weight", "enabled": False, "value": "0.30"}]})
    assert r.status_code == 200
    body = r.json()
    assert "rules" in body and "alerts" in body
    assert not any(a["id"].startswith("single_weight") for a in body["alerts"])


def test_calib_gap_absent_without_samples(api_client: TestClient) -> None:
    # The golden DB seeds insight_evaluations EMPTY -> below min_samples -> calib_gap silent.
    dash = api_client.get("/api/dashboard").json()
    endpoint = api_client.get("/api/alerts").json()
    assert not any(a["id"] == "calib_gap" for a in dash["alerts"])
    assert not any(a["id"] == "calib_gap" for a in endpoint["alerts"])


def test_calib_gap_in_dashboard_and_endpoint(
    golden_db: sqlite3.Connection, api_client: TestClient
) -> None:
    # Seed scored evaluations exceeding the global min_samples gate with a high error.
    _seed_high_calibration_error(golden_db)
    dash_alerts = api_client.get("/api/dashboard").json()["alerts"]
    endpoint_alerts = api_client.get("/api/alerts").json()["alerts"]
    # calib_gap appears in BOTH surfaces (single-source equality preserved).
    dash_cg = next(a for a in dash_alerts if a["id"] == "calib_gap")
    endpoint_cg = next(a for a in endpoint_alerts if a["id"] == "calib_gap")
    assert dash_cg["sev"] == "warn" and dash_cg["rule"] == "calib_gap"
    assert dash_cg == endpoint_cg
    # And the two full arrays remain byte-for-byte identical (the single-source invariant).
    assert sorted(dash_alerts, key=lambda a: a["id"]) == \
        sorted(endpoint_alerts, key=lambda a: a["id"])


def test_calibration_regression_not_surfaced_as_rule(
    golden_db: sqlite3.Connection, api_client: TestClient
) -> None:
    # W3: calibration_regression is a spec-04c EVENT (alert_events / bell feed), NOT a rule
    # in the /api/alerts rule-derived view — it must never appear here even with samples.
    _seed_high_calibration_error(golden_db)
    endpoint_alerts = api_client.get("/api/alerts").json()["alerts"]
    assert not any(a["id"] == "calibration_regression" for a in endpoint_alerts)
    assert not any(a["rule"] == "calibration_regression" for a in endpoint_alerts)
