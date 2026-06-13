"""Integration smokes: confirm the spec 13/14/16 settings routers are mounted on the real app.

The per-router behavior is covered by each router's own contract test; these only verify the
real create_app() mounts them (no route collision / import wiring break).
"""

from fastapi.testclient import TestClient


def test_accounts_mounted(api_client: TestClient) -> None:
    r = api_client.get("/api/accounts")
    assert r.status_code == 200
    assert "accounts" in r.json()


def test_datasources_mounted(api_client: TestClient) -> None:
    r = api_client.get("/api/datasources")
    assert r.status_code == 200
    assert "sources" in r.json()


def test_llm_config_mounted(api_client: TestClient) -> None:
    r = api_client.get("/api/llm/config")
    assert r.status_code == 200
    assert "models" in r.json()
