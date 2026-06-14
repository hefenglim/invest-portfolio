"""Contract tests for the spec-07 §7.2 dry-run preflight (and §7.3 diagnose).

The crux of spec 07: preflight MUST call the SAME runtime gate
(``llm_insight.gating.evaluate_gates``) that ``generate.run_insight_type`` uses — no
second/parallel gate, so a "preflight passed, run failed" double-truth is impossible.
Preflight also reuses the spec-06 assembled-preview path (layers + est_tokens) and is
ZERO-COST: it never calls the LLM and never writes a ``job_runs`` row. These tests assert
all of that, plus the §7.2 gate ordering, verdict, and one-key fixes; and the draft-body
(unsaved-task wizard) path.
"""

import sqlite3
from decimal import Decimal

from fastapi.testclient import TestClient

from portfolio_dash.shared.llm_config import add_topup


def _make_combo(
    api_client: TestClient, *, scope: str = "portfolio", body: str = "{{kpis_json}}"
) -> int:
    sp = api_client.post("/api/strategy-prompts", json={"name": "S", "body": body}).json()
    it = api_client.post(
        "/api/insight-types",
        json={"name": "Daily", "scope": scope, "strategy_ids": [sp["id"]]},
    ).json()
    return int(it["id"])


def _gate_ids(body: dict[str, object]) -> list[str]:
    return [g["id"] for g in body["gates"]]  # type: ignore[index,union-attr]


# --- §7.2 preflight shape + ordering ------------------------------------------


