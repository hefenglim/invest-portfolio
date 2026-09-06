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
    assert body["rolling_gap"] == {"gap": None, "window_n": 0, "min_scored": 8}
    # ⑫c (AI-D38): recorded, never clamped. rate null = no comparable card yet, NOT 0%.
    assert body["ceiling_violations"] == {"n": 0, "violations": 0, "rate": None}
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


def test_ai_score_rows_pagination(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """WPE: rows page via limit/offset; aggregates stay whole-set."""
    now = datetime(2026, 6, 11, 14, 30)
    es.ensure_tables(golden_db)
    for i in range(5):
        es.add_evaluation(
            golden_db, insight_id=100 + i, insight_type_id=10, calibration_version=1,
            is_shadow=False, status="scored", quant_hit=True, narrative_score=80,
            miss=False, actual_value=Decimal("0.05"), confidence=70, now=now)
    p1 = api_client.get("/api/ai-score", params={"limit": 2, "offset": 0}).json()
    p2 = api_client.get("/api/ai-score", params={"limit": 2, "offset": 2}).json()
    assert p1["rows_total_count"] == 5 and p2["rows_total_count"] == 5
    assert len(p1["rows"]) == 2 and len(p2["rows"]) == 2
    assert {r["id"] for r in p1["rows"]}.isdisjoint({r["id"] for r in p2["rows"]})
    # aggregates are NOT affected by the rows page
    assert p1["totals"]["n"] == 5 and p2["totals"]["n"] == 5
    assert len(p1["by_combo"]) == 1


def test_ai_score_decision_quality_fields(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """W7 (AI-D36): the HTTP shape of the trust tier + gate + rolling gap — the page
    renders these server-computed strings and never computes them itself."""
    now = datetime(2026, 6, 11, 14, 30)
    es.ensure_tables(golden_db)
    # 8 rows @ confidence 80, 6 hits → tier 可參考 (success 0.75, bins error 5.00).
    for i in range(8):
        es.add_evaluation(
            golden_db, insight_id=100 + i, insight_type_id=10, calibration_version=1,
            is_shadow=False, status="scored", quant_hit=i < 6, narrative_score=None,
            miss=i >= 6, actual_value=None, confidence=80, now=now)
    body = api_client.get("/api/ai-score").json()
    combo = body["by_combo"][0]
    assert combo["tier"] == "可參考"
    assert combo["gate_open"] is True
    assert combo["min_samples"] == 8 and combo["resolved_n"] == 8
    assert combo["calib_error_pp"] == "5.00"
    # The rolling gap shares the prompt variable's definition: claimed 0.80, actual 0.75.
    assert body["rolling_gap"] == {"gap": "-0.050", "window_n": 8, "min_scored": 8}


# --- M7-03: the DISPLAY GATE belongs to the payload, not to one page -----------


def test_ai_score_totals_carry_the_sample_gate(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """``totals`` reports its own gate the way ``by_combo`` already does (M7-03).

    Three scored rows against a min_samples of 8: the rates are still computed and still
    exact Decimal strings, but the payload now SAYS the gate is shut, so the band (and any
    other consumer) has something to render 「資料不足」 from instead of 66.67%.
    """
    now = datetime(2026, 6, 11, 14, 30)
    es.ensure_tables(golden_db)
    for i in range(3):
        es.add_evaluation(
            golden_db, insight_id=200 + i, insight_type_id=10, calibration_version=1,
            is_shadow=False, status="scored", quant_hit=i < 2, narrative_score=80 + i,
            miss=i >= 2, actual_value=None, confidence=70, now=now)
    totals = api_client.get("/api/ai-score").json()["totals"]
    # the gate — same field names/shape as by_combo (W7 AI-D36)
    assert totals["min_samples"] == 8
    assert totals["resolved_n"] == 3
    assert totals["gate_open"] is False
    # unchanged: the numbers themselves are right, only the disclosure was missing
    assert totals["n"] == 3
    assert totals["quant_hit_rate"] == "0.6667"
    assert totals["miss_rate"] == "0.3333"
    assert totals["avg_narrative"] == "81.00"


def test_ai_score_totals_gate_opens_at_min_samples(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """The aggregate gate opens on the SAME threshold the per-combo gate uses."""
    now = datetime(2026, 6, 11, 14, 30)
    es.ensure_tables(golden_db)
    for i in range(8):
        es.add_evaluation(
            golden_db, insight_id=300 + i, insight_type_id=10, calibration_version=1,
            is_shadow=False, status="scored", quant_hit=True, narrative_score=None,
            miss=False, actual_value=None, confidence=70, now=now)
    totals = api_client.get("/api/ai-score").json()["totals"]
    assert totals["resolved_n"] == 8 and totals["gate_open"] is True


def test_ai_score_empty_totals_are_never_readable_as_zero_percent(
    api_client: TestClient
) -> None:
    """An empty record must not hand a consumer a bare "0" it can print as 0.00%.

    ``quant_hit_rate``/``miss_rate``/``avg_narrative`` keep their existing type (Decimal
    string — the wire contract), so what makes them judgeable is the DENOMINATOR shipping
    beside them: ``resolved_n``/``quant_n`` are 0 and ``gate_open`` is false, which is
    exactly how ``by_combo`` has always been readable.
    """
    totals = api_client.get("/api/ai-score").json()["totals"]
    assert totals["n"] == 0
    assert totals["resolved_n"] == 0
    assert totals["quant_n"] == 0
    assert totals["gate_open"] is False
    assert totals["min_samples"] == 8


def test_ai_score_totals_quant_denominator_survives_an_open_gate(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """``quant_hit_rate`` is "0" both for "no hits" and for "no quantitative row at all".

    Above the gate only ``quant_n`` tells the two apart, so ``totals`` carries it — the
    narrative-only record below would otherwise read as a 0% hit rate over 8 samples.
    """
    now = datetime(2026, 6, 11, 14, 30)
    es.ensure_tables(golden_db)
    for i in range(8):
        es.add_evaluation(
            golden_db, insight_id=400 + i, insight_type_id=10, calibration_version=1,
            is_shadow=False, status="scored", quant_hit=None, narrative_score=70,
            miss=False, actual_value=None, confidence=70, now=now)
    totals = api_client.get("/api/ai-score").json()["totals"]
    assert totals["gate_open"] is True
    assert totals["quant_hit_rate"] == "0"  # unchanged type/name
    assert totals["quant_n"] == 0           # ...and now disambiguated
