"""Loop-4 shadow generation + auto-promote + regression alert wiring (spec 04.6).

Drives ``insight_service`` against seeded prices/composer + a monkeypatched LLM seam (no
network). Covers: a shadow run producing a hidden is_shadow card alongside the active card,
the auto-promote step switching the active version, the manual-flag path, and the
calibration_regression info alert recorded via alerts_bridge.
"""

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.api import insight_service
from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.llm_insight import alerts_bridge as ab
from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.llm_insight import evaluations_store as es
from portfolio_dash.llm_insight import insights_store as istore
from portfolio_dash.pricing import datasources_store, snapshots_store
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.scheduler import jobs
from portfolio_dash.shared import llm as llm_mod
from portfolio_dash.shared.llm_config import (
    LLMRole,
    ModelConfig,
    add_topup,
    ensure_llm_seeded,
    set_role,
    upsert_model,
)

NOW = datetime(2026, 6, 14, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))


class _Resp:
    def __init__(self, content: str) -> None:
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]
        self.usage = type("U", (), {"prompt_tokens": 10, "completion_tokens": 5})()


_CARD_JSON = (
    '{"title": "t", "summary": "s", "body_md": "b", "tags": [], '
    '"confidence": 70, "prediction": {"metric": "price_change", "direction": "up", '
    '"target_pct": "0.03", "horizon_days": 5}}'
)


def _default_model(c: sqlite3.Connection) -> None:
    upsert_model(c, ModelConfig(
        id="def", model_alias="def", provider="openai", model_name="def",
        input_price_per_mtok=Decimal("0"), output_price_per_mtok=Decimal("0"),
    ))
    set_role(c, LLMRole.DEFAULT, "def")


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    create_pricing_tables(c)
    snapshots_store.ensure_tables(c)  # external_snapshots → external vars degrade gracefully
    datasources_store.ensure_seeded(c)  # data_sources (spec 14) read during ctx assembly
    jobs.create_scheduler_tables(c)
    cs.ensure_seeded(c)
    istore.ensure_tables(c)
    es.ensure_tables(c)
    ab.ensure_tables(c)
    ensure_llm_seeded(c)
    add_topup(c, Decimal("100"))
    yield c
    c.close()


def _combo_with_two_versions(conn: sqlite3.Connection) -> int:
    """A self_correct portfolio combo with a strategy + active v1 + a newer v2 (the shadow)."""
    sp = cs.create_strategy(conn, name="S", body="觀察組合走勢", now=NOW)
    it = cs.create_insight_type(
        conn, name="SC", scope="portfolio", self_correct=True, now=NOW
    )
    cs.set_strategies(conn, it.id, [(sp.id, 0)])
    cs.create_calibration(conn, it.id, body="v1 規則", cause=None, now=NOW)
    cs.create_calibration(conn, it.id, body="v2 規則", cause=None, now=NOW)
    cs.set_active_calibration(conn, it.id, 1)  # active=1, latest=2 → 2 is shadow
    return it.id


# --- shadow generation --------------------------------------------------------


