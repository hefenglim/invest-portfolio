"""Contract tests for the insight-composer API (spec 04.7 / 4.9 R1 / 4.2 / 4.6).

Self-contained: a local in-memory connection seeded with the composer + scheduler
tables, and a local FastAPI app mounting ONLY the insights router. No LLM, no money.
R1 (scope×variable-scope mismatch) reuses ``variables.validate_tokens`` — a
``portfolio``-scope insight_type whose strategy body uses a ``per_symbol`` variable is
rejected at create/update with 422.
"""

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest_socket import disable_socket, enable_socket

from portfolio_dash.api.deps import get_conn, get_now
from portfolio_dash.api.errors import register_error_handlers
from portfolio_dash.api.routers import insights as insights_router
from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.scheduler.jobs import bind_insight_schedule, create_scheduler_tables

NOW = datetime(2026, 6, 14, 10, 0, tzinfo=ZoneInfo("Asia/Taipei"))


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    cs.ensure_seeded(c)
    create_scheduler_tables(c)
    yield c
    c.close()


@pytest.fixture
def client(conn: sqlite3.Connection) -> Iterator[TestClient]:
    enable_socket()
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(insights_router.router, prefix="/api")
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_now] = lambda: NOW
    test_client = TestClient(app)
    try:
        yield test_client
    finally:
        app.dependency_overrides.clear()
        disable_socket(allow_unix_socket=True)


# --- strategy-prompts CRUD ----------------------------------------------------


def test_strategy_crud_lifecycle(client: TestClient) -> None:
    # empty list
    assert client.get("/api/strategy-prompts").json() == []
    # create
    r = client.post("/api/strategy-prompts", json={"name": "Mom", "body": "{{kpis_json}}"})
    assert r.status_code == 200
    sp = r.json()
    sid = sp["id"]
    assert sp["name"] == "Mom"
    assert sp["enabled"] is True
    assert sp["archived"] is False
    # list now has it
    assert [s["id"] for s in client.get("/api/strategy-prompts").json()] == [sid]
    # update
    r = client.put(
        f"/api/strategy-prompts/{sid}",
        json={"name": "Mom2", "body": "b2", "enabled": False},
    )
    assert r.status_code == 200
    assert r.json()["name"] == "Mom2"
    assert r.json()["enabled"] is False
    # delete (never referenced -> hard delete; list empty again)
    r = client.delete(f"/api/strategy-prompts/{sid}")
    assert r.status_code == 200
    assert client.get("/api/strategy-prompts").json() == []


