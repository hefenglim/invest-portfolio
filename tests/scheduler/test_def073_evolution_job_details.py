"""DEF-073 (R6, evaluate/calibration half) — the two evolution jobs SAY what they did.

排程中心 printed 「evaluate pass complete」 after every evaluate_insights run and 「calibration
pass complete」 after every generate_calibrations run — whether the pass produced 0 versions
because the sample was below 門檻, had the one it wrote rejected by the validator, or produced
one — so the owner could not tell why no version appeared. The runners (registered by
``api/app.py``, never imported by ``scheduler/``) now return a summary whose ``str()`` is the
zh detail, and the two job wrappers print it.

The job wrappers are driven exactly as the scheduler drives them: the real runner registered
on the seam, then ``jobs.<job>(conn, now=...)``.
"""

import json
import re
import sqlite3
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.api import insight_service
from portfolio_dash.data_ingestion.store import upsert_instrument
from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.llm_insight import evaluations_store as es
from portfolio_dash.llm_insight import insights_store as istore
from portfolio_dash.llm_insight.cards import InsightCard, Prediction
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.scheduler import jobs
from portfolio_dash.shared import llm as llm_mod
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.llm_config import (
    LLMRole,
    ModelConfig,
    add_topup,
    ensure_llm_seeded,
    set_role,
    upsert_model,
)
from portfolio_dash.shared.models.assets import Instrument
from tests.conftest import init_golden_base

NOW = datetime(2026, 9, 13, 19, 0, tzinfo=ZoneInfo("Asia/Taipei"))


class _Resp:
    def __init__(self, content: str) -> None:
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]
        self.usage = type("U", (), {"prompt_tokens": 10, "completion_tokens": 5})()


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_golden_base(c)
    ensure_llm_seeded(c)
    add_topup(c, Decimal("100"))
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _wired() -> Iterator[None]:
    """The two runners as ``api/app.py`` registers them."""
    jobs.register_evaluation_runner(insight_service.evaluate_due)
    jobs.register_calibration_runner(insight_service.generate_calibrations_for_all)
    yield
    jobs.register_evaluation_runner(None)
    jobs.register_calibration_runner(None)