def test_run_generates_active_and_shadow(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _default_model(conn)
    monkeypatch.setattr(llm_mod.litellm, "completion", lambda **kw: _Resp(_CARD_JSON))
    tid = _combo_with_two_versions(conn)

    insight_service.run_for_id(conn, tid, now=NOW)

    cards = istore.list_cards(conn, insight_type_id=tid)
    active_cards = [c for c in cards if not c.is_shadow]
    shadow_cards = [c for c in cards if c.is_shadow]
    assert len(active_cards) == 1
    assert active_cards[0].calibration_version == 1  # active version
    assert len(shadow_cards) == 1
    assert shadow_cards[0].calibration_version == 2  # shadow version


def test_no_shadow_when_active_is_latest(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _default_model(conn)
    monkeypatch.setattr(llm_mod.litellm, "completion", lambda **kw: _Resp(_CARD_JSON))
    it = cs.create_insight_type(conn, name="SC2", scope="portfolio", self_correct=True, now=NOW)
    cs.create_calibration(conn, it.id, body="v1", cause=None, now=NOW)
    cs.set_active_calibration(conn, it.id, 1)  # active == latest → no shadow

    insight_service.run_for_id(conn, it.id, now=NOW)
    cards = istore.list_cards(conn, insight_type_id=it.id)
    assert all(not c.is_shadow for c in cards)  # no shadow card


def test_on_alert_no_shadow_when_disabled(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _default_model(conn)
    monkeypatch.setattr(llm_mod.litellm, "completion", lambda **kw: _Resp(_CARD_JSON))
    it = cs.create_insight_type(
        conn, name="alert", scope="on_alert", self_correct=True,
        alert_rules=["fx_drift"], enabled=True, now=NOW
    )
    cs.create_calibration(conn, it.id, body="v1", cause=None, now=NOW)
    cs.create_calibration(conn, it.id, body="v2", cause=None, now=NOW)
    cs.set_active_calibration(conn, it.id, 1)
    # shadow_on_alert defaults to False → an on_alert run produces no shadow.
    insight_service.run_for_id(conn, it.id, now=NOW, fired_rule="fx_drift", fired_symbol=None)
    cards = istore.list_cards(conn, insight_type_id=it.id)
    assert all(not c.is_shadow for c in cards)


# --- auto-promote -------------------------------------------------------------


def test_promote_step_auto_switches_active(conn: sqlite3.Connection) -> None:
    tid = _combo_with_two_versions(conn)
    # enable auto_promote.
    cs.set_evolution_config(
        conn, auto_promote=True, shadow_batches=3, min_samples=8, max_shadows=2,
        gap_alert_pp=Decimal("10"),
    )
    # active v1: 3 evals, 2 miss. shadow v2: 3 evals, 0 miss → shadow wins.
    for i in range(3):
        es.add_evaluation(conn, insight_id=10 + i, insight_type_id=tid, calibration_version=1,
                          is_shadow=False, status="scored", quant_hit=(i == 0),
                          narrative_score=50, miss=(i != 0), actual_value=None,
                          confidence=70, now=NOW)
    for i in range(3):
        es.add_evaluation(conn, insight_id=20 + i, insight_type_id=tid, calibration_version=2,
                          is_shadow=True, status="scored", quant_hit=True,
                          narrative_score=90, miss=False, actual_value=None,
                          confidence=70, now=NOW)

    promoted = insight_service.promote_and_check(conn, now=NOW)

    assert tid in promoted
    it = cs.get_insight_type(conn, tid)
    assert it is not None
    assert it.active_calibration_version == 2  # auto-promoted to the shadow


def test_promote_step_holds_when_auto_promote_off(conn: sqlite3.Connection) -> None:
    tid = _combo_with_two_versions(conn)
    # auto_promote defaults False; even a winning shadow only flags (no switch).
    for i in range(3):
        es.add_evaluation(conn, insight_id=10 + i, insight_type_id=tid, calibration_version=1,
                          is_shadow=False, status="scored", quant_hit=False,
                          narrative_score=20, miss=True, actual_value=None,
                          confidence=70, now=NOW)
    for i in range(3):
        es.add_evaluation(conn, insight_id=20 + i, insight_type_id=tid, calibration_version=2,
                          is_shadow=True, status="scored", quant_hit=True,
                          narrative_score=90, miss=False, actual_value=None,
                          confidence=70, now=NOW)
    insight_service.promote_and_check(conn, now=NOW)
    it = cs.get_insight_type(conn, tid)
    assert it is not None
    assert it.active_calibration_version == 1  # unchanged (manual switch required)


# --- regression alert ---------------------------------------------------------


def test_regression_emits_calibration_regression(conn: sqlite3.Connection) -> None:
    it = cs.create_insight_type(conn, name="reg", scope="portfolio", self_correct=True, now=NOW)
    cs.create_calibration(conn, it.id, body="v1", cause=None, now=NOW)
    cs.set_active_calibration(conn, it.id, 1)
    # 8 active evals, recent skewed to misses → regression vs the early baseline.
    # earliest 4 are hits, latest 4 are misses (recent worsens).
    for i in range(4):
        es.add_evaluation(conn, insight_id=100 + i, insight_type_id=it.id,
                          calibration_version=1, is_shadow=False, status="scored",
                          quant_hit=True, narrative_score=80, miss=False, actual_value=None,
                          confidence=70, now=NOW)
    for i in range(4):
        es.add_evaluation(conn, insight_id=200 + i, insight_type_id=it.id,
                          calibration_version=1, is_shadow=False, status="scored",
                          quant_hit=False, narrative_score=10, miss=True, actual_value=None,
                          confidence=70, now=NOW)

    insight_service.promote_and_check(conn, now=NOW)

    events = ab.unconsumed_events(conn)
    assert any(e.rule_id == "calibration_regression" for e in events)
