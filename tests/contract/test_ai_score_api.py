"""Contract tests for GET /api/ai-score + GET /api/calibrations/{id}/samples (spec 04.7).

Drives the API through the golden TestClient (in-process, no network). Empty DB → zeroed/[];
seeded evaluations roll up into the battle-record shape.
"""

import sqlite3
from datetime import datetime
from decimal import Decimal

from fastapi.testclient import TestClient

from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.llm_insight import evaluations_store as es


def test_ai_score_empty_db(api_client: TestClient) -> None:
    r = api_client.get("/api/ai-score")
    assert r.status_code == 200
    body = r.json()
    assert body["totals"]["n"] == 0
    assert body["by_combo"] == []
    assert body["calibration_bins"] == []
    assert body["rows"] == []


def test_ai_score_rollup(api_client: TestClient, golden_db: sqlite3.Connection) -> None:
    now = datetime(2026, 6, 11, 14, 30)
    es.ensure_tables(golden_db)
    es.add_evaluation(golden_db, insight_id=1, insight_type_id=10, calibration_version=1,
                      is_shadow=False, status="scored", quant_hit=True, narrative_score=90,
                      miss=False, actual_value=Decimal("0.05"), confidence=80, now=now)
    es.add_evaluation(golden_db, insight_id=2, insight_type_id=10, calibration_version=1,
                      is_shadow=False, status="scored", quant_hit=False, narrative_score=30,
                      miss=True, actual_value=Decimal("0.01"), confidence=60, now=now)
    # a shadow row → excluded from displayed totals, present in rows.
    es.add_evaluation(golden_db, insight_id=3, insight_type_id=10, calibration_version=2,
                      is_shadow=True, status="scored", quant_hit=False, narrative_score=10,
                      miss=True, actual_value=Decimal("0.02"), confidence=95, now=now)

    body = api_client.get("/api/ai-score").json()
    assert body["totals"]["n"] == 2  # shadow excluded from the displayed active totals
    assert body["totals"]["miss_count"] == 1
    assert isinstance(body["totals"]["miss_rate"], str)  # Decimal string (never float)
    assert len(body["by_combo"]) == 1
    assert body["by_combo"][0]["insight_type_id"] == 10
    assert len(body["rows"]) == 3  # all rows incl. shadow
    assert any(row["is_shadow"] for row in body["rows"])
    assert len(body["calibration_bins"]) >= 1


def test_calibration_samples_real(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    now = datetime(2026, 6, 11, 14, 30)
    cs.ensure_seeded(golden_db)
    es.ensure_tables(golden_db)
    it = cs.create_insight_type(golden_db, name="SC", scope="per_symbol",
                                self_correct=True, now=now)
    cal = cs.create_calibration(golden_db, it.id, body="v1", cause=None, now=now)
    # one miss + one hit under version 1; samples returns the miss only.
    es.add_evaluation(golden_db, insight_id=1, insight_type_id=it.id, calibration_version=1,
                      is_shadow=False, status="scored", quant_hit=False, narrative_score=20,
                      miss=True, actual_value=None, confidence=70, now=now,
                      notes="（個股）方向相反")
    es.add_evaluation(golden_db, insight_id=2, insight_type_id=it.id, calibration_version=1,
                      is_shadow=False, status="scored", quant_hit=True, narrative_score=80,
                      miss=False, actual_value=None, confidence=70, now=now)

    r = api_client.get(f"/api/calibrations/{cal.id}/samples")
    assert r.status_code == 200
    samples = r.json()
    assert len(samples) == 1  # only the miss drove this version
    assert samples[0]["insight_id"] == 1
    assert samples[0]["notes"] == "（個股）方向相反"


def test_calibration_samples_empty_for_unknown(api_client: TestClient) -> None:
    # an unknown calibration id → [] (no crash, contract shape preserved).
    r = api_client.get("/api/calibrations/9999/samples")
    assert r.status_code == 200
    assert r.json() == []
