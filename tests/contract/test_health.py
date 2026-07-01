"""Contract test: /api/health carries the single-source app version.

The version is served from ``portfolio_dash.__version__`` (the one source the UI and
packaging both use), so ``curl /api/health`` is a quick post-deploy version check.
"""

from fastapi.testclient import TestClient

from portfolio_dash import __version__


def test_health_reports_status_and_version(api_client: TestClient) -> None:
    r = api_client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    # Same single source the sidebar brand tag + settings 一般 row display.
    assert body["version"] == __version__
    assert body["version"]  # non-empty
