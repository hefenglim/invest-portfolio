"""Contract tests for the spec-07 Insight Pipeline Hub: route aliases + status API.

Spec 07 adds NO new business logic — it is the read-only observability layer over the
spec-04 insight machinery. This file covers §7.0 (the ``/api/insight-tasks/*`` full alias
of the ``/api/insight-types/*`` resource) and §7.1/7.1.1 (the converged task-status API +
its pure node-state derivation as seen through the API). Driven through the golden
TestClient (in-process, no network); no LLM is ever called by status/diagnose.
"""

import sqlite3

from fastapi.testclient import TestClient


def _make_combo(api_client: TestClient, *, scope: str = "portfolio") -> int:
    sp = api_client.post(
        "/api/strategy-prompts", json={"name": "S", "body": "{{kpis_json}}"}
    ).json()
    it = api_client.post(
        "/api/insight-types",
        json={"name": "Daily", "scope": scope, "strategy_ids": [sp["id"]]},
    ).json()
    return int(it["id"])


# --- §7.0 route aliases (full mirror, same resource) --------------------------


def test_insight_tasks_list_is_alias_of_insight_types(api_client: TestClient) -> None:
    _make_combo(api_client)
    types = api_client.get("/api/insight-types")
    tasks = api_client.get("/api/insight-tasks")
    assert types.status_code == 200
    assert tasks.status_code == 200
    # Same resource → identical payload.
    assert tasks.json() == types.json()


def test_insight_tasks_crud_reaches_same_resource(api_client: TestClient) -> None:
    sp = api_client.post(
        "/api/strategy-prompts", json={"name": "S", "body": "{{kpis_json}}"}
    ).json()
    # POST via the new alias.
    created = api_client.post(
        "/api/insight-tasks",
        json={"name": "ViaAlias", "scope": "portfolio", "strategy_ids": [sp["id"]]},
    )
    assert created.status_code == 200
    tid = created.json()["id"]
    # The old route sees the same row.
    via_old = api_client.get("/api/insight-types")
    assert any(t["id"] == tid and t["name"] == "ViaAlias" for t in via_old.json())
    # PUT via the alias updates the same row.
    put = api_client.put(
        f"/api/insight-tasks/{tid}",
        json={"name": "Renamed", "scope": "portfolio", "strategy_ids": [sp["id"]]},
    )
    assert put.status_code == 200
    assert put.json()["name"] == "Renamed"
    # DELETE via the alias archives the same row (drops out of the non-archived list).
    assert api_client.delete(f"/api/insight-tasks/{tid}").status_code == 200
    assert all(t["id"] != tid for t in api_client.get("/api/insight-types").json())


