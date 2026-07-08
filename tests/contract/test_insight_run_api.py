"""Contract tests for the manual insight-run + run-history + stored-cards API (spec 04.2/4.10).

The §4.2 ``/run`` mirrors spec-15: a 202 + run_id with the running row inserted on the
request connection (the bg thread opens its OWN throwaway session, so its completion is not
asserted here). ``/runs`` polls the kind=insight job_runs; ``/insights`` lists stored cards.
Uses the shared golden DB + frozen clock; no LLM (the bg daemon is hermetic).
"""

import sqlite3

from fastapi.testclient import TestClient


def _make_combo(api_client: TestClient) -> int:
    sp = api_client.post(
        "/api/strategy-prompts", json={"name": "S", "body": "{{kpis_json}}"}
    ).json()
    it = api_client.post(
        "/api/insight-types",
        json={"name": "Daily", "scope": "portfolio", "strategy_ids": [sp["id"]]},
    ).json()
    return int(it["id"])


def test_run_returns_202_and_inserts_running_row(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    it_id = _make_combo(api_client)
    r = api_client.post(f"/api/insight-types/{it_id}/run")
    assert r.status_code == 202
    body = r.json()
    assert body["insight_type_id"] == it_id
    assert isinstance(body["run_id"], int)
    row = golden_db.execute(
        "SELECT job_id, started_at, payload FROM job_runs WHERE id = ?", (body["run_id"],)
    ).fetchone()
    assert row is not None
    assert row["job_id"] == f"insight:{it_id}"
    assert row["payload"] == str(it_id)


def test_run_unknown_insight_type_404(api_client: TestClient) -> None:
    assert api_client.post("/api/insight-types/999/run").status_code == 404


def test_run_already_running_409(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    it_id = _make_combo(api_client)
    golden_db.execute(
        "INSERT INTO job_runs (job_id, started_at) VALUES (?, '2026-06-11T14:00:00+08:00')",
        (f"insight:{it_id}",),
    )
    golden_db.commit()
    r = api_client.post(f"/api/insight-types/{it_id}/run")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "already_running"


def test_run_disabled_task_409(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    # H2 fix (decision Q2a): a disabled task must not run (previously the flag was
    # display-only and the run generated + billed anyway).
    it_id = _make_combo(api_client)
    golden_db.execute("UPDATE insight_types SET enabled = 0 WHERE id = ?", (it_id,))
    golden_db.commit()
    r = api_client.post(f"/api/insight-types/{it_id}/run")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "task_disabled"
    assert "任務已停用" in r.json()["error"]["message"]


def test_run_archived_task_409(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    # H2 fix: get_insight_type returns archived rows — that's how an archived re-run
    # used to slip through the 404 check.
    it_id = _make_combo(api_client)
    assert api_client.delete(f"/api/insight-types/{it_id}").status_code == 200  # archive
    r = api_client.post(f"/api/insight-types/{it_id}/run")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "task_archived"
    assert "任務已刪除" in r.json()["error"]["message"]


def test_runs_list_returns_rows_with_reason(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    it_id = _make_combo(api_client)
    golden_db.execute(
        "INSERT INTO job_runs (job_id, started_at, finished_at, status, reason, payload, "
        "cost_usd) VALUES (?, '2026-06-11T08:00:00+08:00', '2026-06-11T08:00:01+08:00', "
        "'skipped', 'R6_quota', ?, '0')",
        (f"insight:{it_id}", str(it_id)),
    )
    golden_db.commit()
    r = api_client.get(f"/api/insight-types/{it_id}/runs?limit=10")
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert rows[0]["status"] == "skipped"
    assert rows[0]["reason"] == "R6_quota"
    assert rows[0]["insight_type_id"] == it_id


def test_runs_list_limit_over_max_400(api_client: TestClient) -> None:
    it_id = _make_combo(api_client)
    assert api_client.get(f"/api/insight-types/{it_id}/runs?limit=99999").status_code == 400


def test_insights_list_empty_returns_empty(api_client: TestClient) -> None:
    body = api_client.get("/api/insights").json()
    assert body["rows"] == [] and body["total_count"] == 0


def test_insights_list_returns_stored_card(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    # Insert a card directly via the store, then read it through the API.
    from datetime import datetime
    from decimal import Decimal
    from zoneinfo import ZoneInfo

    from portfolio_dash.llm_insight import insights_store as istore
    from portfolio_dash.llm_insight.cards import InsightCard, Prediction

    it_id = _make_combo(api_client)
    now = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))
    card = InsightCard(
        title="洞察", summary="s", body_md="b", tags=["TW"], symbol="2330", confidence=70,
        prediction=Prediction(
            metric="price_change", direction="up", target_pct=Decimal("0.05"),
            horizon_days=5,
        ),
    )
    istore.add_card(
        golden_db, insight_type_id=it_id, card=card,
        fingerprint=istore.fingerprint(it_id, "p", "d", "v1"), calibration_version=None,
        horizon_days=5, input_snapshot="{}", model="m", cost_usd=Decimal("0.001"), now=now,
    )
    body = api_client.get("/api/insights").json()
    rows = body["rows"]
    assert len(rows) == 1 and body["total_count"] == 1
    assert rows[0]["title"] == "洞察"
    assert rows[0]["prediction"]["target_pct"] == "0.05"  # Decimal STRING
    assert rows[0]["due_at"] is not None
    # filter by symbol
    assert len(api_client.get("/api/insights?symbol=2330").json()["rows"]) == 1
    assert api_client.get("/api/insights?symbol=NOPE").json()["rows"] == []
    # filter by insight_type
    assert len(api_client.get(f"/api/insights?insight_type={it_id}").json()["rows"]) == 1
    # scope filter (WPE): a 2330 card is per-symbol scope, not portfolio scope
    assert api_client.get("/api/insights?scope=symbol").json()["total_count"] == 1
    assert api_client.get("/api/insights?scope=portfolio").json()["total_count"] == 0
    assert api_client.get("/api/insights?scope=bogus").status_code == 400
    # symbol-grouped shape (WPE, 持倉健診): one group for 2330 with its card
    grouped = api_client.get("/api/insights?group=symbol&history_limit=5").json()
    assert grouped["total_count"] == 1
    assert grouped["groups"][0]["symbol"] == "2330"
    assert grouped["groups"][0]["total"] == 1
    assert len(grouped["groups"][0]["cards"]) == 1


def test_deleted_task_history_hidden_but_preserved(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    # Deleting a task is an ARCHIVE (spec 4.1) — its cards/evaluations must stop
    # surfacing on /api/insights, the dashboard embed, and /api/ai-score, while the
    # rows stay in the tables (2026-07-05 fix: orphan cards polluted all three).
    from datetime import datetime
    from decimal import Decimal
    from zoneinfo import ZoneInfo

    from portfolio_dash.llm_insight import evaluations_store as es
    from portfolio_dash.llm_insight import insights_store as istore
    from portfolio_dash.llm_insight.cards import InsightCard

    it_id = _make_combo(api_client)
    now = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))
    card = InsightCard(title="孤兒卡", summary="s", body_md="b", tags=[])
    card_id = istore.add_card(
        golden_db, insight_type_id=it_id, card=card,
        fingerprint=istore.fingerprint(it_id, "p2", "d2", "v1"), calibration_version=None,
        horizon_days=5, input_snapshot="{}", model="m", cost_usd=Decimal("0"), now=now,
    ).id
    es.ensure_tables(golden_db)
    es.add_evaluation(
        golden_db, insight_id=card_id, insight_type_id=it_id, calibration_version=None,
        is_shadow=False, status="scored", quant_hit=True, narrative_score=80,
        miss=False, actual_value=Decimal("0.01"), confidence=70, now=now,
    )
    assert len(api_client.get("/api/insights").json()["rows"]) == 1
    assert len(api_client.get("/api/ai-score").json()["rows"]) == 1

    assert api_client.delete(f"/api/insight-types/{it_id}").status_code == 200

    assert api_client.get("/api/insights").json()["rows"] == []
    score = api_client.get("/api/ai-score").json()
    assert score["rows"] == [] and score["totals"]["n"] == 0
    dash = api_client.get("/api/dashboard").json()
    assert all(c["title"] != "孤兒卡" for c in dash["insights"])
    # archive semantics: the history rows are still in the tables.
    n_cards = golden_db.execute(
        "SELECT COUNT(*) FROM insights WHERE insight_type_id = ?", (it_id,)
    ).fetchone()[0]
    n_evals = golden_db.execute(
        "SELECT COUNT(*) FROM insight_evaluations WHERE insight_type_id = ?", (it_id,)
    ).fetchone()[0]
    assert n_cards == 1 and n_evals == 1
