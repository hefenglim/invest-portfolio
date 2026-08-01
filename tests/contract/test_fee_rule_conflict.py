"""discount + rebate_rate describe ONE broker benefit; both on double-counts it."""

from typing import Any

from fastapi.testclient import TestClient


def _tw(client: TestClient) -> dict[str, Any]:
    body = client.get("/api/fee-rules").json()
    return next(rs for rs in body["rule_sets"] if rs["name"] == "tw")


def test_defaults_carry_no_conflict(api_client: TestClient) -> None:
    assert _tw(api_client)["conflicts"] == []


def test_discount_plus_rebate_raises_a_non_blocking_conflict(api_client: TestClient) -> None:
    r = api_client.put("/api/fee-rules/tw", json={"overrides": {"discount": "0.23"}})
    assert r.status_code == 200, r.text          # a WARNING, never a block
    c = r.json()["conflicts"]
    assert len(c) == 1
    assert set(c[0]["fields"]) == {"discount", "rebate_rate"}
    # The explanation must be plain language with the actual arithmetic, not jargon.
    assert "23" in c[0]["plain"] and "兩次" in c[0]["plain"]
    assert {tuple(o["set"].items()) for o in c[0]["options"]} == {
        (("discount", "1"),), (("rebate_rate", "0"),)}


def test_conflict_survives_a_reload_not_only_the_edit(api_client: TestClient) -> None:
    api_client.put("/api/fee-rules/tw", json={"overrides": {"discount": "0.23"}})
    assert _tw(api_client)["conflicts"]          # visible on a fresh GET too


def test_either_offered_resolution_clears_it(api_client: TestClient) -> None:
    for fix in ({"discount": "1"}, {"rebate_rate": "0"}):
        api_client.post("/api/fee-rules/tw/reset")
        api_client.put("/api/fee-rules/tw", json={"overrides": {"discount": "0.23"}})
        assert _tw(api_client)["conflicts"]
        r = api_client.put("/api/fee-rules/tw", json={"overrides": fix})
        assert r.json()["conflicts"] == [], fix
