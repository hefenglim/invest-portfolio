"""Unit tests for the insight_evaluations store + ai-score aggregation (spec 04.4/4.7).

In-memory connection; the store owns its ``insight_evaluations`` table. Pure persistence
+ deterministic SQL rollups — no LLM, no float (actual_value is a Decimal string).
"""

import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.llm_insight import evaluations_store as es

NOW = datetime(2026, 6, 14, 10, 0, tzinfo=ZoneInfo("Asia/Taipei"))


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    es.ensure_tables(c)
    yield c
    c.close()


def _tables(c: sqlite3.Connection) -> set[str]:
    return {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


# --- DDL ----------------------------------------------------------------------


def test_ensure_tables_creates_and_is_idempotent(conn: sqlite3.Connection) -> None:
    assert "insight_evaluations" in _tables(conn)
    assert conn.execute("SELECT COUNT(*) FROM insight_evaluations").fetchone()[0] == 0
    es.ensure_tables(conn)  # idempotent
    assert "insight_evaluations" in _tables(conn)


# --- add_evaluation -----------------------------------------------------------


def test_add_scored_evaluation_roundtrip(conn: sqlite3.Connection) -> None:
    ev = es.add_evaluation(
        conn, insight_id=1, insight_type_id=10, calibration_version=2,
        is_shadow=False, status="scored", quant_hit=True, narrative_score=80,
        miss=False, actual_value=Decimal("123.45"), confidence=70, now=NOW,
    )
    assert ev.id > 0
    assert ev.status == "scored"
    assert ev.quant_hit is True
    assert ev.narrative_score == 80
    assert ev.miss is False
    assert ev.actual_value == "123.45"  # Decimal STRING (never float)
    assert ev.defer_count == 0
    fetched = es.get_evaluation(conn, ev.id)
    assert fetched is not None
    assert fetched.quant_hit is True


def test_mark_pending_then_bump_defer(conn: sqlite3.Connection) -> None:
    ev = es.add_evaluation(
        conn, insight_id=5, insight_type_id=10, calibration_version=None,
        is_shadow=False, status="pending_data", quant_hit=None, narrative_score=None,
        miss=False, actual_value=None, confidence=None, now=NOW,
    )
    assert ev.status == "pending_data"
    assert ev.defer_count == 1  # add_evaluation with pending starts the defer counter at 1
    n = es.bump_defer(conn, insight_id=5, insight_type_id=10)
    assert n == 2  # one prior pending row → bumped to 2
    latest = es.latest_for_insight(conn, insight_id=5)
    assert latest is not None
    assert latest.defer_count == 2
    assert latest.status == "pending_data"


def test_mark_undetermined(conn: sqlite3.Connection) -> None:
    es.add_evaluation(
        conn, insight_id=7, insight_type_id=10, calibration_version=None,
        is_shadow=False, status="pending_data", quant_hit=None, narrative_score=None,
        miss=False, actual_value=None, confidence=None, now=NOW,
    )
    es.mark_undetermined(conn, insight_id=7, insight_type_id=10)
    latest = es.latest_for_insight(conn, insight_id=7)
    assert latest is not None
    assert latest.status == "undetermined"
    assert latest.miss is False  # undetermined is NEVER a miss


# --- due_insights -------------------------------------------------------------


def test_due_insights_excludes_already_scored(conn: sqlite3.Connection) -> None:
    # seed the insights table (the store reads due_at + columns from it).
    _seed_insights(conn)
    due = es.due_insights(conn, now=NOW)
    ids = {d.insight_id for d in due}
    # insight 100 due yesterday + unscored → due; 101 due tomorrow → not due;
    # 102 due yesterday but already scored → not due; 103 narrative (due_at NULL) → not due.
    assert ids == {100}


def test_due_insights_pending_still_due(conn: sqlite3.Connection) -> None:
    _seed_insights(conn)
    # a pending_data eval does NOT remove it from the due set (it must be retried).
    es.add_evaluation(
        conn, insight_id=100, insight_type_id=10, calibration_version=None,
        is_shadow=False, status="pending_data", quant_hit=None, narrative_score=None,
        miss=False, actual_value=None, confidence=None, now=NOW,
    )
    due = es.due_insights(conn, now=NOW)
    assert 100 in {d.insight_id for d in due}


# --- combo_score rollup -------------------------------------------------------


def test_combo_score_rollup_excludes_shadow_and_undetermined(conn: sqlite3.Connection) -> None:
    # three scored active rows + one shadow + one undetermined for combo 10.
    es.add_evaluation(conn, insight_id=1, insight_type_id=10, calibration_version=1,
                      is_shadow=False, status="scored", quant_hit=True, narrative_score=90,
                      miss=False, actual_value=None, confidence=80, now=NOW)
    es.add_evaluation(conn, insight_id=2, insight_type_id=10, calibration_version=1,
                      is_shadow=False, status="scored", quant_hit=False, narrative_score=40,
                      miss=True, actual_value=None, confidence=60, now=NOW)
    es.add_evaluation(conn, insight_id=3, insight_type_id=10, calibration_version=1,
                      is_shadow=False, status="scored", quant_hit=True, narrative_score=70,
                      miss=False, actual_value=None, confidence=50, now=NOW)
    es.add_evaluation(conn, insight_id=4, insight_type_id=10, calibration_version=1,
                      is_shadow=True, status="scored", quant_hit=False, narrative_score=10,
                      miss=True, actual_value=None, confidence=90, now=NOW)
    es.add_evaluation(conn, insight_id=5, insight_type_id=10, calibration_version=1,
                      is_shadow=False, status="undetermined", quant_hit=None,
                      narrative_score=None, miss=False, actual_value=None, confidence=None,
                      now=NOW)
    score = es.combo_score(conn, 10)
    assert score["n"] == 3  # only scored, non-shadow
    assert score["miss_count"] == 1
    assert score["quant_hit_count"] == 2  # 2 of 3 quant hits
    assert score["narrative_sum"] == 200  # 90 + 40 + 70
    # avg_narrative is a Decimal STRING (never float); miss_rate is a ratio string.
    assert isinstance(score["avg_narrative"], str)
    assert isinstance(score["miss_rate"], str)


def test_combo_score_shadow_rollup(conn: sqlite3.Connection) -> None:
    es.add_evaluation(conn, insight_id=1, insight_type_id=10, calibration_version=2,
                      is_shadow=True, status="scored", quant_hit=True, narrative_score=88,
                      miss=False, actual_value=None, confidence=70, now=NOW)
    es.add_evaluation(conn, insight_id=2, insight_type_id=10, calibration_version=2,
                      is_shadow=True, status="scored", quant_hit=True, narrative_score=92,
                      miss=False, actual_value=None, confidence=70, now=NOW)
    shadow = es.combo_score(conn, 10, is_shadow=True)
    assert shadow["n"] == 2
    assert shadow["miss_count"] == 0


# --- calibration_bins ---------------------------------------------------------


def test_calibration_bins_confidence_buckets(conn: sqlite3.Connection) -> None:
    # high confidence (90) but a miss → claimed high, actual low (calibration error).
    es.add_evaluation(conn, insight_id=1, insight_type_id=10, calibration_version=1,
                      is_shadow=False, status="scored", quant_hit=False, narrative_score=20,
                      miss=True, actual_value=None, confidence=90, now=NOW)
    es.add_evaluation(conn, insight_id=2, insight_type_id=10, calibration_version=1,
                      is_shadow=False, status="scored", quant_hit=True, narrative_score=80,
                      miss=False, actual_value=None, confidence=90, now=NOW)
    bins = es.calibration_bins(conn, 10)
    # find the 80-100 bucket
    hi = [b for b in bins if b["bucket"] == "80-100"]
    assert hi, "expected an 80-100 confidence bucket"
    b = hi[0]
    assert b["n"] == 2
    assert b["hit_count"] == 1  # 1 of 2 hit despite claimed 90% confidence
    # claimed (avg confidence) ~ 90; actual hit rate 50 → ~40pp calibration error.
    assert isinstance(b["claimed_pct"], str)
    assert isinstance(b["actual_pct"], str)
    assert isinstance(b["calibration_error_pp"], str)


# --- ai_score aggregate -------------------------------------------------------


def test_ai_score_empty_db() -> None:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    es.ensure_tables(c)
    score = es.ai_score(c)
    assert score["totals"]["n"] == 0
    assert score["by_combo"] == []
    assert score["calibration_bins"] == []
    assert score["rows"] == []
    c.close()


def test_ai_score_shape(conn: sqlite3.Connection) -> None:
    es.add_evaluation(conn, insight_id=1, insight_type_id=10, calibration_version=1,
                      is_shadow=False, status="scored", quant_hit=True, narrative_score=90,
                      miss=False, actual_value=Decimal("10"), confidence=80, now=NOW)
    es.add_evaluation(conn, insight_id=2, insight_type_id=10, calibration_version=1,
                      is_shadow=False, status="scored", quant_hit=False, narrative_score=30,
                      miss=True, actual_value=Decimal("11"), confidence=60, now=NOW)
    # a shadow row is excluded from the DISPLAYED active totals but kept in rows.
    es.add_evaluation(conn, insight_id=3, insight_type_id=10, calibration_version=2,
                      is_shadow=True, status="scored", quant_hit=False, narrative_score=10,
                      miss=True, actual_value=Decimal("12"), confidence=95, now=NOW)
    score = es.ai_score(conn)
    assert score["totals"]["n"] == 2  # shadow excluded from displayed active total
    assert score["totals"]["miss_count"] == 1
    assert len(score["by_combo"]) == 1
    assert score["by_combo"][0]["insight_type_id"] == 10
    assert len(score["rows"]) == 3  # all rows present (incl. shadow)
    assert any(r["is_shadow"] for r in score["rows"])


# --- samples for a calibration version ----------------------------------------


def test_miss_samples_for_version(conn: sqlite3.Connection) -> None:
    es.add_evaluation(conn, insight_id=1, insight_type_id=10, calibration_version=1,
                      is_shadow=False, status="scored", quant_hit=False, narrative_score=20,
                      miss=True, actual_value=None, confidence=70, now=NOW,
                      notes="個股失誤：方向相反")
    es.add_evaluation(conn, insight_id=2, insight_type_id=10, calibration_version=1,
                      is_shadow=False, status="scored", quant_hit=True, narrative_score=80,
                      miss=False, actual_value=None, confidence=70, now=NOW)
    samples = es.miss_samples_for_version(conn, insight_type_id=10, version=1)
    assert len(samples) == 1  # only the miss
    assert samples[0]["insight_id"] == 1
    assert samples[0]["notes"] == "個股失誤：方向相反"


# --- resolved sample count gate (min_samples) ---------------------------------


def test_resolved_sample_count_excludes_pending_and_undetermined(
    conn: sqlite3.Connection,
) -> None:
    es.add_evaluation(conn, insight_id=1, insight_type_id=10, calibration_version=1,
                      is_shadow=False, status="scored", quant_hit=True, narrative_score=80,
                      miss=False, actual_value=None, confidence=70, now=NOW)
    es.add_evaluation(conn, insight_id=2, insight_type_id=10, calibration_version=1,
                      is_shadow=False, status="pending_data", quant_hit=None,
                      narrative_score=None, miss=False, actual_value=None, confidence=None,
                      now=NOW)
    es.add_evaluation(conn, insight_id=3, insight_type_id=10, calibration_version=1,
                      is_shadow=False, status="undetermined", quant_hit=None,
                      narrative_score=None, miss=False, actual_value=None, confidence=None,
                      now=NOW)
    assert es.resolved_sample_count(conn, 10) == 1  # only the scored, non-shadow row


# --- helpers ------------------------------------------------------------------


def _seed_insights(conn: sqlite3.Connection) -> None:
    """A minimal ``insights`` table for due_insights() to read due_at + metadata."""
    from portfolio_dash.llm_insight import insights_store as istore
    from portfolio_dash.llm_insight.cards import Prediction

    istore.ensure_tables(conn)
    yesterday = (NOW - timedelta(days=1)).isoformat()
    tomorrow = (NOW + timedelta(days=1)).isoformat()
    pred = Prediction(metric="price_change", direction="up", target_pct=Decimal("0.03"),
                      horizon_days=5)
    # 100: due yesterday, prediction → due now
    _insert_insight(conn, 100, 10, "AAPL", pred, due_at=yesterday)
    # 101: due tomorrow → not yet due
    _insert_insight(conn, 101, 10, "MSFT", pred, due_at=tomorrow)
    # 102: due yesterday but already scored → not due
    _insert_insight(conn, 102, 10, "GOOG", pred, due_at=yesterday)
    es.add_evaluation(conn, insight_id=102, insight_type_id=10, calibration_version=None,
                      is_shadow=False, status="scored", quant_hit=True, narrative_score=80,
                      miss=False, actual_value=None, confidence=70, now=NOW)
    # 103: narrative card, due_at NULL → never due
    _insert_insight(conn, 103, 10, "TSLA", None, due_at=None)


def _insert_insight(
    conn: sqlite3.Connection,
    insight_id: int,
    type_id: int,
    symbol: str,
    prediction: object,
    *,
    due_at: str | None,
) -> None:
    from portfolio_dash.llm_insight import insights_store as istore
    from portfolio_dash.llm_insight.cards import InsightCard, Prediction

    pred = prediction if isinstance(prediction, Prediction) else None
    card = InsightCard(
        title=f"{symbol} view", summary="s", body_md="b", tags=[], symbol=symbol,
        confidence=70 if pred is not None else None, prediction=pred,
    )
    rec = istore.add_card(
        conn, insight_type_id=type_id, card=card, fingerprint=f"fp-{insight_id}",
        calibration_version=None, horizon_days=5, input_snapshot="snap", model="m",
        cost_usd=Decimal("0"), now=NOW,
    )
    # Force the row id + due_at to the deterministic test values.
    conn.execute(
        "UPDATE insights SET id = ?, due_at = ? WHERE id = ?",
        (insight_id, due_at, rec.id),
    )
    conn.commit()


# --- scored_confidence_hits (calib_gap input, spec 03/04 I1) ------------------


def test_scored_confidence_hits_filters_and_maps(conn: sqlite3.Connection) -> None:
    # A scored, non-shadow, confidence=80, NOT a miss -> (80, True).
    es.add_evaluation(
        conn, insight_id=1, insight_type_id=10, calibration_version=None,
        is_shadow=False, status="scored", quant_hit=True, narrative_score=80,
        miss=False, actual_value=None, confidence=80, now=NOW,
    )
    # A scored, non-shadow, confidence=60, a miss -> (60, False) (hit = not miss).
    es.add_evaluation(
        conn, insight_id=2, insight_type_id=10, calibration_version=None,
        is_shadow=False, status="scored", quant_hit=False, narrative_score=40,
        miss=True, actual_value=None, confidence=60, now=NOW,
    )
    # Excluded: shadow row.
    es.add_evaluation(
        conn, insight_id=3, insight_type_id=10, calibration_version=None,
        is_shadow=True, status="scored", quant_hit=True, narrative_score=80,
        miss=False, actual_value=None, confidence=90, now=NOW,
    )
    # Excluded: pending_data status.
    es.add_evaluation(
        conn, insight_id=4, insight_type_id=10, calibration_version=None,
        is_shadow=False, status="pending_data", quant_hit=None, narrative_score=None,
        miss=False, actual_value=None, confidence=50, now=NOW,
    )
    # Excluded: scored but NULL confidence.
    es.add_evaluation(
        conn, insight_id=5, insight_type_id=10, calibration_version=None,
        is_shadow=False, status="scored", quant_hit=True, narrative_score=80,
        miss=False, actual_value=None, confidence=None, now=NOW,
    )
    pairs = es.scored_confidence_hits(conn)
    assert sorted(pairs) == [(60, False), (80, True)]


def test_scored_confidence_hits_empty(conn: sqlite3.Connection) -> None:
    assert es.scored_confidence_hits(conn) == []
