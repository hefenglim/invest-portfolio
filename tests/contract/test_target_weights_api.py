"""Contract tests: GET/PUT /api/target-weights (D8) + the rebalance_drift rule flowing
through BOTH alert surfaces (single-source equality) once a target creates drift."""

from typing import Any, cast

from fastapi.testclient import TestClient


def _registered(client: TestClient) -> list[dict[str, Any]]:
    r = client.get("/api/target-weights")
    assert r.status_code == 200
    return cast(list[dict[str, Any]], r.json()["symbols"])


def test_get_lists_registered_symbols_unset(api_client: TestClient) -> None:
    body = api_client.get("/api/target-weights").json()
    assert isinstance(body["symbols"], list) and body["symbols"]
    # Each row carries symbol + name + held flag + a (null when unset) weight.
    row = body["symbols"][0]
    assert set(row) == {"symbol", "name", "held", "weight"}
    assert all(s["weight"] is None for s in body["symbols"])  # nothing set yet
    assert body["sum"] == "0"


def test_put_round_trip(api_client: TestClient) -> None:
    syms = _registered(api_client)
    held = next(s for s in syms if s["held"])
    r = api_client.put("/api/target-weights", json={"weights": {held["symbol"]: "0.25"}})
    assert r.status_code == 200
    view = r.json()
    got = next(s for s in view["symbols"] if s["symbol"] == held["symbol"])
    assert got["weight"] == "0.25" and view["sum"] == "0.25"
    assert view["updated_at"] is not None
    # persisted: a fresh GET returns the same
    again = api_client.get("/api/target-weights").json()
    assert next(s for s in again["symbols"] if s["symbol"] == held["symbol"])["weight"] == "0.25"


def test_put_unknown_symbol_400(api_client: TestClient) -> None:
    r = api_client.put("/api/target-weights", json={"weights": {"NOSUCH": "0.1"}})
    assert r.status_code == 400
    assert r.json()["error"]["field"] == "weights"


def test_put_weight_out_of_range_400(api_client: TestClient) -> None:
    sym = _registered(api_client)[0]["symbol"]
    for bad in ("1.5", "0", "-0.1"):
        r = api_client.put("/api/target-weights", json={"weights": {sym: bad}})
        assert r.status_code == 400, bad


def test_put_sum_over_one_400(api_client: TestClient) -> None:
    syms = [s["symbol"] for s in _registered(api_client)]
    assert len(syms) >= 2
    r = api_client.put("/api/target-weights",
                       json={"weights": {syms[0]: "0.6", syms[1]: "0.6"}})
    assert r.status_code == 400
    assert "100%" in r.json()["error"]["message"]


def test_put_empty_clears(api_client: TestClient) -> None:
    sym = next(s for s in _registered(api_client) if s["held"])["symbol"]
    api_client.put("/api/target-weights", json={"weights": {sym: "0.2"}})
    r = api_client.put("/api/target-weights", json={"weights": {}})
    assert r.status_code == 200
    assert all(s["weight"] is None for s in r.json()["symbols"])
    assert r.json()["sum"] == "0"


def test_target_drift_fires_in_both_surfaces(api_client: TestClient) -> None:
    # Set a target far below the largest current weight -> rebalance_drift must appear in BOTH
    # the dashboard embed and GET /api/alerts, and the two arrays stay byte-identical.
    dash = api_client.get("/api/dashboard").json()
    top = max((h for h in dash["holdings"] if h.get("weight") is not None),
              key=lambda h: float(h["weight"]))
    api_client.put("/api/target-weights", json={"weights": {top["symbol"]: "0.01"}})

    dash_alerts = api_client.get("/api/dashboard").json()["alerts"]
    endpoint_alerts = api_client.get("/api/alerts").json()["alerts"]
    drift_id = f"rebalance_drift:{top['symbol']}"
    assert any(a["id"] == drift_id for a in dash_alerts)
    rd = next(a for a in dash_alerts if a["id"] == drift_id)
    assert rd["sev"] == "risk" and "%" in rd["detail"]
    # single-source equality preserved with the new fed rule present
    assert sorted(dash_alerts, key=lambda a: a["id"]) == \
        sorted(endpoint_alerts, key=lambda a: a["id"])
