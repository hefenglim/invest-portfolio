"""Contract test: /api/health carries the single-source build identity.

The version is served from ``portfolio_dash.__version__`` (the one source the UI and
packaging both use); ``commit`` / ``release`` come from ``shared.buildinfo`` (git
checkout identity), so ``curl /api/health`` is a one-call post-deploy check of
version + commit + tag-release status.
"""

from fastapi.testclient import TestClient

from portfolio_dash import __version__


def test_health_reports_status_and_build_identity(api_client: TestClient) -> None:
    r = api_client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    # Same single source the sidebar brand tag + settings 一般 row display.
    assert body["version"] == __version__
    assert body["version"]  # non-empty
    # Build identity: always present, always non-empty strings. On the dev checkout
    # commit is a real short hash; the values themselves are environment-dependent so
    # the contract asserts shape, not content.
    assert isinstance(body["commit"], str) and body["commit"]
    assert isinstance(body["release"], str) and body["release"]
