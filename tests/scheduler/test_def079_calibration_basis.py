"""DEF-079 (R6) — a calibration version is written from the ACTIVE version's own failures.

Loop 3 (spec 04 §4.5) hands the master 「現行生效版 body、失誤樣本明細、分桶命中率」. It did
neither half right:

* the samples were read with ``version = active[-1].version if active else 1`` — for a task
  with no version yet that is version 1, while every card (and so every evaluation) of such a
  task carries ``calibration_version`` NULL: the FIRST version was generated from ZERO miss
  samples (measured: 8 misses → 0 samples in the prompt);
* the body was ``active[-1].body`` — the LATEST version, which may be a shadow nobody adopted
  (or one that lost its evaluation), not the version the cards are actually generated with.

The existing tests could not see it: ``test_generate_calibrations._seed_misses`` stamped its
evaluations ``calibration_version=1`` on a task with no version at all — a row real data never
contains. The rows here are what Loop 2 writes: a real card, and an evaluation carrying that
card's own ``calibration_version`` and lane, evaluated when the card matured.
"""

import json
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.api import insight_service
from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.llm_insight import evaluations_store as es
from portfolio_dash.llm_insight import insights_store as istore
from portfolio_dash.llm_insight.cards import InsightCard, Prediction
from portfolio_dash.shared import llm as llm_mod
from portfolio_dash.shared.llm_config import (
    LLMRole,
    ModelConfig,
    add_topup,
    ensure_llm_seeded,
    set_role,
    upsert_model,
)
from tests.conftest import init_golden_base

T0 = datetime(2026, 9, 1, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))
SUNDAY = datetime(2026, 9, 13, 19, 0, tzinfo=ZoneInfo("Asia/Taipei"))


class _Resp:
    def __init__(self, content: str) -> None:
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]
        self.usage = type("U", (), {"prompt_tokens": 10, "completion_tokens": 5})()


def make_conn() -> sqlite3.Connection:
    """The full empty schema + a master model bound (shared with the DEF-080 tests)."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_golden_base(c)
    ensure_llm_seeded(c)
    add_topup(c, Decimal("100"))
    upsert_model(c, ModelConfig(
        id="master", model_alias="master", provider="openai", model_name="master",
        input_price_per_mtok=Decimal("0"), output_price_per_mtok=Decimal("0"),
    ))
    set_role(c, LLMRole.MASTER, "master")
    return c


def install_fake_master(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every calibration-generation prompt the master received (the review pass excluded)."""
    seen: list[str] = []

    def completion(**kw: Any) -> _Resp:
        text = "".join(str(m.get("content")) for m in kw["messages"])
        if "審查" in text:
            return _Resp('{"ok": true, "reasons": []}')
        seen.append(text)
        return _Resp(json.dumps({"body": "新版校正規則", "cause": "連續失誤"}, ensure_ascii=False))

    monkeypatch.setattr(llm_mod.litellm, "completion", completion)
    return seen


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = make_conn()
    yield c
    c.close()


@pytest.fixture
def prompts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    return install_fake_master(monkeypatch)


def scored_card(
    conn: sqlite3.Connection, tid: int, title: str, *, version: int | None, miss: bool,
    evaluated: datetime, shadow: bool = False,
) -> int:
    """One card as Loop 1 stores it + its evaluation as Loop 2 writes it (same version/lane)."""
    rec = istore.add_card(
        conn, insight_type_id=tid,
        card=InsightCard(
            title=title, summary=f"{title} 主張", body_md="b", symbol="2330", confidence=70,
            prediction=Prediction(metric="price_change", direction="up", horizon_days=5),
        ),
        fingerprint=f"fp-{title}", calibration_version=version, horizon_days=5,
        input_snapshot="x", model="m", cost_usd=Decimal("0"),
        now=evaluated - timedelta(days=7), is_shadow=shadow,
    )
    es.add_evaluation(
        conn, insight_id=rec.id, insight_type_id=tid, calibration_version=version,
        is_shadow=shadow, status="scored", quant_hit=not miss, narrative_score=None,
        miss=miss, actual_value=None, confidence=70, now=evaluated, notes="高估" if miss else None,
    )
    return rec.id


def _task(conn: sqlite3.Connection) -> int:
    return cs.create_insight_type(
        conn, name="個股健檢", scope="per_symbol", self_correct=True, now=T0,
    ).id


def test_the_first_version_is_written_from_the_uncalibrated_cards_failures(
    conn: sqlite3.Connection, prompts: list[str]
) -> None:
    tid = _task(conn)
    for i in range(8):  # no version yet: every card and evaluation carries NULL
        scored_card(conn, tid, f"未校正失誤{i}", version=None, miss=True,
                    evaluated=T0 + timedelta(days=1, hours=i))

    summary = insight_service.generate_calibrations_for_all(conn, now=SUNDAY)

    assert [c.version for c in cs.list_calibrations(conn, tid)] == [1]
    (prompt,) = prompts
    for i in range(8):
        assert f"「未校正失誤{i}」" in prompt, f"sample {i} never reached the master"
    assert "（無失誤樣本）" not in prompt
    assert str(summary) == "產生 1 版（個股健檢 v1・失誤樣本 8 筆）"


def test_a_later_version_builds_on_the_active_version_not_the_latest(
    conn: sqlite3.Connection, prompts: list[str]
) -> None:
    tid = _task(conn)
    cs.create_calibration(conn, tid, body="BASE-ONE 生效中的規則", cause=None, now=T0)
    cs.create_calibration(conn, tid, body="BASE-TWO 未採用的規則", cause=None, now=T0)
    cs.set_active_calibration(conn, tid, 1)
    day = T0 + timedelta(days=1)
    # v2 was shadow-evaluated and LOST (3 of 3 missed) — its body must not be the base
    for i in range(3):
        scored_card(conn, tid, f"影子v2失誤{i}", version=2, miss=True, shadow=True,
                    evaluated=day + timedelta(hours=i))
    # v1, the version the cards are generated with: 5 of 8 missed, the last 3 in a row
    for i in range(8):
        scored_card(conn, tid, f"生效v1卡{i}", version=1, miss=i != 0 and i != 2 and i != 4,
                    evaluated=day + timedelta(hours=10 + i))
    it = cs.get_insight_type(conn, tid)
    assert it is not None
    assert insight_service.shadow_state(conn, it).phase == "lost"

    insight_service.generate_calibrations_for_all(conn, now=SUNDAY)

    assert [c.version for c in cs.list_calibrations(conn, tid)] == [1, 2, 3]
    (prompt,) = prompts
    assert "BASE-ONE" in prompt and "BASE-TWO" not in prompt
    for i in (1, 3, 5, 6, 7):  # exactly the ACTIVE version's misses
        assert f"「生效v1卡{i}」" in prompt
    assert "影子v2失誤" not in prompt
    assert "「生效v1卡0」" not in prompt  # a hit is not a miss sample
