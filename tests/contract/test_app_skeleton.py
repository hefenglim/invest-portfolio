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
    assert r.json() == {"status": "ok"}


def test_unknown_api_route_uses_error_envelope(skeleton_client: TestClient) -> None:
    r = skeleton_client.get("/api/does-not-exist")
    assert r.status_code == 404
    body = r.json()
    assert set(body["error"]) >= {"code", "message"}
    assert body["error"]["code"] == "not_found"
