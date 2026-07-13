"""Unit tests for the AI-prediction CSV builder (export.ai_predictions).

Reads the SAME evaluations store the 預測明細 table is fed from; mirrors its columns and
emits the whole archived-excluded set at source precision (raw Decimal/int strings).
"""

import sqlite3
from datetime import datetime
from decimal import Decimal

from portfolio_dash.export.ai_predictions import build_ai_predictions_csv
from portfolio_dash.llm_insight import evaluations_store as es
from tests.conftest import init_golden_base

_COLS = ("insight_type_id,insight_id,calibration_version,is_shadow,status,"
         "quant_hit,narrative_score,miss,actual_value,confidence,evaluated_at")
_NOW = datetime(2026, 6, 11, 14, 30)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_golden_base(conn)
    conn.commit()
    return conn


def test_header_bom_and_empty_body() -> None:
    conn = _conn()
    try:
        art = build_ai_predictions_csv(conn)
    finally:
        conn.close()
    assert art.filename == "ai_predictions.csv"
    assert art.content[:3] == b"\xef\xbb\xbf"
    text = art.content[3:].decode("utf-8")
    lines = [ln for ln in text.split("\r\n") if ln]
    assert lines == [_COLS]  # header only on an empty store


def test_rows_are_raw_source_precision_values() -> None:
    conn = _conn()
    try:
        es.add_evaluation(conn, insight_id=7, insight_type_id=10, calibration_version=2,
                          is_shadow=False, status="scored", quant_hit=True,
                          narrative_score=90, miss=False, actual_value=Decimal("0.05"),
                          confidence=80, now=_NOW)
        es.add_evaluation(conn, insight_id=8, insight_type_id=10, calibration_version=2,
                          is_shadow=True, status="scored", quant_hit=False,
                          narrative_score=30, miss=True, actual_value=Decimal("-0.012"),
                          confidence=60, now=_NOW)
        conn.commit()
        art = build_ai_predictions_csv(conn)
    finally:
        conn.close()
    text = art.content[3:].decode("utf-8")
    rows = [ln for ln in text.split("\r\n") if ln][1:]  # drop header
    assert len(rows) == 2
    # booleans lowercased; actual_value verbatim (never float-coerced); no display glyphs.
    assert "10,7,2,false,scored,true,90,false,0.05,80," in text
    assert "10,8,2,true,scored,false,30,true,-0.012,60," in text
    assert "✓" not in text and "✗" not in text
