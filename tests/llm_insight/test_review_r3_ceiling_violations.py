"""R3/⑫c counter-evidence: AI-D38 step three — RECORD the violations, never clamp them.

Steps (a) and (b) fixed the ceiling's arithmetic. Step (c) is the accountability half: the
prompt asks the model to keep its stated confidence at or below the computed ceiling, and
until now **nothing anywhere recorded whether it did**. A rule with no measurement is a
suggestion, and W7.1's first live run had 0 of 13 cards obey a comparable one.

The design constraint that decides where the column lives: an ``insight_evaluations`` row is
written at SCORING time, while ``confidence_ceiling`` is a CREATE-time value derived from
calibration bins that keep moving. It is therefore not recoverable after the fact — it has to
be stamped on the ``insights`` row when the card is written, exactly as ``price_at_create``
(M4) is. That is the precedent this mirrors, migration and all.

Population (owner ruling 2026-08-26): every non-shadow card carrying both a stated confidence
and a recorded ceiling — scored or not. A violation is a fact about the moment of creation;
tying it to the evaluation queue would delay the number by a full horizon and would silently
drop every card that ends ``undetermined``, which could have violated just the same.

⚠ The red line AI-D33 drew is not touched: a card whose confidence exceeds its ceiling is
stored with the confidence THE MODEL STATED. The last test pins that.
"""

import sqlite3
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from portfolio_dash.llm_insight import evaluations_store as es
from portfolio_dash.llm_insight import insights_store as istore
from portfolio_dash.llm_insight.cards import InsightCard

NOW = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))


def _card(confidence: int | None) -> InsightCard:
    return InsightCard(
        title="t", summary="s", body_md="b", tags=["x"],
        confidence=confidence, prediction=None, symbol="AAPL",
    )


def _add(
    conn: sqlite3.Connection, *, confidence: int | None, ceiling: int | None,
    shadow: bool = False,
) -> None:
    istore.add_card(
        conn, insight_type_id=1, card=_card(confidence),
        fingerprint=f"fp-{confidence}-{ceiling}-{shadow}", calibration_version=None,
        horizon_days=14, input_snapshot="{}", model="m", cost_usd=Decimal("0"),
        now=NOW, is_shadow=shadow, ceiling_at_create=ceiling,
    )


def _store(tmp_path: object) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    istore.ensure_tables(conn)
    es.ensure_tables(conn)
    return conn


# --- the number itself ---------------------------------------------------------------


def test_the_rate_counts_cards_over_their_own_recorded_ceiling(tmp_path: object) -> None:
    conn = _store(tmp_path)
    _add(conn, confidence=55, ceiling=60)   # obeyed
    _add(conn, confidence=80, ceiling=60)   # violated
    out = es.ceiling_violations(conn)
    assert out == {"n": 2, "violations": 1, "rate": "0.5000"}  # _ratio_str 4-dp, like miss_rate


def test_confidence_exactly_at_the_ceiling_is_not_a_violation(tmp_path: object) -> None:
    """「不得超過」 — equal is allowed. The boundary, not the midpoint."""
    conn = _store(tmp_path)
    _add(conn, confidence=60, ceiling=60)
    assert es.ceiling_violations(conn)["violations"] == 0


def test_a_card_with_no_recorded_ceiling_is_outside_the_population(tmp_path: object) -> None:
    """Legacy cards (and any card generated while backtest_json was unavailable) have no
    ceiling to have violated. Counting them as COMPLIANT would flatter the rate with rows
    that were never governed by the rule."""
    conn = _store(tmp_path)
    _add(conn, confidence=95, ceiling=None)
    assert es.ceiling_violations(conn) == {"n": 0, "violations": 0, "rate": None}


def test_a_card_with_no_confidence_is_outside_the_population(tmp_path: object) -> None:
    conn = _store(tmp_path)
    _add(conn, confidence=None, ceiling=60)
    assert es.ceiling_violations(conn)["n"] == 0


def test_shadow_cards_do_not_enter_the_displayed_record(tmp_path: object) -> None:
    conn = _store(tmp_path)
    _add(conn, confidence=99, ceiling=10, shadow=True)
    assert es.ceiling_violations(conn)["n"] == 0


def test_an_empty_population_reports_null_not_zero_percent(tmp_path: object) -> None:
    """0% compliance and 「no cards yet」 are different facts; the wire must not conflate
    them into a reassuring 0.00%."""
    conn = _store(tmp_path)
    assert es.ceiling_violations(conn)["rate"] is None


# --- the red line: recorded, never clamped -------------------------------------------


def test_a_violating_card_is_stored_with_the_confidence_the_model_stated(
    tmp_path: object,
) -> None:
    """AI-D33/AI-D38: measuring obedience must not become enforcing it. A validator that
    quietly rewrote the model's stated confidence would be the same class of defect as
    averaging two providers' PE into a number neither reported."""
    conn = _store(tmp_path)
    _add(conn, confidence=80, ceiling=60)
    row = conn.execute("SELECT confidence, ceiling_at_create FROM insights").fetchone()
    assert (row["confidence"], row["ceiling_at_create"]) == (80, 60)


# --- the wire ------------------------------------------------------------------------


def test_ai_score_carries_the_block_at_top_level(tmp_path: object) -> None:
    conn = _store(tmp_path)
    _add(conn, confidence=80, ceiling=60)
    score = es.ai_score(conn, min_samples=8)
    assert score["ceiling_violations"] == {"n": 1, "violations": 1, "rate": "1.0000"}


# --- the generation path actually stamps it ------------------------------------------


def test_the_ceiling_is_read_from_the_payload_the_prompt_rendered() -> None:
    """Not re-derived: the SAME object the model was shown, so the two cannot disagree."""
    from portfolio_dash.llm_insight.generate import _ceiling_at_create

    assert _ceiling_at_create({"backtest_json": {"confidence_ceiling": 71}}) == 71


def test_an_unavailable_backtest_payload_records_no_ceiling() -> None:
    """Such a card was never governed by one — it must land NULL, not 0 (which would make
    every card a violation) and not 100 (which would make none)."""
    from portfolio_dash.llm_insight.generate import _ceiling_at_create

    assert _ceiling_at_create({"backtest_json": {"unavailable": True}}) is None
    assert _ceiling_at_create({}) is None
    assert _ceiling_at_create({"backtest_json": {"confidence_ceiling": None}}) is None
    # bool is an int subclass; a stray True must not read as ceiling 1.
    assert _ceiling_at_create({"backtest_json": {"confidence_ceiling": True}}) is None


def test_an_evaluations_only_connection_reads_as_empty_not_as_an_error() -> None:
    """architecture.md: a cross-table read degrades to "no rows", never OperationalError.
    ``ai_score`` is reachable on a connection that only ever ensured its own table."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    es.ensure_tables(conn)                      # deliberately NOT istore.ensure_tables
    assert es.ceiling_violations(conn) == {"n": 0, "violations": 0, "rate": None}
    assert es.ai_score(conn, min_samples=8)["ceiling_violations"]["rate"] is None
    conn.close()
