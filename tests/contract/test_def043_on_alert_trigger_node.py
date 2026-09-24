"""DEF-043 (functional test manual H-05, 2026-09-24): an ``on_alert`` task's trigger is the alert,
not "manual — will not auto-run".

The pipeline page's ① 觸發節點 for the 持倉提點 card read 「未排程（手動）・不會自動執行」 and the
page head counted the task under 「需注意」 — while the SAME task's dry-run G1 said 「由風險預警事件
觸發」 ✓ and ``alert_scan`` #183 had just dispatched its run #184. The node derivation
(``llm_insight/pipeline_status._trigger``) read ``scheduled`` alone; an on_alert task can never
be scheduled (the schedule route refuses it), so it was always 「手動」.

Checked where the owner sees it: ``GET /api/insight-tasks/status`` (the pipeline page's only
source) and ``POST …/preflight`` (G1), for an on_alert task and — the counter-proof — for an
ordinary unscheduled task, which must STILL warn. And at the classification level: a task that
is otherwise healthy must not aggregate to ``warn`` (需注意) merely for being event-triggered.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

from portfolio_dash.llm_insight.pipeline_status import PipelineFacts, derive_node_states


def _task(client: TestClient, *, scope: str, **extra: Any) -> int:
    sp = client.post("/api/strategy-prompts",
                     json={"name": "S-" + scope, "body": "{{kpis_json}}"}).json()
    it = client.post("/api/insight-types", json={
        "name": "T-" + scope, "scope": scope, "strategy_ids": [sp["id"]], **extra}).json()
    return int(it["id"])


def _status(client: TestClient, tid: int) -> dict[str, Any]:
    tasks = client.get("/api/insight-tasks/status").json()["tasks"]
    (task,) = [t for t in tasks if t["id"] == tid]
    assert isinstance(task, dict)
    return task


def test_an_on_alert_task_reads_as_alert_triggered_on_the_page_and_in_the_dry_run(
    api_client: TestClient,
) -> None:
    tid = _task(api_client, scope="on_alert", alert_rules="all", enabled=True)
    trig = _status(api_client, tid)["nodes"]["trigger"]
    assert trig["lv"] == "ok", trig
    assert trig["text"] == "預警觸發" and "自動執行" in (trig["sub"] or "")
    assert "未排程" not in trig["text"] and "不會自動執行" not in (trig["sub"] or "")
    # The dry run says the same thing about the same task.
    g1 = next(g for g in api_client.post(f"/api/insight-tasks/{tid}/preflight").json()["gates"]
              if g["id"] == "G1")
    assert g1["lv"] == "ok" and g1["msg"] == "由風險預警事件觸發"


def test_an_ordinary_unscheduled_task_still_warns(api_client: TestClient) -> None:
    """Counter-proof: 「手動」 is still the truth for a task nothing triggers."""
    tid = _task(api_client, scope="portfolio")
    trig = _status(api_client, tid)["nodes"]["trigger"]
    assert trig == {"lv": "warn", "text": "未排程（手動）", "sub": "不會自動執行"}
    g1 = next(g for g in api_client.post(f"/api/insight-tasks/{tid}/preflight").json()["gates"]
              if g["id"] == "G1")
    assert g1["lv"] == "warn"


def _healthy(**over: object) -> PipelineFacts:
    base: dict[str, object] = {
        "enabled": True, "scope": "on_alert", "scheduled": False, "universe_symbols": [],
        "removed_recently": [], "missing_price_symbols": [], "stale_price_symbols": [],
        "live_template_count": 1, "total_template_count": 1, "r1_mismatch": False,
        "unapplied_calibration": False, "self_correct": False, "master_configured": True,
        "quota_remaining": Decimal("5"), "quota_low": Decimal("1"), "last_run_status": "ok",
    }
    base.update(over)
    return PipelineFacts(**base)  # type: ignore[arg-type]


def test_a_healthy_on_alert_task_is_not_counted_under_needs_attention() -> None:
    """The page's 「需注意」 count is the tasks whose aggregate level is warn/fail."""
    assert derive_node_states(_healthy()).level == "ok"
    assert derive_node_states(_healthy(scope="portfolio")).level == "warn"
