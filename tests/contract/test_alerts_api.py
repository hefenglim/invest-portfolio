"""Contract tests: GET /api/alerts, the dashboard payload's embedded alerts, and the
PUT /api/alert-rules recompute. All three must reflect the SAME single rule engine."""

from fastapi.testclient import TestClient


def test_get_alerts(api_client: TestClient) -> None:
    r = api_client.get("/api/alerts")
    assert r.status_code == 200
    body = r.json()
    assert "as_of" in body and isinstance(body["alerts"], list)
    assert any(a["id"] == "single_weight:2330" for a in body["alerts"])


def test_dashboard_embeds_same_alerts(api_client: TestClient) -> None:
    dash = api_client.get("/api/dashboard").json()
    assert "alerts" in dash
    assert any(a["id"] == "single_weight:2330" for a in dash["alerts"])


def test_put_alert_rules_returns_recomputed_alerts(api_client: TestClient) -> None:
    r = api_client.put("/api/alert-rules",
                       json={"rules": [{"id": "single_weight", "enabled": False, "value": "0.30"}]})
    assert r.status_code == 200
    body = r.json()
    assert "rules" in body and "alerts" in body
    assert not any(a["id"].startswith("single_weight") for a in body["alerts"])
