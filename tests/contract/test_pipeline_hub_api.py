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


# --- §7.6 acceptance scenarios (the 3 frontend failure demos, reproducible) ----


def test_scenario_1_disabled_unscheduled(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """Demo 1 (股利展望): task disabled + unscheduled → diagnose first_blocker=G0 with
    enable_task + create_schedule fixes; status trigger node is idle (whole task idle)."""
    from decimal import Decimal

    from portfolio_dash.shared.llm_config import add_topup

    add_topup(golden_db, Decimal("5"))
    sp = api_client.post(
        "/api/strategy-prompts", json={"name": "S", "body": "{{kpis_json}}"}
    ).json()
    it = api_client.post(
        "/api/insight-types",
        json={
            "name": "股利展望", "scope": "portfolio", "strategy_ids": [sp["id"]],
            "enabled": False,
        },
    ).json()
    tid = it["id"]
    diag = api_client.get(f"/api/insight-tasks/{tid}/diagnose").json()
    assert diag["first_blocker"] == "G0"
    fix_kinds = {g["fix"]["kind"] for g in diag["gates"] if g["fix"] is not None}
    assert {"enable_task", "create_schedule"} <= fix_kinds
    # status: a disabled task is wholly idle.
    task = next(
        t for t in api_client.get("/api/insight-tasks/status").json()["tasks"]
        if t["id"] == tid
    )
    assert task["enabled"] is False
    assert task["level"] == "idle"
    assert task["nodes"]["trigger"]["lv"] == "idle"


def test_scenario_2_only_template_disabled_shared_gate(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """Demo 2 (動能週報): the only template disabled → preflight R3=fail with an
    enable_template fix; and a REAL run writes job_runs skipped/R3_no_live_templates — the
    SAME gate verdict, proving preflight and execution share one gate (the §7.2 hard rule).
    """
    from datetime import datetime
    from decimal import Decimal
    from zoneinfo import ZoneInfo

    from portfolio_dash.api import insight_service
    from portfolio_dash.shared.llm_config import add_topup

    add_topup(golden_db, Decimal("5"))  # quota so R6 is not the blocker
    sp = api_client.post(
        "/api/strategy-prompts", json={"name": "Mom", "body": "{{kpis_json}}"}
    ).json()
    it = api_client.post(
        "/api/insight-types",
        json={"name": "動能週報", "scope": "portfolio", "strategy_ids": [sp["id"]]},
    ).json()
    tid = it["id"]
    # disable the only template.
    api_client.put(
        f"/api/strategy-prompts/{sp['id']}",
        json={"name": "Mom", "body": "{{kpis_json}}", "enabled": False},
    )
    # preflight predicts R3 fail + enable_template fix.
    pf = api_client.post(f"/api/insight-tasks/{tid}/preflight").json()
    r3 = next(g for g in pf["gates"] if g["id"] == "R3")
    assert r3["lv"] == "fail"
    assert r3["fix"]["kind"] == "enable_template"

    # a REAL run goes through the SAME gate and writes the SAME R3 skip reason.
    now = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))
    result = insight_service.run_for_id(golden_db, tid, now=now)
    assert result.status == "skipped"
    assert result.reason == "R3_no_live_templates"
    row = golden_db.execute(
        "SELECT status, reason FROM job_runs WHERE job_id = ? ORDER BY id DESC LIMIT 1",
        (f"insight:{tid}",),
    ).fetchone()
    assert row["status"] == "skipped"
    assert row["reason"] == "R3_no_live_templates"

    # status assemble node = fail (all templates off).
    task = next(
        t for t in api_client.get("/api/insight-tasks/status").json()["tasks"]
        if t["id"] == tid
    )
    assert task["nodes"]["assemble"]["lv"] == "fail"


