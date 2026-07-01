from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# This module enters the TestClient via `with` → the FastAPI lifespan runs, which
# starts a Windows ProactorEventLoop. That loop opens an internal socketpair self-pipe
# (not real network I/O), which the global --disable-socket ban blocks. Allow sockets
# for this module only; the hermetic api_client fixture (tests/conftest.py) avoids the
# lifespan precisely so it never needs this.
pytestmark = pytest.mark.enable_socket


@pytest.fixture
def skeleton_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "skeleton.db"))
    monkeypatch.setenv("PD_DISABLE_SCHEDULER", "1")
    from portfolio_dash.shared.config import get_settings
    get_settings.cache_clear()
    from portfolio_dash.api.app import create_app
    with TestClient(create_app()) as client:
        yield client
    get_settings.cache_clear()


def test_app_boots_and_health_ok(skeleton_client: TestClient) -> None:
    r = skeleton_client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["version"]  # single-source app version now included (post-deploy check)


def test_unknown_api_route_uses_error_envelope(skeleton_client: TestClient) -> None:
    r = skeleton_client.get("/api/does-not-exist")
    assert r.status_code == 404
    body = r.json()
    assert set(body["error"]) >= {"code", "message"}
    assert body["error"]["code"] == "not_found"


def test_lifespan_creates_insight_evaluations(skeleton_client: TestClient) -> None:
    # ai-score endpoint reads insight_evaluations; an empty boot returns the zeroed shape,
    # proving the lifespan created the table (spec 04c).
    r = skeleton_client.get("/api/ai-score")
    assert r.status_code == 200
    body = r.json()
    assert body["totals"]["n"] == 0
    assert body["rows"] == []


def test_lifespan_registers_evolution_runners(skeleton_client: TestClient) -> None:
    # The app wires the Loop-2/3 runners to the api seam at startup.
    from portfolio_dash.scheduler import jobs

    assert jobs.get_evaluation_runner() is not None
    assert jobs.get_calibration_runner() is not None


def test_evolution_jobs_in_scheduler_config(skeleton_client: TestClient) -> None:
    # The static evaluate/calibrate jobs are seeded into schedule_config (spec 04c).
    r = skeleton_client.get("/api/scheduler/jobs")
    assert r.status_code == 200
    job_ids = {row["id"] for row in r.json()["jobs"]}
    assert "evaluate_insights" in job_ids
    assert "generate_calibrations" in job_ids
