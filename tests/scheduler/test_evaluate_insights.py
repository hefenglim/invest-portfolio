"""Loop-2 evaluate pipeline + the daily ``evaluate_insights`` job (spec 04.4 / 4.10).

The price-read seam (``insight_service``) is driven against seeded prices; the master LLM
seam is monkeypatched (no network). Covers: quant scoring, the pending_data anti-poison
(missing actual → defer, never miss; defer cap → undetermined), and master-unavailable
degradation (quant-only scored). The scheduler job is registered + dispatched via the
registered evaluation runner (no scheduler→api import).
"""

import sqlite3
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.api import insight_service
from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.llm_insight import evaluations_store as es
from portfolio_dash.llm_insight import insights_store as istore
from portfolio_dash.llm_insight.cards import InsightCard, Prediction
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.scheduler import jobs
from portfolio_dash.shared import llm as llm_mod
from portfolio_dash.shared.enums import Market
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


def _master(c: sqlite3.Connection) -> None:
    upsert_model(c, ModelConfig(
        id="master", model_alias="master", provider="openai", model_name="master",
        input_price_per_mtok=Decimal("1"), output_price_per_mtok=Decimal("2"),
    ))
    set_role(c, LLMRole.MASTER, "master")


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    create_pricing_tables(c)
    jobs.create_scheduler_tables(c)
    cs.ensure_seeded(c)
    istore.ensure_tables(c)
    es.ensure_tables(c)
    ensure_llm_seeded(c)
    add_topup(c, Decimal("100"))
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _clear_runner() -> Iterator[None]:
    jobs.register_evaluation_runner(None)
    yield
    jobs.register_evaluation_runner(None)


def _add_due_card(
    conn: sqlite3.Connection, *, symbol: str, prediction: Prediction | None,
    created: datetime, due: datetime,
) -> int:
    card = InsightCard(
        title=f"{symbol}", summary="s", body_md="b", tags=[], symbol=symbol,
        confidence=70 if prediction is not None else None, prediction=prediction,
    )
    rec = istore.add_card(
        conn, insight_type_id=10, card=card, fingerprint=f"fp-{symbol}-{due.isoformat()}",
        calibration_version=1, horizon_days=5, input_snapshot=f"snap-{symbol}",
        model="default", cost_usd=Decimal("0"), now=created,
    )
    conn.execute("UPDATE insights SET due_at = ? WHERE id = ?", (due.isoformat(), rec.id))
    conn.commit()
    return rec.id


def _prices(conn: sqlite3.Connection, symbol: str, points: list[tuple[date, str]]) -> None:
    upsert_prices(conn, [
        PriceRow(instrument=symbol, market=Market.US, as_of=d, close=Decimal(c),
                 source="test")
        for d, c in points
    ], fetched_at=NOW)


# --- quant scoring: price_change hit, master narrative scored ------------------


