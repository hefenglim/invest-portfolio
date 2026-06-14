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