def test_strategy_update_unknown_404(client: TestClient) -> None:
    r = client.put(
        "/api/strategy-prompts/999", json={"name": "x", "body": "y", "enabled": True}
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_strategy_delete_unknown_404(client: TestClient) -> None:
    assert client.delete("/api/strategy-prompts/999").status_code == 404


def test_strategy_delete_referenced_409(client: TestClient) -> None:
    sp = client.post(
        "/api/strategy-prompts", json={"name": "A", "body": "{{kpis_json}}"}
    ).json()
    it = client.post(
        "/api/insight-types",
        json={"name": "Combo", "scope": "portfolio", "strategy_ids": [sp["id"]]},
    ).json()
    r = client.delete(f"/api/strategy-prompts/{sp['id']}")
    assert r.status_code == 409
    body = r.json()
    assert body["error"]["code"] == "conflict"
    assert it["id"] in body["error"]["referencing"]


# --- insight-types CRUD -------------------------------------------------------


def test_insight_type_list_empty(client: TestClient) -> None:
    assert client.get("/api/insight-types").json() == []


def test_insight_type_create_with_ordered_strategies(client: TestClient) -> None:
    a = client.post("/api/strategy-prompts", json={"name": "A", "body": "{{kpis_json}}"}).json()
    b = client.post("/api/strategy-prompts", json={"name": "B", "body": "{{kpis_json}}"}).json()
    r = client.post(
        "/api/insight-types",
        json={"name": "Combo", "scope": "portfolio", "strategy_ids": [b["id"], a["id"]]},
    )
    assert r.status_code == 200
    it = r.json()
    assert it["scope"] == "portfolio"
    assert it["self_correct"] is False
    assert it["use_system_prompt"] is True
    assert it["enabled"] is True
    assert it["schedule"] is None
    assert it["active_calibration_version"] is None
    assert it["calib_summary"] is None
    # strategies serialized in position order, with names
    assert [s["id"] for s in it["strategies"]] == [b["id"], a["id"]]
    assert [s["position"] for s in it["strategies"]] == [0, 1]
    assert it["strategies"][0]["name"] == "B"


def test_insight_type_update(client: TestClient) -> None:
    it = client.post(
        "/api/insight-types", json={"name": "N", "scope": "portfolio"}
    ).json()
    r = client.put(
        f"/api/insight-types/{it['id']}",
        json={"name": "N2", "scope": "portfolio", "self_correct": True},
    )
    assert r.status_code == 200
    assert r.json()["name"] == "N2"
    assert r.json()["self_correct"] is True


def test_insight_type_update_unknown_404(client: TestClient) -> None:
    r = client.put("/api/insight-types/999", json={"name": "x", "scope": "portfolio"})
    assert r.status_code == 404


def test_insight_type_horizon_and_eval_prompt_in_wire(client: TestClient) -> None:
    # Defaults: horizon_days 5, eval_prompt null (spec 04.10).
    created = client.post(
        "/api/insight-types", json={"name": "N", "scope": "portfolio"}
    ).json()
    assert created["horizon_days"] == 5
    assert created["eval_prompt"] is None
    # Explicit override on create.
    custom = client.post(
        "/api/insight-types",
        json={
            "name": "Watch", "scope": "per_symbol",
            "horizon_days": 10, "eval_prompt": "自訂 {{now}}",
        },
    ).json()
    assert custom["horizon_days"] == 10
    assert custom["eval_prompt"] == "自訂 {{now}}"
    # Update changes them; GET echoes.
    upd = client.put(
        f"/api/insight-types/{custom['id']}",
        json={"name": "Watch", "scope": "per_symbol", "horizon_days": 3},
    ).json()
    assert upd["horizon_days"] == 3
    listed = {x["id"]: x for x in client.get("/api/insight-types").json()}
    assert listed[custom["id"]]["horizon_days"] == 3


def test_insight_type_delete_archives_and_clears_schedule(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    it = client.post(
        "/api/insight-types", json={"name": "N", "scope": "portfolio"}
    ).json()
    # bind a schedule row directly so DELETE must clear it (schedule POST is task 5)
    bind_insight_schedule(conn, it["id"], cron="0 8 * * *")
    r = client.delete(f"/api/insight-types/{it['id']}")
    assert r.status_code == 200
    # gone from the default list...
    assert it["id"] not in {x["id"] for x in client.get("/api/insight-types").json()}
    # ...and the schedule_config binding row was removed.
    assert conn.execute(
        "SELECT 1 FROM schedule_config WHERE job_id = ?", (f"insight:{it['id']}",)
    ).fetchone() is None


def test_insight_type_delete_unknown_404(client: TestClient) -> None:
    assert client.delete("/api/insight-types/999").status_code == 404


def test_on_alert_defaults_disabled_r7(client: TestClient) -> None:
    r = client.post(
        "/api/insight-types",
        json={"name": "Alert", "scope": "on_alert", "alert_rules": ["fx_drift"]},
    )
    assert r.status_code == 200
    assert r.json()["enabled"] is False  # R7: new on_alert rows default disabled


# --- R1: scope x variable-scope mismatch (spec 4.9) ---------------------------


def test_r1_portfolio_scope_with_per_symbol_variable_422(client: TestClient) -> None:
    # symbol_detail_json is a per_symbol variable; in a portfolio-scope combo -> reject.
    sp = client.post(
        "/api/strategy-prompts", json={"name": "Bad", "body": "{{symbol_detail_json}}"}
    ).json()
    r = client.post(
        "/api/insight-types",
        json={"name": "Combo", "scope": "portfolio", "strategy_ids": [sp["id"]]},
    )
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "validation_error"
    tokens = {i["token"] for i in body["error"]["issues"]}
    assert "symbol_detail_json" in tokens


def test_r1_per_symbol_scope_accepts_per_symbol_variable(client: TestClient) -> None:
    sp = client.post(
        "/api/strategy-prompts", json={"name": "OK", "body": "{{symbol_detail_json}}"}
    ).json()
    r = client.post(
        "/api/insight-types",
        json={"name": "Sym", "scope": "per_symbol", "strategy_ids": [sp["id"]]},
    )
    assert r.status_code == 200


def test_r1_enforced_on_update(client: TestClient) -> None:
    bad = client.post(
        "/api/strategy-prompts", json={"name": "Bad", "body": "{{price_history_json}}"}
    ).json()
    it = client.post(
        "/api/insight-types", json={"name": "N", "scope": "portfolio"}
    ).json()
    r = client.put(
        f"/api/insight-types/{it['id']}",
        json={"name": "N", "scope": "portfolio", "strategy_ids": [bad["id"]]},
    )
    assert r.status_code == 422


# --- schedule (spec 4.2) ------------------------------------------------------


def test_schedule_post_then_get_echoes_cron(client: TestClient) -> None:
    it = client.post(
        "/api/insight-types", json={"name": "N", "scope": "portfolio"}
    ).json()
    r = client.post(f"/api/insight-types/{it['id']}/schedule", json={"cron": "0 8 * * *"})
    assert r.status_code == 200
    assert r.json()["job_id"] == f"insight:{it['id']}"
    # Subsequent GET reflects the cron under `schedule`.
    listed = {x["id"]: x for x in client.get("/api/insight-types").json()}
    assert listed[it["id"]]["schedule"] == {"cron": "0 8 * * *"}


def test_schedule_delete_removes_it(client: TestClient) -> None:
    it = client.post(
        "/api/insight-types", json={"name": "N", "scope": "portfolio"}
    ).json()
    client.post(f"/api/insight-types/{it['id']}/schedule", json={"cron": "0 8 * * *"})
    r = client.delete(f"/api/insight-types/{it['id']}/schedule")
    assert r.status_code == 200
    listed = {x["id"]: x for x in client.get("/api/insight-types").json()}
    assert listed[it["id"]]["schedule"] is None


def test_schedule_on_alert_rejected_400(client: TestClient) -> None:
    it = client.post(
        "/api/insight-types",
        json={"name": "Alert", "scope": "on_alert", "alert_rules": ["fx_drift"]},
    ).json()
    r = client.post(f"/api/insight-types/{it['id']}/schedule", json={"cron": "0 8 * * *"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "validation_error"


def test_schedule_unknown_insight_type_404(client: TestClient) -> None:
    r = client.post("/api/insight-types/999/schedule", json={"cron": "0 8 * * *"})
    assert r.status_code == 404


# --- active-calibration (spec 4.6) --------------------------------------------


def test_active_calibration_set_and_clear(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    it = client.post(
        "/api/insight-types", json={"name": "N", "scope": "portfolio"}
    ).json()
    cs.create_calibration(conn, it["id"], body="v1", cause="seed", now=NOW)  # version 1
    r = client.put(
        f"/api/insight-types/{it['id']}/active-calibration", json={"version": 1}
    )
    assert r.status_code == 200
    listed = {x["id"]: x for x in client.get("/api/insight-types").json()}
    assert listed[it["id"]]["active_calibration_version"] == 1
    # clear with null
    r = client.put(
        f"/api/insight-types/{it['id']}/active-calibration", json={"version": None}
    )
    assert r.status_code == 200
    listed = {x["id"]: x for x in client.get("/api/insight-types").json()}
    assert listed[it["id"]]["active_calibration_version"] is None


def test_active_calibration_nonexistent_version_400(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    it = client.post(
        "/api/insight-types", json={"name": "N", "scope": "portfolio"}
    ).json()
    r = client.put(
        f"/api/insight-types/{it['id']}/active-calibration", json={"version": 5}
    )
    assert r.status_code == 400


def test_active_calibration_unknown_insight_type_404(client: TestClient) -> None:
    r = client.put("/api/insight-types/999/active-calibration", json={"version": None})
    assert r.status_code == 404


# --- calibrations (spec 4.7) --------------------------------------------------


def test_calibrations_filter_and_include_archived(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    it = client.post(
        "/api/insight-types", json={"name": "N", "scope": "portfolio"}
    ).json()
    other = client.post(
        "/api/insight-types", json={"name": "O", "scope": "portfolio"}
    ).json()
    c1 = cs.create_calibration(conn, it["id"], body="v1", cause="seed", now=NOW)
    cs.create_calibration(conn, it["id"], body="v2", cause="miss", now=NOW)
    cs.create_calibration(conn, other["id"], body="x", cause="seed", now=NOW)
    cs.archive_calibration(conn, c1.id)
    # default: filtered to insight_type, archived hidden
    rows = client.get(f"/api/calibrations?insight_type={it['id']}").json()
    assert [c["version"] for c in rows] == [2]
    # include_archived
    rows = client.get(
        f"/api/calibrations?insight_type={it['id']}&include_archived=true"
    ).json()
    assert [c["version"] for c in rows] == [1, 2]


def test_calibration_archive_soft_deletes_and_clears_active(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    it = client.post(
        "/api/insight-types", json={"name": "N", "scope": "portfolio"}
    ).json()
    c1 = cs.create_calibration(conn, it["id"], body="v1", cause="seed", now=NOW)
    client.put(f"/api/insight-types/{it['id']}/active-calibration", json={"version": 1})
    r = client.post(f"/api/calibrations/{c1.id}/archive")
    assert r.status_code == 200
    # hidden by default
    assert client.get(f"/api/calibrations?insight_type={it['id']}").json() == []
    # active cleared
    listed = {x["id"]: x for x in client.get("/api/insight-types").json()}
    assert listed[it["id"]]["active_calibration_version"] is None


def test_calibration_archive_unknown_404(client: TestClient) -> None:
    assert client.post("/api/calibrations/999/archive").status_code == 404


def test_calibration_samples_empty_shape(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    it = client.post(
        "/api/insight-types", json={"name": "N", "scope": "portfolio"}
    ).json()
    c1 = cs.create_calibration(conn, it["id"], body="v1", cause="seed", now=NOW)
    r = client.get(f"/api/calibrations/{c1.id}/samples")
    assert r.status_code == 200
    assert r.json() == []  # real shape; populated by 04c


# --- evolution-config (spec 4.6) ----------------------------------------------


def test_evolution_config_get_defaults(client: TestClient) -> None:
    r = client.get("/api/evolution-config")
    assert r.status_code == 200
    assert r.json() == {
        "auto_promote": False,
        "shadow_batches": 3,
        "min_samples": 8,
        "max_shadows": 2,
        "gap_alert_pp": "10",
        "defer_limit_days": 5,
        "horizon_basis": "trading_days",
        "shadow_on_alert": False,
    }


def test_evolution_config_put_roundtrip(client: TestClient) -> None:
    r = client.put(
        "/api/evolution-config",
        json={
            "auto_promote": True,
            "shadow_batches": 5,
            "min_samples": 12,
            "max_shadows": 3,
            "gap_alert_pp": "7.5",
            "defer_limit_days": 7,
            "horizon_basis": "calendar_days",
            "shadow_on_alert": True,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["gap_alert_pp"] == "7.5"
    assert body["auto_promote"] is True
    assert body["defer_limit_days"] == 7
    assert body["horizon_basis"] == "calendar_days"
    assert body["shadow_on_alert"] is True
    # persisted
    fresh = client.get("/api/evolution-config").json()
    assert fresh["shadow_batches"] == 5
    assert fresh["horizon_basis"] == "calendar_days"


def test_evolution_config_put_bad_gap_400(client: TestClient) -> None:
    r = client.put(
        "/api/evolution-config",
        json={
            "auto_promote": False,
            "shadow_batches": 3,
            "min_samples": 8,
            "max_shadows": 2,
            "gap_alert_pp": "not-a-number",
        },
    )
    assert r.status_code == 400


def test_evolution_config_put_bad_horizon_basis_400(client: TestClient) -> None:
    r = client.put(
        "/api/evolution-config",
        json={
            "auto_promote": False,
            "shadow_batches": 3,
            "min_samples": 8,
            "max_shadows": 2,
            "gap_alert_pp": "10",
            "horizon_basis": "weekly",  # not in {trading_days, calendar_days}
        },
    )
    assert r.status_code == 400