def test_preflight_gate_order_and_shape(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    add_topup(golden_db, Decimal("5"))  # quota so R6 is ok
    tid = _make_combo(api_client)
    r = api_client.post(f"/api/insight-tasks/{tid}/preflight")
    assert r.status_code == 200
    body = r.json()
    assert _gate_ids(body) == ["G0", "G1", "R1", "R2", "R3", "R4", "R5", "R6", "G7"]
    for g in body["gates"]:
        assert g["lv"] in {"ok", "info", "warn", "fail"}
        assert "name" in g and "msg" in g
    assert body["verdict"] in {"blocked", "degraded", "clean"}
    preview = body["assembled_preview"]
    assert isinstance(preview["layers"], list)
    assert isinstance(preview["est_tokens"], int)
    assert isinstance(preview["est_cost_usd"], str)  # Decimal STRING, never float


def test_preflight_unscheduled_g1_fail_with_create_schedule_fix(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    add_topup(golden_db, Decimal("5"))
    tid = _make_combo(api_client)
    body = api_client.post(f"/api/insight-tasks/{tid}/preflight").json()
    g1 = next(g for g in body["gates"] if g["id"] == "G1")
    assert g1["lv"] == "fail"  # manual / unscheduled won't auto-run
    assert g1["fix"]["kind"] == "create_schedule"
    assert body["verdict"] == "blocked"


def test_preflight_disabled_task_g0_fail_with_enable_fix(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    add_topup(golden_db, Decimal("5"))
    sp = api_client.post(
        "/api/strategy-prompts", json={"name": "S", "body": "{{kpis_json}}"}
    ).json()
    it = api_client.post(
        "/api/insight-types",
        json={
            "name": "Off", "scope": "portfolio", "strategy_ids": [sp["id"]],
            "enabled": False,
        },
    ).json()
    body = api_client.post(f"/api/insight-tasks/{it['id']}/preflight").json()
    g0 = next(g for g in body["gates"] if g["id"] == "G0")
    assert g0["lv"] == "fail"
    assert g0["fix"]["kind"] == "enable_task"


def test_preflight_quota_zero_r6_fail(api_client: TestClient) -> None:
    # No top-up in the golden DB → quota 0 → R6 hard block.
    tid = _make_combo(api_client)
    body = api_client.post(f"/api/insight-tasks/{tid}/preflight").json()
    r6 = next(g for g in body["gates"] if g["id"] == "R6")
    assert r6["lv"] == "fail"
    assert body["verdict"] == "blocked"
    # Senior-review fix: the quota gate has NO one-click fix (a top-up is not in the
    # §7.2 fix.kind enum); R6 must never emit create_schedule (that belongs to G1).
    assert r6.get("fix") is None


def test_preflight_template_disabled_r3_fail_with_enable_fix(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    add_topup(golden_db, Decimal("5"))
    sp = api_client.post(
        "/api/strategy-prompts", json={"name": "S", "body": "{{kpis_json}}"}
    ).json()
    it = api_client.post(
        "/api/insight-types",
        json={"name": "T", "scope": "portfolio", "strategy_ids": [sp["id"]]},
    ).json()
    # disable the only template.
    api_client.put(
        f"/api/strategy-prompts/{sp['id']}",
        json={"name": "S", "body": "{{kpis_json}}", "enabled": False},
    )
    body = api_client.post(f"/api/insight-tasks/{it['id']}/preflight").json()
    r3 = next(g for g in body["gates"] if g["id"] == "R3")
    assert r3["lv"] == "fail"
    assert r3["fix"]["kind"] == "enable_template"
    assert r3["fix"]["id"] == sp["id"]


# --- zero-cost guarantees -----------------------------------------------------


def test_preflight_writes_no_job_run(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    tid = _make_combo(api_client)
    before = golden_db.execute("SELECT COUNT(*) AS c FROM job_runs").fetchone()["c"]
    api_client.post(f"/api/insight-tasks/{tid}/preflight")
    after = golden_db.execute("SELECT COUNT(*) AS c FROM job_runs").fetchone()["c"]
    assert after == before  # NEVER writes a job_runs row


def test_preflight_never_calls_llm(
    api_client: TestClient, golden_db: sqlite3.Connection, monkeypatch
) -> None:
    add_topup(golden_db, Decimal("5"))
    tid = _make_combo(api_client)

    def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("preflight must NEVER call the LLM")

    import portfolio_dash.shared.llm as llm_mod

    monkeypatch.setattr(llm_mod, "complete_structured_meta", _boom)
    monkeypatch.setattr(llm_mod, "complete_text", _boom)
    r = api_client.post(f"/api/insight-tasks/{tid}/preflight")
    assert r.status_code == 200
    # No usage row was written either.
    assert golden_db.execute("SELECT COUNT(*) AS c FROM llm_usage").fetchone()["c"] == 0


# --- HARD rule: the SAME gate function as execution ---------------------------


def test_preflight_uses_the_shared_gate_fn(
    api_client: TestClient, golden_db: sqlite3.Connection, monkeypatch
) -> None:
    """Preflight routes through ``llm_insight.gating.evaluate_gates`` — the SAME function
    object ``generate.run_insight_type`` calls (asserted by spying on the gating module)."""
    add_topup(golden_db, Decimal("5"))
    tid = _make_combo(api_client)

    import portfolio_dash.llm_insight.gating as gating_mod

    calls: list[object] = []
    real = gating_mod.evaluate_gates

    def _spy(ctx: object) -> object:
        calls.append(ctx)
        return real(ctx)  # type: ignore[arg-type]

    monkeypatch.setattr(gating_mod, "evaluate_gates", _spy)
    api_client.post(f"/api/insight-tasks/{tid}/preflight")
    assert calls, "preflight did not call gating.evaluate_gates"


# --- draft body (unsaved wizard) ----------------------------------------------


def test_preflight_draft_body_no_persist(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    add_topup(golden_db, Decimal("5"))
    sp = api_client.post(
        "/api/strategy-prompts", json={"name": "Draft S", "body": "{{kpis_json}}"}
    ).json()
    before = golden_db.execute("SELECT COUNT(*) AS c FROM insight_types").fetchone()["c"]
    # preflight a NEW task that does not exist yet (id 0 placeholder + draft body).
    r = api_client.post(
        "/api/insight-tasks/0/preflight",
        json={
            "name": "Wizard", "scope": "portfolio", "strategy_ids": [sp["id"]],
            "enabled": True,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert _gate_ids(body) == ["G0", "G1", "R1", "R2", "R3", "R4", "R5", "R6", "G7"]
    # G0 ok (draft enabled), R3 ok (one live template), assembled preview built.
    assert next(g for g in body["gates"] if g["id"] == "G0")["lv"] == "ok"
    assert next(g for g in body["gates"] if g["id"] == "R3")["lv"] == "ok"
    assert body["assembled_preview"]["layers"]
    # NOTHING persisted.
    after = golden_db.execute("SELECT COUNT(*) AS c FROM insight_types").fetchone()["c"]
    assert after == before


def test_preflight_unknown_id_no_body_404(api_client: TestClient) -> None:
    assert api_client.post("/api/insight-tasks/999/preflight").status_code == 404


# --- §7.3 diagnose ------------------------------------------------------------


def test_diagnose_same_gates_plus_first_blocker_and_skips(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    tid = _make_combo(api_client)  # unscheduled + quota 0 → G1 + R6 fail
    # two prior skipped runs to surface in recent_skips.
    for reason in ("R6_quota", "R3_no_live_templates"):
        golden_db.execute(
            "INSERT INTO job_runs (job_id, started_at, finished_at, status, reason, "
            "payload, is_shadow) VALUES (?, '2026-06-11T08:00:00+08:00', "
            "'2026-06-11T08:00:01+08:00', 'skipped', ?, ?, 0)",
            (f"insight:{tid}", reason, str(tid)),
        )
    golden_db.commit()
    r = api_client.get(f"/api/insight-tasks/{tid}/diagnose")
    assert r.status_code == 200
    body = r.json()
    # same gate ids/order as preflight; no assembled_preview in diagnose.
    assert _gate_ids(body) == ["G0", "G1", "R1", "R2", "R3", "R4", "R5", "R6", "G7"]
    assert "assembled_preview" not in body
    # G1 (manual) is the first failing gate.
    assert body["first_blocker"] == "G1"
    skips = body["recent_skips"]
    assert len(skips) == 2
    assert skips[0]["reason"] == "R3_no_live_templates"  # newest first
    assert {s["reason"] for s in skips} == {"R6_quota", "R3_no_live_templates"}


def test_diagnose_clean_task_has_null_first_blocker(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    from decimal import Decimal as _D

    add_topup(golden_db, _D("5"))
    tid = _make_combo(api_client)
    api_client.post(f"/api/insight-tasks/{tid}/schedule", json={"cron": "0 8 * * *"})
    body = api_client.get(f"/api/insight-tasks/{tid}/diagnose").json()
    assert body["first_blocker"] is None  # no failing gate
    assert body["recent_skips"] == []


def test_diagnose_unknown_id_404(api_client: TestClient) -> None:
    assert api_client.get("/api/insight-tasks/999/diagnose").status_code == 404


# --- §7.4 task-view runs (via the alias, is_shadow excluded) -------------------


def test_runs_alias_filters_by_task_and_excludes_shadow(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    tid = _make_combo(api_client)
    other = _make_combo(api_client)
    # an active skipped run for this task, a shadow run, and a run for another task.
    golden_db.execute(
        "INSERT INTO job_runs (job_id, started_at, finished_at, status, reason, payload, "
        "is_shadow) VALUES (?, '2026-06-11T08:00:00+08:00', '2026-06-11T08:00:01+08:00', "
        "'skipped', 'R6_quota', ?, 0)",
        (f"insight:{tid}", str(tid)),
    )
    golden_db.execute(
        "INSERT INTO job_runs (job_id, started_at, finished_at, status, reason, payload, "
        "is_shadow) VALUES (?, '2026-06-11T08:01:00+08:00', '2026-06-11T08:01:01+08:00', "
        "'ok', NULL, ?, 1)",
        (f"insight:{tid}", str(tid)),
    )
    golden_db.execute(
        "INSERT INTO job_runs (job_id, started_at, finished_at, status, reason, payload, "
        "is_shadow) VALUES (?, '2026-06-11T08:02:00+08:00', '2026-06-11T08:02:01+08:00', "
        "'ok', NULL, ?, 0)",
        (f"insight:{other}", str(other)),
    )
    golden_db.commit()
    rows = api_client.get(f"/api/insight-tasks/{tid}/runs?limit=20").json()["rows"]
    assert len(rows) == 1  # only this task's active (non-shadow) run
    assert rows[0]["reason"] == "R6_quota"
    assert rows[0]["insight_type_id"] == tid
