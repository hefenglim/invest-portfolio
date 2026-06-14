"""Loop-3 calibration generation + the weekly ``generate_calibrations`` job (spec 04.5/4.8).

The master LLM seam is monkeypatched. Covers: the §4.5 triggers, the min_samples gate, the
§4.8 validator rejecting越權/幣別混算, master-unset pausing the pipeline (no crash), and the
append-only version chain. The job dispatches the registered calibration runner.
"""

import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.api import insight_service
from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.llm_insight import evaluations_store as es
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

NOW = datetime(2026, 6, 14, 19, 0, tzinfo=ZoneInfo("Asia/Taipei"))


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
    jobs.create_scheduler_tables(c)
    cs.ensure_seeded(c)
    es.ensure_tables(c)
    ensure_llm_seeded(c)
    add_topup(c, Decimal("100"))
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _clear_runner() -> Iterator[None]:
    jobs.register_calibration_runner(None)
    yield
    jobs.register_calibration_runner(None)


def _self_correct_combo(conn: sqlite3.Connection) -> int:
    it = cs.create_insight_type(
        conn, name="SC", scope="per_symbol", self_correct=True, now=NOW
    )
    return it.id


def _seed_misses(conn: sqlite3.Connection, type_id: int, n: int, *, miss: bool) -> None:
    for i in range(n):
        es.add_evaluation(
            conn, insight_id=1000 + i, insight_type_id=type_id, calibration_version=1,
            is_shadow=False, status="scored", quant_hit=not miss,
            narrative_score=20 if miss else 80, miss=miss, actual_value=None,
            confidence=70, now=NOW, notes="高估" if miss else None,
        )


def _good_master(monkeypatch: pytest.MonkeyPatch) -> None:
    def completion(**kw: object) -> _Resp:
        msgs = kw["messages"]
        assert isinstance(msgs, list)
        joined = "".join(str(m.get("content")) for m in msgs)
        if "審查" in joined:  # validator review pass
            return _Resp('{"ok": true, "reasons": []}')
        return _Resp('{"body": "新版校正規則：修訂幅度高估條款", "cause": "連續高估失誤"}')

    monkeypatch.setattr(llm_mod.litellm, "completion", completion)


# --- trigger + min_samples gate ----------------------------------------------


def test_generate_creates_new_version_on_consecutive_misses(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _master(conn)
    _good_master(monkeypatch)
    tid = _self_correct_combo(conn)
    # min_samples default 8: seed 8 misses (≥3 consecutive + miss-rate high).
    _seed_misses(conn, tid, 8, miss=True)
    insight_service.generate_calibrations_for_all(conn, now=NOW)
    versions = cs.list_calibrations(conn, tid)
    assert len(versions) == 1
    assert versions[0].version == 1  # first appended version
    assert "校正" in versions[0].body
    assert versions[0].cause == "連續高估失誤"


def test_min_samples_not_met_is_noop(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _master(conn)
    _good_master(monkeypatch)
    tid = _self_correct_combo(conn)
    # only 3 resolved samples (< default min_samples 8) → no generation even if all miss.
    _seed_misses(conn, tid, 3, miss=True)
    insight_service.generate_calibrations_for_all(conn, now=NOW)
    assert cs.list_calibrations(conn, tid) == []


def test_no_trigger_is_noop(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _master(conn)
    _good_master(monkeypatch)
    tid = _self_correct_combo(conn)
    # 8 samples, all hits (no miss streak, low miss rate) → no trigger → no version.
    _seed_misses(conn, tid, 8, miss=False)
    insight_service.generate_calibrations_for_all(conn, now=NOW)
    assert cs.list_calibrations(conn, tid) == []


def test_non_self_correct_combo_skipped(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _master(conn)
    _good_master(monkeypatch)
    it = cs.create_insight_type(conn, name="plain", scope="per_symbol",
                                self_correct=False, now=NOW)
    _seed_misses(conn, it.id, 8, miss=True)
    insight_service.generate_calibrations_for_all(conn, now=NOW)
    assert cs.list_calibrations(conn, it.id) == []


# --- validator rejection (§4.8) ----------------------------------------------


def test_invalid_calibration_not_written(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _master(conn)
    # master returns a body containing a denylisted phrase → validator rejects → not written.
    monkeypatch.setattr(
        llm_mod.litellm, "completion",
        lambda **kw: _Resp('{"body": "建議立即加碼此標的", "cause": "x"}'),
    )
    tid = _self_correct_combo(conn)
    _seed_misses(conn, tid, 8, miss=True)
    insight_service.generate_calibrations_for_all(conn, now=NOW)
    assert cs.list_calibrations(conn, tid) == []  # rejected, no version appended


# --- master unset pauses pipeline (no crash) ---------------------------------


def test_master_unset_pauses_no_crash(conn: sqlite3.Connection) -> None:
    tid = _self_correct_combo(conn)
    _seed_misses(conn, tid, 8, miss=True)
    # No master role bound → generation pauses gracefully (no version, no exception).
    insight_service.generate_calibrations_for_all(conn, now=NOW)
    assert cs.list_calibrations(conn, tid) == []


# --- append-only chain --------------------------------------------------------


def test_second_run_appends_version(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _master(conn)
    _good_master(monkeypatch)
    tid = _self_correct_combo(conn)
    _seed_misses(conn, tid, 8, miss=True)
    insight_service.generate_calibrations_for_all(conn, now=NOW)
    # add another miss batch under version 1 + run again → appends version 2.
    for i in range(8):
        es.add_evaluation(
            conn, insight_id=2000 + i, insight_type_id=tid, calibration_version=1,
            is_shadow=False, status="scored", quant_hit=False, narrative_score=10,
            miss=True, actual_value=None, confidence=70, now=NOW, notes="再次高估",
        )
    insight_service.generate_calibrations_for_all(conn, now=NOW + timedelta(days=7))
    versions = cs.list_calibrations(conn, tid)
    assert [v.version for v in versions] == [1, 2]  # append-only


# --- scheduler job wiring -----------------------------------------------------


def test_generate_calibrations_job_registered() -> None:
    assert "generate_calibrations" in {j.id for j in jobs.JOBS}


def test_generate_calibrations_job_dispatches_runner(conn: sqlite3.Connection) -> None:
    calls: list[datetime] = []
    jobs.register_calibration_runner(lambda c, *, now: calls.append(now))
    detail = jobs.generate_calibrations(conn, now=NOW)
    assert calls == [NOW]
    assert isinstance(detail, str)


def test_generate_calibrations_job_no_runner_safe(conn: sqlite3.Connection) -> None:
    jobs.register_calibration_runner(None)
    detail = jobs.generate_calibrations(conn, now=NOW)
    assert isinstance(detail, str)
