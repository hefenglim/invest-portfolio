from fastapi.testclient import TestClient


def test_refresh_quotes_all_markets(api_client: TestClient) -> None:
    r = api_client.post("/api/actions/refresh-quotes", json={})
    assert r.status_code == 200
    b = r.json()
    assert set(b["jobs"]) == {"quotes_tw", "quotes_us", "quotes_my"}
    assert len(b["run_ids"]) == 3 and all(isinstance(x, int) for x in b["run_ids"])


def test_refresh_quotes_subset(api_client: TestClient) -> None:
    r = api_client.post("/api/actions/refresh-quotes", json={"markets": ["TW"]})
    assert r.status_code == 200
    assert r.json()["jobs"] == ["quotes_tw"] and len(r.json()["run_ids"]) == 1


def test_refresh_quotes_unknown_market_400(api_client: TestClient) -> None:
    r = api_client.post("/api/actions/refresh-quotes", json={"markets": ["XX"]})
    assert r.status_code == 400 and r.json()["error"]["code"] == "validation_error"