def test_scenario_3_custom_universe_emptied(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """Demo 3 (高息標的體檢): a per_symbol task whose custom universe is emptied → status
    input node = fail (R2) and preflight R2 = fail."""
    from decimal import Decimal

    from portfolio_dash.shared.llm_config import add_topup

    add_topup(golden_db, Decimal("5"))
    sp = api_client.post(
        "/api/strategy-prompts", json={"name": "Yield", "body": "{{symbol}}"}
    ).json()
    it = api_client.post(
        "/api/insight-types",
        json={
            "name": "高息標的體檢", "scope": "per_symbol", "strategy_ids": [sp["id"]],
            "universe": {"mode": "custom", "symbols": []},  # emptied list
        },
    ).json()
    tid = it["id"]
    # status: input node fails on the empty universe.
    task = next(
        t for t in api_client.get("/api/insight-tasks/status").json()["tasks"]
        if t["id"] == tid
    )
    assert task["nodes"]["input"]["lv"] == "fail"
    # preflight: R2 fails too (shared gate).
    pf = api_client.post(f"/api/insight-tasks/{tid}/preflight").json()
    r2 = next(g for g in pf["gates"] if g["id"] == "R2")
    assert r2["lv"] == "fail"


def test_scenario_3_missing_price_holding_warns_input(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """Demo 3 (R4 source = dashboard freshness, the locked decision): a per_symbol task over
    a HELD symbol whose stored price is stale/missing surfaces as an input WARN (not a fail);
    the universe is non-empty, so the task still has work. The input node reuses the SAME
    dashboard freshness the dashboard itself computes."""
    from decimal import Decimal

    from portfolio_dash.shared.llm_config import add_topup

    add_topup(golden_db, Decimal("5"))
    # remove the stored AAPL price so the dashboard freshness reports it missing.
    golden_db.execute("DELETE FROM prices WHERE instrument = 'AAPL'")
    golden_db.commit()
    sp = api_client.post(
        "/api/strategy-prompts", json={"name": "Yield", "body": "{{symbol}}"}
    ).json()
    it = api_client.post(
        "/api/insight-types",
        json={
            "name": "高息標的體檢", "scope": "per_symbol", "strategy_ids": [sp["id"]],
            "universe": {"mode": "custom", "symbols": ["AAPL"]},
        },
    ).json()
    tid = it["id"]
    task = next(
        t for t in api_client.get("/api/insight-tasks/status").json()["tasks"]
        if t["id"] == tid
    )
    assert task["nodes"]["input"]["lv"] == "warn"  # missing price, not empty


# --- one-click official pack (usability decision ①, 2026-07-05) ----------------


def test_official_pack_creates_tasks_with_schedules(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    r = api_client.post("/api/insight-tasks/official-pack")
    assert r.status_code == 200
    body = r.json()
    assert [c["name"] for c in body["created"]] == ["持倉週報", "個股健檢", "市場週報"]
    assert body["skipped"] == []
    # tasks exist with the preset knobs + a mounted schedule.
    types = {t["name"]: t for t in api_client.get("/api/insight-types").json()}
    weekly, checkup, market = types["持倉週報"], types["個股健檢"], types["市場週報"]
    assert weekly["scope"] == "portfolio" and weekly["self_correct"] is False
    assert checkup["scope"] == "per_symbol" and checkup["self_correct"] is True
    assert market["scope"] == "per_market" and market["self_correct"] is False
    assert checkup["horizon_days"] == 14
    assert weekly["schedule"] and checkup["schedule"] and market["schedule"]
    assert [s["name"] for s in weekly["strategies"]] == ["持倉週報策略"]
    assert [s["name"] for s in market["strategies"]] == ["市場週報策略"]
    # strategies were created from the library.
    names = {s["name"] for s in api_client.get("/api/strategy-prompts").json()}
    assert {"持倉週報策略", "個股健檢策略", "市場週報策略"} <= names


def test_official_pack_is_idempotent_and_reuses_strategies(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    # A pre-existing customized strategy with the official name is REUSED, not
    # duplicated; a second click skips both tasks.
    api_client.post("/api/strategy-prompts",
                    json={"name": "持倉週報策略", "body": "我的自訂版 {{kpis_json}}"})
    first = api_client.post("/api/insight-tasks/official-pack").json()
    weekly_created = next(c for c in first["created"] if c["name"] == "持倉週報")
    assert weekly_created["strategy_reused"] is True
    weeklies = [s for s in api_client.get("/api/strategy-prompts").json()
                if s["name"] == "持倉週報策略"]
    assert len(weeklies) == 1 and "我的自訂版" in weeklies[0]["body"]
    second = api_client.post("/api/insight-tasks/official-pack").json()
    assert second["created"] == []
    assert sorted(second["skipped"]) == sorted(["持倉週報", "個股健檢", "市場週報"])


def test_official_pack_rename_then_repack_no_duplicate(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    # M3 fix (decision Q3a): the pack's idempotency keys on preset_key provenance, so a
    # RENAMED official task is never re-created (no double cron, no double cost).
    first = api_client.post("/api/insight-tasks/official-pack").json()
    assert len(first["created"]) == 3
    weekly = next(c for c in first["created"] if c["name"] == "持倉週報")
    # user renames the official weekly task (PUT keeps preset_key untouched).
    detail = api_client.get("/api/insight-types").json()
    weekly_full = next(t for t in detail if t["id"] == weekly["id"])
    r = api_client.put(f"/api/insight-types/{weekly['id']}", json={
        "name": "我的週報", "scope": weekly_full["scope"],
        "strategy_ids": [s["id"] for s in weekly_full["strategies"]],
    })
    assert r.status_code == 200

    second = api_client.post("/api/insight-tasks/official-pack").json()

    assert second["created"] == []  # nothing re-created despite the rename
    assert sorted(second["skipped"]) == sorted(["持倉週報", "個股健檢", "市場週報"])
    names = [t["name"] for t in api_client.get("/api/insight-types").json()]
    assert names.count("持倉週報") == 0 and names.count("我的週報") == 1
    # exactly one schedule binding for the renamed task (no double cron).
    n = golden_db.execute(
        "SELECT COUNT(*) AS n FROM schedule_config WHERE job_id LIKE 'insight:%'"
    ).fetchone()["n"]
    assert n == 3


def test_official_pack_name_fallback_for_precolumn_installs(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    # An install created BEFORE the preset_key column has official tasks with NULL
    # provenance — the pack must still skip them by exact name (no duplicate).
    api_client.post("/api/insight-tasks/official-pack")
    golden_db.execute("UPDATE insight_types SET preset_key = NULL")
    golden_db.commit()
    second = api_client.post("/api/insight-tasks/official-pack").json()
    assert second["created"] == []
    assert sorted(second["skipped"]) == sorted(["持倉週報", "個股健檢", "市場週報"])


# --- per_market tasks over the API (2026-07-05 spec) ----------------------------


def test_per_market_task_create_preflight_run(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    from decimal import Decimal as _D

    from portfolio_dash.shared.llm_config import add_topup

    add_topup(golden_db, _D("5"))
    sp = api_client.post(
        "/api/strategy-prompts", json={"name": "M", "body": "{{holdings_json}}"}
    ).json()
    it = api_client.post(
        "/api/insight-types",
        json={"name": "市場卡", "scope": "per_market", "strategy_ids": [sp["id"]]},
    ).json()
    assert it["scope"] == "per_market"
    # preflight: the shared gate accepts the scope; R1/R2 pass on the golden book.
    pf = api_client.post(f"/api/insight-tasks/{it['id']}/preflight").json()
    gates = {g["id"]: g["lv"] for g in pf["gates"]}
    assert gates["R1"] == "ok" and gates["R2"] == "ok"
    # a per_symbol variable in the template is an R1 block for per_market.
    sp2 = api_client.post(
        "/api/strategy-prompts", json={"name": "M2", "body": "{{symbol_detail_json}}"}
    ).json()
    r = api_client.post(
        "/api/insight-types",
        json={"name": "市場卡2", "scope": "per_market", "strategy_ids": [sp2["id"]]},
    )
    assert r.status_code == 422  # R1 rejected at create, same as portfolio scope
    # manual run dispatches (bg thread is hermetic here; 202 + running row is the contract).
    assert api_client.post(f"/api/insight-types/{it['id']}/run").status_code == 202
