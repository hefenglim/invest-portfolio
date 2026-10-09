"""DEF-081 (owner ruling 2026-09-26): the AI 戰績 tab names a task by its NAME.

「各洞察任務命中率」 and 預測明細 printed 「任務 #2」 — the id, which nothing else on the page
shows, so the owner could not tell which task a hit rate belonged to (verifier R5 observation
5, R6 §2 observation 3). ``GET /api/ai-score`` now carries ``insight_type_label`` on every
``by_combo`` entry and every ``rows`` item, in the 排程中心's three cases
(``api/routers/scheduler.py::_insight_label``) without its 「AI 洞察任務」 prefix: the name;
「名稱（已刪除）」 for an archived task; 「任務 #N（已刪除）」 when the task row is gone.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal

from fastapi.testclient import TestClient

from portfolio_dash.api.routers.insights import _task_label
from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.llm_insight import evaluations_store as es

_NOW = datetime(2026, 6, 11, 14, 30)


def _scored(conn: sqlite3.Connection, insight_id: int, type_id: int) -> None:
    es.add_evaluation(conn, insight_id=insight_id, insight_type_id=type_id,
                      calibration_version=None, is_shadow=False, status="scored",
                      quant_hit=True, narrative_score=80, miss=False,
                      actual_value=Decimal("0.01"), confidence=70, now=_NOW)


def test_every_combo_and_row_carries_the_task_name(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    cs.ensure_seeded(golden_db)
    es.ensure_tables(golden_db)
    task = cs.create_insight_type(golden_db, name="個股健檢（測試）", scope="per_symbol",
                                  now=_NOW)
    gone_id = task.id + 500                     # scored rows whose task row does not exist
    _scored(golden_db, 1, task.id)
    _scored(golden_db, 2, task.id)
    _scored(golden_db, 3, gone_id)
    golden_db.commit()

    body = api_client.get("/api/ai-score").json()
    combos = {c["insight_type_id"]: c["insight_type_label"] for c in body["by_combo"]}
    assert combos == {task.id: "個股健檢（測試）", gone_id: f"任務 #{gone_id}（已刪除）"}
    rows = {(r["insight_id"], r["insight_type_label"]) for r in body["rows"]}
    assert rows == {(1, "個股健檢（測試）"), (2, "個股健檢（測試）"),
                    (3, f"任務 #{gone_id}（已刪除）")}
    assert all("insight_type_id" in r for r in body["rows"])   # additive: the id stays


def test_the_label_has_the_scheduler_centres_three_cases(golden_db: sqlite3.Connection) -> None:
    cs.ensure_seeded(golden_db)
    live = cs.create_insight_type(golden_db, name="市場週報", scope="portfolio", now=_NOW)
    old = cs.create_insight_type(golden_db, name="舊任務", scope="portfolio", now=_NOW)
    cs.delete_insight_type(golden_db, old.id, now=_NOW)
    tasks = {it.id: it for it in cs.list_insight_types(golden_db, include_archived=True)}
    assert _task_label(live.id, tasks) == "市場週報"
    assert _task_label(old.id, tasks) == "舊任務（已刪除）"
    assert _task_label(9999, tasks) == "任務 #9999（已刪除）"