def _master(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    upsert_model(conn, ModelConfig(
        id="master", model_alias="master", provider="openai", model_name="master",
        input_price_per_mtok=Decimal("0"), output_price_per_mtok=Decimal("0"),
    ))
    set_role(conn, LLMRole.MASTER, "master")

    def completion(**kw: object) -> _Resp:
        msgs = kw["messages"]
        assert isinstance(msgs, list)
        if "審查" in "".join(str(m.get("content")) for m in msgs):
            return _Resp('{"ok": true, "reasons": []}')
        return _Resp(json.dumps({"body": body, "cause": "連續失誤"}, ensure_ascii=False))

    monkeypatch.setattr(llm_mod.litellm, "completion", completion)


def _task(conn: sqlite3.Connection, name: str = "個股健檢") -> int:
    return cs.create_insight_type(
        conn, name=name, scope="per_symbol", self_correct=True, now=NOW,
    ).id


def _evals(conn: sqlite3.Connection, tid: int, n: int, *, miss: bool,
           version: int | None = None, shadow: bool = False, first_id: int = 1000) -> None:
    for i in range(n):
        es.add_evaluation(
            conn, insight_id=first_id + i, insight_type_id=tid, calibration_version=version,
            is_shadow=shadow, status="scored", quant_hit=not miss, narrative_score=None,
            miss=miss, actual_value=None, confidence=70, now=NOW,
        )


# --- generate_calibrations ---------------------------------------------------------------


def test_below_the_sample_threshold_says_which_task_and_how_many(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _master(conn, monkeypatch, "新版校正規則")
    tid = _task(conn)
    _evals(conn, tid, 3, miss=True)
    assert jobs.generate_calibrations(conn, now=NOW) == (
        "產生 0 版；略過 1 個任務（個股健檢 新樣本 3／門檻 8）"
    )


def test_a_validator_rejection_names_the_reason(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _master(conn, monkeypatch, "建議立即加碼此標的")
    tid = _task(conn)
    _evals(conn, tid, 8, miss=True)
    assert jobs.generate_calibrations(conn, now=NOW) == (
        "產生 0 版；驗證器拒絕 1 版（個股健檢：越權/幣別混算關鍵字：加碼）"
    )
    assert cs.list_calibrations(conn, tid) == []


def test_a_produced_version_is_named(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _master(conn, monkeypatch, "新版校正規則")
    tid = _task(conn)
    _evals(conn, tid, 8, miss=True)
    other = _task(conn, "組合週報")
    _evals(conn, other, 8, miss=False, first_id=2000)  # enough samples, no trigger
    assert jobs.generate_calibrations(conn, now=NOW) == (
        "產生 1 版（個股健檢 v1・失誤樣本 8 筆）；1 個任務未達觸發條件"
    )


def test_master_unset_pauses_and_says_so(conn: sqlite3.Connection) -> None:
    tid = _task(conn)
    _evals(conn, tid, 8, miss=True)
    assert jobs.generate_calibrations(conn, now=NOW) == (
        "產生 0 版；未設定 AI 大師模型，暫停 1 個任務"
    )


def test_no_self_correct_task(conn: sqlite3.Connection) -> None:
    cs.create_insight_type(conn, name="一般", scope="portfolio", self_correct=False, now=NOW)
    assert jobs.generate_calibrations(conn, now=NOW) == "沒有開啟自我校正的任務，未產生校正版本"


# --- evaluate_insights ---------------------------------------------------------------------


def _due_card(conn: sqlite3.Connection, tid: int, symbol: str, *, days_ago: int) -> None:
    created = NOW - timedelta(days=days_ago)
    rec = istore.add_card(
        conn, insight_type_id=tid,
        card=InsightCard(
            title=symbol, summary="s", body_md="b", symbol=symbol, confidence=70,
            prediction=Prediction(metric="price_change", direction="up", horizon_days=1),
        ),
        fingerprint=f"fp-{symbol}", calibration_version=None, horizon_days=1,
        input_snapshot="x", model="m", cost_usd=Decimal("0"), now=created,
    )
    conn.execute("UPDATE insights SET due_at = ? WHERE id = ?",
                 ((NOW - timedelta(hours=1)).isoformat(), rec.id))
    conn.commit()


def test_evaluate_counts_scored_and_deferred(conn: sqlite3.Connection) -> None:
    tid = _task(conn)
    upsert_instrument(conn, Instrument(
        symbol="AAA", market=Market.US, quote_ccy=Currency.USD, sector="科技", name="AAA",
    ))
    upsert_prices(conn, [
        PriceRow(instrument="AAA", market=Market.US, as_of=date(2026, 9, 1) + timedelta(i),
                 close=Decimal(100 + 5 * i), source="test")
        for i in range(13)
    ], fetched_at=NOW)
    _due_card(conn, tid, "AAA", days_ago=5)   # priced → scored
    _due_card(conn, tid, "ZZZ", days_ago=5)   # no price at all → pending_data (deferred)
    assert jobs.evaluate_insights(conn, now=NOW) == "評分 1 張、延後 1 張；晉升：無"


@pytest.mark.parametrize("auto", [True, False])
def test_evaluate_names_the_promotion_or_the_waiting_winner(
    conn: sqlite3.Connection, auto: bool
) -> None:
    cs.set_evolution_config(
        conn, auto_promote=auto, shadow_batches=3, min_samples=8, max_shadows=2,
        gap_alert_pp=Decimal("15"),
    )
    tid = _task(conn, "組合甲")
    cs.create_calibration(conn, tid, body="v1", cause=None, now=NOW)
    cs.create_calibration(conn, tid, body="v2", cause=None, now=NOW)  # shadow: none active
    _evals(conn, tid, 3, miss=True)                                   # the shown (no layer)
    _evals(conn, tid, 3, miss=False, version=2, shadow=True, first_id=3000)
    detail = jobs.evaluate_insights(conn, now=NOW)
    if auto:
        assert detail == "評分 0 張、延後 0 張；晉升：組合甲 v2"
    else:
        assert detail == "評分 0 張、延後 0 張；勝出待設為生效：組合甲 v2"


def test_details_are_chinese_even_without_a_runner(conn: sqlite3.Connection) -> None:
    jobs.register_evaluation_runner(None)
    jobs.register_calibration_runner(None)
    for detail in (jobs.evaluate_insights(conn, now=NOW),
                   jobs.generate_calibrations(conn, now=NOW)):
        assert re.search(r"[A-Za-z]{2,}", detail) is None, detail
        assert re.search(r"[一-鿿]", detail), detail