def test_evaluate_price_change_hit_scored(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _master(conn)
    monkeypatch.setattr(
        llm_mod.litellm, "completion",
        lambda **kw: _Resp('{"narrative_score": 85, "miss": false, "note": "good"}'),
    )
    created = NOW - timedelta(days=10)
    pred = Prediction(metric="price_change", direction="up", target_pct=Decimal("0.03"),
                      horizon_days=5)
    insight_id = _add_due_card(conn, symbol="AAPL", prediction=pred,
                               created=created, due=NOW - timedelta(days=1))
    # +5% move create→due → hits the +3% up target.
    _prices(conn, "AAPL", [(created.date(), "100"), ((NOW - timedelta(days=1)).date(), "105")])

    insight_service.evaluate_due(conn, now=NOW)

    ev = es.latest_for_insight(conn, insight_id)
    assert ev is not None
    assert ev.status == "scored"
    assert ev.quant_hit is True
    assert ev.narrative_score == 85
    assert ev.miss is False


# --- pending_data anti-poison: missing actual → defer, NEVER miss --------------


def test_evaluate_missing_price_defers_pending(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _master(conn)
    monkeypatch.setattr(
        llm_mod.litellm, "completion",
        lambda **kw: _Resp('{"narrative_score": 50, "miss": true, "note": "n"}'),
    )
    created = NOW - timedelta(days=10)
    pred = Prediction(metric="price_change", direction="up", horizon_days=5)
    insight_id = _add_due_card(conn, symbol="HALT", prediction=pred,
                               created=created, due=NOW - timedelta(days=1))
    # No prices for HALT → actual unavailable → pending_data, not a miss.
    insight_service.evaluate_due(conn, now=NOW)
    ev = es.latest_for_insight(conn, insight_id)
    assert ev is not None
    assert ev.status == "pending_data"
    assert ev.miss is False
    assert ev.defer_count == 1


def test_evaluate_defer_cap_becomes_undetermined(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _master(conn)
    monkeypatch.setattr(llm_mod.litellm, "completion",
                        lambda **kw: _Resp('{"narrative_score": 1, "miss": true, "note": "n"}'))
    # defer_limit_days defaults to 5; pre-load 5 pending rows so the next miss tips it over.
    created = NOW - timedelta(days=20)
    pred = Prediction(metric="price_change", direction="up", horizon_days=5)
    insight_id = _add_due_card(conn, symbol="HALT2", prediction=pred,
                               created=created, due=NOW - timedelta(days=10))
    for _ in range(5):
        es.bump_defer(conn, insight_id=insight_id, insight_type_id=10)
    assert es.latest_for_insight(conn, insight_id).defer_count == 5  # type: ignore[union-attr]
    # still no price → evaluate again; defer_count would become 6 > limit 5 → undetermined.
    insight_service.evaluate_due(conn, now=NOW)
    ev = es.latest_for_insight(conn, insight_id)
    assert ev is not None
    assert ev.status == "undetermined"
    assert ev.miss is False  # undetermined is NEVER a miss


# --- master unavailable → quant-only scored (graceful degrade) -----------------


def test_evaluate_master_unset_quant_only(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No master role bound; the LLM seam (if reached) would error — it must not be reached.
    def boom(**kw: object) -> _Resp:
        raise AssertionError("master must not be called when unset")

    monkeypatch.setattr(llm_mod.litellm, "completion", boom)
    created = NOW - timedelta(days=10)
    pred = Prediction(metric="price_change", direction="up", horizon_days=5)
    insight_id = _add_due_card(conn, symbol="MSFT", prediction=pred,
                               created=created, due=NOW - timedelta(days=1))
    _prices(conn, "MSFT", [(created.date(), "100"), ((NOW - timedelta(days=1)).date(), "110")])
    insight_service.evaluate_due(conn, now=NOW)
    ev = es.latest_for_insight(conn, insight_id)
    assert ev is not None
    assert ev.status == "scored"
    assert ev.quant_hit is True  # quant-only
    assert ev.narrative_score is None  # narrative skipped (master unset)
    assert ev.miss is False  # quant hit → not a miss


# --- scheduler job wiring -----------------------------------------------------


def test_evaluate_insights_job_registered() -> None:
    assert "evaluate_insights" in {j.id for j in jobs.JOBS}


def test_evaluate_insights_job_dispatches_runner(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[datetime] = []

    def runner(c: sqlite3.Connection, *, now: datetime) -> None:
        calls.append(now)

    jobs.register_evaluation_runner(runner)
    detail = jobs.evaluate_insights(conn, now=NOW)
    assert calls == [NOW]
    assert isinstance(detail, str)


def test_evaluate_insights_job_no_runner_is_safe(conn: sqlite3.Connection) -> None:
    jobs.register_evaluation_runner(None)
    # No runner wired (scheduler-only process) → no crash, returns a summary.
    detail = jobs.evaluate_insights(conn, now=NOW)
    assert isinstance(detail, str)
