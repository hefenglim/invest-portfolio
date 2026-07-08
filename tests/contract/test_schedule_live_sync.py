"""H1 fix — insight schedule bind/unbind/delete/pack must sync the LIVE APScheduler.

Before this fix the composer endpoints only wrote ``schedule_config`` rows: a new
binding never fired until restart, and a deleted task's stale trigger kept firing into
a KeyError. These tests assert (a) every handler degrades to a no-op when the scheduler
is absent (``app.state.scheduler`` unset — the standard hermetic ``api_client``), and
(b) with a REAL (paused) BackgroundScheduler mounted, the ``insight:{id}`` trigger
appears on bind / official-pack and disappears on unbind / task delete.
"""

import sqlite3
from collections.abc import Iterator

import pytest
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi.testclient import TestClient


def _make_combo(api_client: TestClient, name: str = "Daily") -> int:
    sp = api_client.post(
        "/api/strategy-prompts", json={"name": f"S-{name}", "body": "{{kpis_json}}"}
    ).json()
    it = api_client.post(
        "/api/insight-types",
        json={"name": name, "scope": "portfolio", "strategy_ids": [sp["id"]]},
    ).json()
    return int(it["id"])


# --- scheduler absent (app.state.scheduler unset) → graceful no-op --------------


def test_bind_unbind_delete_without_scheduler_no_crash(api_client: TestClient) -> None:
    it_id = _make_combo(api_client)
    r = api_client.post(f"/api/insight-types/{it_id}/schedule", json={"cron": "0 9 * * mon"})
    assert r.status_code == 200
    assert r.json()["job_id"] == f"insight:{it_id}"
    assert api_client.delete(f"/api/insight-types/{it_id}/schedule").status_code == 200
    assert api_client.delete(f"/api/insight-types/{it_id}").status_code == 200


def test_official_pack_without_scheduler_no_crash(api_client: TestClient) -> None:
    r = api_client.post("/api/insight-tasks/official-pack")
    assert r.status_code == 200
    assert len(r.json()["created"]) == 3


def test_mount_schedule_invalid_cron_400_no_write(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    # An invalid cron must 400 BEFORE any write — a stored bad cron would crash the
    # scheduler build at the next startup.
    it_id = _make_combo(api_client)
    r = api_client.post(f"/api/insight-types/{it_id}/schedule", json={"cron": "not a cron"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_cron"
    row = golden_db.execute(
        "SELECT 1 FROM schedule_config WHERE job_id = ?", (f"insight:{it_id}",)
    ).fetchone()
    assert row is None


# --- real (paused) scheduler mounted → live trigger add/remove ------------------


@pytest.fixture
def live_scheduler(api_client: TestClient) -> Iterator[BackgroundScheduler]:
    sched = BackgroundScheduler()
    sched.start(paused=True)  # real scheduler, no thread ever fires a job
    api_client.app.state.scheduler = sched  # type: ignore[attr-defined]
    try:
        yield sched
    finally:
        api_client.app.state.scheduler = None  # type: ignore[attr-defined]
        sched.shutdown(wait=False)


def test_bind_mounts_live_trigger_and_unbind_removes_it(
    api_client: TestClient, live_scheduler: BackgroundScheduler
) -> None:
    it_id = _make_combo(api_client)
    job_id = f"insight:{it_id}"
    assert live_scheduler.get_job(job_id) is None

    api_client.post(f"/api/insight-types/{it_id}/schedule", json={"cron": "0 9 * * mon"})
    job = live_scheduler.get_job(job_id)
    assert job is not None  # mounted immediately, not at next restart

    # re-bind updates in place (no duplicate), still one job
    api_client.post(f"/api/insight-types/{it_id}/schedule", json={"cron": "30 10 * * tue"})
    assert live_scheduler.get_job(job_id) is not None

    api_client.delete(f"/api/insight-types/{it_id}/schedule")
    assert live_scheduler.get_job(job_id) is None  # removed immediately


def test_delete_task_removes_live_trigger(
    api_client: TestClient, live_scheduler: BackgroundScheduler
) -> None:
    it_id = _make_combo(api_client, name="ToDelete")
    api_client.post(f"/api/insight-types/{it_id}/schedule", json={"cron": "0 9 * * mon"})
    assert live_scheduler.get_job(f"insight:{it_id}") is not None

    api_client.delete(f"/api/insight-types/{it_id}")
    assert live_scheduler.get_job(f"insight:{it_id}") is None


def test_official_pack_mounts_live_triggers(
    api_client: TestClient, live_scheduler: BackgroundScheduler
) -> None:
    body = api_client.post("/api/insight-tasks/official-pack").json()
    assert len(body["created"]) == 3
    for created in body["created"]:
        assert live_scheduler.get_job(f"insight:{created['id']}") is not None
