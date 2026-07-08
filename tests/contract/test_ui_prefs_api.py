"""Contract: GET/PUT /api/ui-prefs — the backend-persisted 每頁筆數 (WPC 2026-07-07)."""

from fastapi.testclient import TestClient


def test_get_defaults_to_50(api_client: TestClient) -> None:
    r = api_client.get("/api/ui-prefs")
    assert r.status_code == 200
    assert r.json() == {"page_size": 50}


def test_put_persists_allowed_value(api_client: TestClient) -> None:
    r = api_client.put("/api/ui-prefs", json={"page_size": 100})
    assert r.status_code == 200
    assert r.json() == {"page_size": 100}
    # round-trip: a fresh GET reads the persisted value
    assert api_client.get("/api/ui-prefs").json() == {"page_size": 100}


def test_put_rejects_disallowed_value(api_client: TestClient) -> None:
    r = api_client.put("/api/ui-prefs", json={"page_size": 37})
    assert r.status_code == 400
    body = r.json()
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["field"] == "page_size"
    # nothing written on refusal
    assert api_client.get("/api/ui-prefs").json() == {"page_size": 50}


def test_every_allowed_option_accepted(api_client: TestClient) -> None:
    for value in (20, 50, 100, 200):
        r = api_client.put("/api/ui-prefs", json={"page_size": value})
        assert r.status_code == 200
        assert r.json() == {"page_size": value}
