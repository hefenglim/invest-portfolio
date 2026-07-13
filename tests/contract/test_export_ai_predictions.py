"""Contract tests for POST /api/export/ai-predictions (battle-record reconciliation CSV)."""

import sqlite3
from datetime import datetime
from decimal import Decimal

from fastapi.testclient import TestClient

from portfolio_dash.llm_insight import evaluations_store as es

_HEADER = ("insight_type_id,insight_id,calibration_version,is_shadow,status,"
           "quant_hit,narrative_score,miss,actual_value,confidence,evaluated_at")


def test_export_ai_predictions_empty(api_client: TestClient) -> None:
    r = api_client.post("/api/export/ai-predictions", json={})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "ai_predictions.csv" in r.headers["content-disposition"]
    assert r.content[:3] == b"\xef\xbb\xbf"
    text = r.content[3:].decode("utf-8")
    lines = [ln for ln in text.split("\r\n") if ln]
    assert lines == [_HEADER]


def test_export_ai_predictions_seeded_value(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    now = datetime(2026, 6, 11, 14, 30)
    es.ensure_tables(golden_db)
    es.add_evaluation(golden_db, insight_id=7, insight_type_id=10, calibration_version=2,
                      is_shadow=False, status="scored", quant_hit=True, narrative_score=90,
                      miss=False, actual_value=Decimal("0.05"), confidence=80, now=now)
    golden_db.commit()
    text = api_client.post("/api/export/ai-predictions", json={}).content[3:].decode("utf-8")
    # raw source-precision row, booleans lowercased, actual_value verbatim (0.05).
    assert "10,7,2,false,scored,true,90,false,0.05,80," in text
