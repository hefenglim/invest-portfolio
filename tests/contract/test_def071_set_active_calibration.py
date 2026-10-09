"""DEF-071 (R6) — 「設為生效」 / 「取消生效」 exist, and what they set is what the next batch uses.

The verifier found no way to adopt a calibration version from the page: the drawer's ④ 校正版本
鏈 offered only 「封存」, ``PUT …/active-calibration`` had no caller (it sat on the
no-frontend-caller allowlist with the reason 「the newest live one is active by default」 —
which was never true: with no active version the shown cards carry NO calibration layer), and
with auto_promote off (the default) neither a lone v1 nor a winning shadow could be adopted.

The page half is ``tests/e2e/test_def069_def071_shadow_ui_flow.py``. This file pins the API
half the page relies on: the chain says which version is active and where the shadow stands,
an archived version is refused in Chinese, and — the part a UI test cannot see — the NEXT
batch's cards carry the version that was set (and carry none after 取消生效).
"""

import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.api import insight_service
from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.llm_insight import evaluations_store as es
from portfolio_dash.shared import llm as llm_mod
from portfolio_dash.shared.llm_config import (
    LLMRole,
    ModelConfig,
    add_topup,
    ensure_llm_seeded,
    set_role,
    upsert_model,
)

NOW = datetime(2026, 9, 21, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))


class _Resp:
    def __init__(self, content: str) -> None:
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]
        self.usage = type("U", (), {"prompt_tokens": 10, "completion_tokens": 5})()


def _task(api_client: TestClient, golden_db: sqlite3.Connection) -> int:
    sp = api_client.post(
        "/api/strategy-prompts", json={"name": "S", "body": "{{kpis_json}}"}
    ).json()
    it = api_client.post("/api/insight-tasks", json={
        "name": "組合週報", "scope": "portfolio", "strategy_ids": [sp["id"]],
        "self_correct": True,
    }).json()
    tid = int(it["id"])
    cs.create_calibration(golden_db, tid, body="CAL-ONE 第一版規則", cause="seed", now=NOW)
    cs.create_calibration(golden_db, tid, body="CAL-TWO 第二版規則", cause="miss", now=NOW)
    return tid


def _chain(api_client: TestClient, tid: int) -> dict[int, dict[str, Any]]:
    return {c["version"]: c for c in api_client.get(f"/api/calibrations?insight_type={tid}").json()}


def test_the_chain_marks_the_active_version_and_the_shadow(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    tid = _task(api_client, golden_db)
    chain = _chain(api_client, tid)
    assert [chain[v]["is_active"] for v in (1, 2)] == [False, False]
    assert chain[1]["shadow"] is None
    assert chain[2]["shadow"]["phase"] == "waiting"  # v2 shadows from the next batch
    assert chain[2]["shadow"]["needed"] == cs.get_evolution_config(golden_db)["shadow_batches"]

    # v2 wins on its own record: 5 shadow hits vs the no-layer cards' 5 misses.
    for i in range(5):
        es.add_evaluation(golden_db, insight_id=900 + i, insight_type_id=tid,
                          calibration_version=None, is_shadow=False, status="scored",
                          quant_hit=False, narrative_score=None, miss=True, actual_value=None,
                          confidence=70, now=NOW)
        es.add_evaluation(golden_db, insight_id=950 + i, insight_type_id=tid,
                          calibration_version=2, is_shadow=True, status="scored",
                          quant_hit=True, narrative_score=None, miss=False, actual_value=None,
                          confidence=70, now=NOW)
    won = _chain(api_client, tid)[2]["shadow"]
    assert won["phase"] == "won"
    assert won["shadow_record"] == {"n": 5, "miss_count": 0}
    assert won["active_record"] == {"n": 5, "miss_count": 5}

    r = api_client.put(f"/api/insight-tasks/{tid}/active-calibration", json={"version": 2})
    assert r.status_code == 200
    chain = _chain(api_client, tid)
    assert (chain[1]["is_active"], chain[2]["is_active"]) == (False, True)
    assert chain[2]["shadow"] is None  # active == latest: no shadow left


def test_an_archived_version_is_refused_in_chinese(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    tid = _task(api_client, golden_db)
    v1 = next(c for c in cs.list_calibrations(golden_db, tid) if c.version == 1)
    cs.archive_calibration(golden_db, v1.id)
    r = api_client.put(f"/api/insight-tasks/{tid}/active-calibration", json={"version": 1})
    assert r.status_code == 400
    assert r.json()["error"]["message"] == "校正版本 v1 已封存，不能設為生效"
    r = api_client.put(f"/api/insight-tasks/{tid}/active-calibration", json={"version": 9})
    assert r.status_code == 400
    assert r.json()["error"]["message"] == "該洞察組合無校正版本 9"


def test_the_next_batch_carries_the_version_set_and_none_after_cancel(
    api_client: TestClient, golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_llm_seeded(golden_db)
    add_topup(golden_db, Decimal("100"))
    upsert_model(golden_db, ModelConfig(
        id="def", model_alias="def", provider="openai", model_name="def",
        input_price_per_mtok=Decimal("0"), output_price_per_mtok=Decimal("0"),
    ))
    set_role(golden_db, LLMRole.DEFAULT, "def")
    prompts: list[str] = []

    def completion(**kw: Any) -> _Resp:
        prompts.append("".join(str(m.get("content")) for m in kw["messages"]))
        return _Resp(json.dumps({"title": "t", "summary": "s", "body_md": "b", "tags": []}))

    monkeypatch.setattr(llm_mod.litellm, "completion", completion)
    tid = _task(api_client, golden_db)
    # prompts[0] is always the SHOWN card's call: the shadow lane (v2, the latest) runs
    # after it in the same batch and is asserted on through the stored rows' is_shadow.

    assert api_client.put(
        f"/api/insight-tasks/{tid}/active-calibration", json={"version": 1}
    ).status_code == 200
    insight_service.run_for_id(golden_db, tid, now=NOW)
    shown = golden_db.execute(
        "SELECT calibration_version FROM insights WHERE insight_type_id = ? AND is_shadow = 0",
        (tid,),
    ).fetchall()
    assert [r["calibration_version"] for r in shown] == [1]
    assert "CAL-ONE" in prompts[0] and "CAL-TWO" not in prompts[0]

    assert api_client.put(
        f"/api/insight-tasks/{tid}/active-calibration", json={"version": None}
    ).status_code == 200
    prompts.clear()
    insight_service.run_for_id(golden_db, tid, now=NOW.replace(day=22))
    newest = golden_db.execute(
        "SELECT calibration_version FROM insights WHERE insight_type_id = ? AND is_shadow = 0 "
        "ORDER BY id DESC LIMIT 1", (tid,),
    ).fetchone()
    assert newest["calibration_version"] is None
    assert "CAL-ONE" not in prompts[0] and "CAL-TWO" not in prompts[0]
