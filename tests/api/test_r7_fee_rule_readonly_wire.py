"""R7 / QA-07 follow-up: ``rounding`` must be VISIBLE and read-only, not gone.

QA-07's repair made the field unwritable — correctly, because the fee engine branches on the
market and never read it, while stamping it into every row's permanent ``fee_rule_snapshot``
as "the regime that produced these numbers". But the wire builder iterated
``EDITABLE_FIELD_ORDER``, so removing the field from that set removed it from the payload
too, and the settings card silently lost a row. Invisible is a different wrong answer from
uneditable: TW's 無條件捨去 is FE-D3 (財政部 角以下免收), a market fact the owner should be
able to read and not change.

The ``editable`` flag is what lets the frontend tell those two apart, so it is what this
file pins — together with the property that matters operationally: saving a fee-rule set
still works, and a read-only field is never posted back.
"""

from typing import Any

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion import fee_overrides


def _sets(client: TestClient) -> dict[str, Any]:
    r = client.get("/api/fee-rules")
    assert r.status_code == 200
    return {s["name"]: s for s in r.json()["rule_sets"]}


def _field(client: TestClient, rule_set: str, key: str) -> dict[str, Any] | None:
    for f in _sets(client)[rule_set]["fields"]:
        if f["key"] == key:
            return dict(f)
    return None


def test_rounding_is_on_the_wire_and_marked_not_editable(api_client: TestClient) -> None:
    field = _field(api_client, "tw", "rounding")
    assert field is not None, "rounding vanished from the payload — invisible, not read-only"
    assert field["editable"] is False
    assert field["effective"] == "floor", "TW quantizes fee AND tax by 無條件捨去 (FE-D3)"


def test_every_other_field_is_still_editable(api_client: TestClient) -> None:
    """The flag must discriminate, not blanket-lock the card."""
    fields = _sets(api_client)["tw"]["fields"]
    editable = {f["key"] for f in fields if f["editable"]}
    locked = {f["key"] for f in fields if not f["editable"]}
    assert locked == {"rounding"}, locked
    assert "brokerage" in editable and "tax_normal" in editable


def test_us_and_my_report_their_own_regime_read_only(api_client: TestClient) -> None:
    for name in ("schwab", "moomoo_my"):
        field = _field(api_client, name, "rounding")
        assert field is not None, name
        assert field["editable"] is False, name
        assert field["effective"] == "half_up", name


def test_the_wire_flag_agrees_with_the_module_that_enforces_it(
    api_client: TestClient,
) -> None:
    """The flag is DERIVED, not a second opinion.

    A hand-maintained list on the router would be one more place to forget — which is the
    class of bug QA-07 itself was.
    """
    for f in _sets(api_client)["tw"]["fields"]:
        assert f["editable"] is fee_overrides.is_editable(f["key"]), f["key"]


def test_saving_a_rule_set_still_works_and_read_only_stays_put(
    api_client: TestClient,
) -> None:
    """The operational half: no 400 on save, and the locked field does not move."""
    r = api_client.put("/api/fee-rules/tw", json={"overrides": {"brokerage": "0.003"}})
    assert r.status_code == 200, r.text
    field = _field(api_client, "tw", "rounding")
    assert field is not None and field["effective"] == "floor"
    assert field["overridden"] is False


def test_an_attempted_rounding_override_is_refused_in_chinese(
    api_client: TestClient,
) -> None:
    """A caller that posts it anyway (a stale tab, a script) is told why, not ignored."""
    r = api_client.put("/api/fee-rules/tw", json={"overrides": {"rounding": "half_up"}})
    assert r.status_code == 400, r.text
    err = r.json()["error"]
    assert err["field"] == "rounding"
    assert any("一" <= ch <= "鿿" for ch in err["message"]), err["message"]
    field = _field(api_client, "tw", "rounding")
    assert field is not None and field["effective"] == "floor"
