from fastapi.testclient import TestClient


def test_get_alert_rules(api_client: TestClient) -> None:
    r = api_client.get("/api/alert-rules")
    assert r.status_code == 200
    rules = {row["id"]: row for row in r.json()["rules"]}
    assert rules["single_weight"]["value"] == "0.30"
    assert rules["single_weight"]["unit"] == "ratio"
    assert rules["single_weight"]["min"] == "0.05" and rules["single_weight"]["max"] == "1"
    assert rules["quota_low"]["value"] is None
    # calib_gap is now a rule (spec 03/04 I1): threshold in PERCENTAGE POINTS, default 15pp.
    assert rules["calib_gap"]["value"] == "15"
    assert rules["calib_gap"]["unit"] == "pp"
    assert rules["calib_gap"]["min"] == "5" and rules["calib_gap"]["max"] == "50"
    # calibration_regression stays a spec-04c EVENT (alert_events), NOT a rule here (W3).
    assert "calibration_regression" not in rules
    # FU-D28: target_cross is a toggle-only rule (no numeric threshold — per-symbol targets).
    assert rules["target_cross"]["value"] is None
    assert rules["target_cross"]["unit"] is None
    assert rules["target_cross"]["enabled"] is True


def test_put_toggle_target_cross_off_round_trips(api_client: TestClient) -> None:
    # FU-D28: the on/off toggle persists (value:null ignored — it is a toggle-only rule).
    r = api_client.put("/api/alert-rules",
                       json={"rules": [{"id": "target_cross", "enabled": False, "value": None}]})
    assert r.status_code == 200
    rules = {row["id"]: row for row in r.json()["rules"]}
    assert rules["target_cross"]["enabled"] is False and rules["target_cross"]["value"] is None


def test_put_alert_rules_merges_over_current(api_client: TestClient) -> None:
    body = {"rules": [
        {"id": "single_weight", "enabled": True, "value": "0.25"},
        {"id": "fx_drift", "enabled": False, "value": "0.03"},
    ]}
    r = api_client.put("/api/alert-rules", json=body)
    assert r.status_code == 200
    rules = {row["id"]: row for row in r.json()["rules"]}
    assert rules["single_weight"]["value"] == "0.25"
    assert rules["fx_drift"]["enabled"] is False
    assert rules["sector_weight"]["value"] == "0.60"  # omitted -> default preserved


def test_put_out_of_bounds_400(api_client: TestClient) -> None:
    r = api_client.put("/api/alert-rules",
                       json={"rules": [{"id": "single_weight", "enabled": True, "value": "2.0"}]})
    assert r.status_code == 400 and r.json()["error"]["code"] == "validation_error"
