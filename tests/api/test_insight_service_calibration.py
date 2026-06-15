"""Unit tests for ``api.insight_service.calibration_gap`` — the SINGLE-SOURCE calib_gap
helper (spec 03/04 I1).

Gates the portfolio-wide AI calibration error on the GLOBAL ``min_samples`` (composer
evolution_config; default 8) and returns ``scoring.calibration_error`` in PERCENTAGE
POINTS, else None (so the dashboard / alerts degrade silently below the gate). In-memory
connection with the composer + evaluations tables; no LLM, no float.
"""

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.api import insight_service
from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.llm_insight import evaluations_store as es

NOW = datetime(2026, 6, 14, 10, 0, tzinfo=ZoneInfo("Asia/Taipei"))


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    cs.ensure_seeded(c)  # evolution_config (min_samples default 8)
    es.ensure_tables(c)  # insight_evaluations
    yield c
    c.close()


def _seed_scored(
    c: sqlite3.Connection, n: int, *, confidence: int, miss: bool
) -> None:
    for i in range(n):
        es.add_evaluation(
            c, insight_id=1000 + i, insight_type_id=10, calibration_version=None,
            is_shadow=False, status="scored", quant_hit=not miss,
            narrative_score=20 if miss else 80, miss=miss, actual_value=None,
            confidence=confidence, now=NOW,
        )


def test_min_samples_is_eight_default(conn: sqlite3.Connection) -> None:
    # Pin the gate value this test relies on (so below/at-gate boundaries are meaningful).
    assert int(cs.get_evolution_config(conn)["min_samples"]) == 8


def test_below_min_samples_returns_none(conn: sqlite3.Connection) -> None:
    # 5 scored rows < 8 min_samples -> None (degrade silently).
    _seed_scored(conn, 5, confidence=80, miss=True)
    assert insight_service.calibration_gap(conn) is None


def test_empty_returns_none(conn: sqlite3.Connection) -> None:
    assert insight_service.calibration_gap(conn) is None


def test_at_gate_returns_calibration_error_pp(conn: sqlite3.Connection) -> None:
    # 8 scored rows, all confidence=80, all hits (miss=False) -> claimed avg = 80,
    # actual hit rate = 100% -> calibration error = |80 - 100| = 20 (PERCENTAGE POINTS).
    _seed_scored(conn, 8, confidence=80, miss=False)
    gap = insight_service.calibration_gap(conn)
    assert gap == Decimal("20")


def test_matches_scoring_calibration_error_directly(conn: sqlite3.Connection) -> None:
    # The helper is exactly scoring.calibration_error over scored_confidence_hits once gated.
    from portfolio_dash.llm_insight import scoring
    _seed_scored(conn, 10, confidence=70, miss=True)  # claimed 70, actual 0 -> 70pp
    rows = es.scored_confidence_hits(conn)
    assert insight_service.calibration_gap(conn) == scoring.calibration_error(rows)
    assert insight_service.calibration_gap(conn) == Decimal("70")