def test_insight_tasks_schedule_and_runs_aliases(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    tid = _make_combo(api_client)
    # schedule via the alias.
    sch = api_client.post(
        f"/api/insight-tasks/{tid}/schedule", json={"cron": "0 8 * * *"}
    )
    assert sch.status_code == 200
    assert sch.json()["job_id"] == f"insight:{tid}"
    # active-calibration via the alias (null is always valid).
    ac = api_client.put(
        f"/api/insight-tasks/{tid}/active-calibration", json={"version": None}
    )
    assert ac.status_code == 200
    # runs via the alias mirrors the old /runs query.
    golden_db.execute(
        "INSERT INTO job_runs (job_id, started_at, finished_at, status, reason, payload, "
        "cost_usd) VALUES (?, '2026-06-11T08:00:00+08:00', '2026-06-11T08:00:01+08:00', "
        "'skipped', 'R6_quota', ?, '0')",
        (f"insight:{tid}", str(tid)),
    )
    golden_db.commit()
    via_alias = api_client.get(f"/api/insight-tasks/{tid}/runs?limit=10")
    via_old = api_client.get(f"/api/insight-types/{tid}/runs?limit=10")
    assert via_alias.status_code == 200
    assert via_alias.json() == via_old.json()
    assert via_alias.json()["rows"][0]["reason"] == "R6_quota"


def test_old_insight_types_routes_still_work(api_client: TestClient) -> None:
    # The alias must not remove the old routes.
    tid = _make_combo(api_client)
    assert api_client.get("/api/insight-types").status_code == 200
    assert api_client.post(f"/api/insight-types/{tid}/run").status_code == 202


# --- §7.1 task-status API -----------------------------------------------------


def test_status_empty_db_returns_empty_tasks(api_client: TestClient) -> None:
    r = api_client.get("/api/insight-tasks/status")
    assert r.status_code == 200
    body = r.json()
    assert body["tasks"] == []
    assert "as_of" in body
    health = body["health"]
    # No master role bound and no top-ups in the golden DB → AI off, quota 0.
    assert health["master_ok"] is False
    assert health["quota_remaining"] == "0"  # Decimal STRING, never float
    assert health["last_batch"] is None


def test_status_lists_task_with_nodes_and_level(api_client: TestClient) -> None:
    tid = _make_combo(api_client, scope="portfolio")
    body = api_client.get("/api/insight-tasks/status").json()
    assert len(body["tasks"]) == 1
    task = body["tasks"][0]
    assert task["id"] == tid
    assert task["scope"] == "portfolio"
    assert task["enabled"] is True
    assert set(task["nodes"].keys()) == {"trigger", "input", "assemble", "exec", "output"}
    # Unscheduled + quota 0 → trigger warn, exec fail → aggregate fail.
    assert task["nodes"]["trigger"]["lv"] == "warn"
    assert task["nodes"]["exec"]["lv"] == "fail"
    assert task["level"] == "fail"
    assert task["last_run"] is None  # never run


def test_status_exec_node_reflects_quota(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    from decimal import Decimal

    from portfolio_dash.shared.llm_config import add_topup

    tid = _make_combo(api_client, scope="portfolio")
    add_topup(golden_db, Decimal("5"), note="test")
    task = next(
        t for t in api_client.get("/api/insight-tasks/status").json()["tasks"]
        if t["id"] == tid
    )
    assert task["nodes"]["exec"]["lv"] == "ok"  # quota 5 > quota_low 1


def test_status_last_run_and_last_batch(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    tid = _make_combo(api_client, scope="portfolio")
    # A finished non-shadow insight run + a card created in the same batch.
    golden_db.execute(
        "INSERT INTO job_runs (job_id, started_at, finished_at, status, detail, payload, "
        "cost_usd, is_shadow) VALUES (?, '2026-06-11T08:00:00+08:00', "
        "'2026-06-11T08:00:05+08:00', 'ok', 'done', ?, '0.094', 0)",
        (f"insight:{tid}", str(tid)),
    )
    golden_db.execute(
        "INSERT INTO insights (insight_type_id, symbol, is_shadow, calibration_version, "
        "fingerprint, title, summary, body_md, tags, confidence, prediction, horizon_days, "
        "due_at, input_snapshot, model, cost_usd, created_at) VALUES "
        "(?, NULL, 0, NULL, 'fp', 't', 's', 'b', '[]', NULL, NULL, 5, NULL, '{}', 'm', "
        "'0.094', '2026-06-11T08:00:00+08:00')",
        (tid,),
    )
    golden_db.commit()
    body = api_client.get("/api/insight-tasks/status").json()
    task = next(t for t in body["tasks"] if t["id"] == tid)
    assert task["last_run"]["status"] == "ok"
    assert task["last_run"]["at"] == "2026-06-11T08:00:05+08:00"
    last_batch = body["health"]["last_batch"]
    assert last_batch is not None
    assert last_batch["cost_usd"] == "0.094"  # Decimal STRING
    assert last_batch["cards"] == 1


def test_status_excludes_shadow_from_last_batch(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    tid = _make_combo(api_client, scope="portfolio")
    # Only a SHADOW run exists → it must NOT surface as last_batch (spec 04 fix #3).
    golden_db.execute(
        "INSERT INTO job_runs (job_id, started_at, finished_at, status, detail, payload, "
        "cost_usd, is_shadow) VALUES (?, '2026-06-11T08:00:00+08:00', "
        "'2026-06-11T08:00:05+08:00', 'ok', 'shadow', ?, '0.05', 1)",
        (f"insight:{tid}", str(tid)),
    )
    golden_db.commit()
    body = api_client.get("/api/insight-tasks/status").json()
    assert body["health"]["last_batch"] is None
    task = next(t for t in body["tasks"] if t["id"] == tid)
    assert task["last_run"] is None  # shadow excluded from the task last_run too
