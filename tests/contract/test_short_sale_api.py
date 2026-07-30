"""API guard rails for the declared short sale + the date-aware 賣超 check (2026-07-31)."""

from typing import Any

from fastapi.testclient import TestClient


def _commit(client: TestClient, **kw: Any) -> Any:
    body = {"account_id": "schwab", "symbol": "AAPL", "side": "sell",
            "date": "2026-06-10", "shares": "100", "price": "260"}
    body.update(kw)
    return client.post("/api/input/manual/commit", json=body)


def test_backdated_sell_covered_only_by_a_later_buy_is_blocked(
    api_client: TestClient,
) -> None:
    """The 2026-07-30 defect: `current_shares` nets across ALL dates, so a sell dated before
    the buy that covers it slipped through and the replay then discarded the cost basis."""
    api_client.post("/api/input/manual/commit", json={
        "account_id": "schwab", "symbol": "AAPL", "side": "buy",
        "date": "2026-07-23", "shares": "100", "price": "366"})
    r = _commit(api_client)                      # dated BEFORE that buy
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "oversell_unacknowledged"
    msg = " ".join(i["text"] for i in r.json()["error"].get("issues", []))
    assert "2026-06-10" in msg, msg               # names the date it was short on


def test_declared_short_sale_needs_no_acknowledgement(api_client: TestClient) -> None:
    r = _commit(api_client, short_sale=True)
    assert r.status_code == 201, r.text
    rows = api_client.get("/api/ledgers/transactions", params={"limit": 500}).json()["rows"]
    row = next(t for t in rows if t["id"] == r.json()["txn_id"])
    assert row["side"] == "sell" and row["shares"] == "100"


def test_short_position_is_reported_signed_and_labelled(api_client: TestClient) -> None:
    _commit(api_client, short_sale=True)
    h = next(x for x in api_client.get("/api/dashboard").json()["holdings"]
             if x["symbol"] == "AAPL" and x["account_id"] == "schwab")
    assert h["shares"].startswith("-")
    assert h["short_open"] is True and h["oversold"] is False


def test_short_flag_is_off_by_default_so_a_typo_cannot_become_a_short(
    api_client: TestClient,
) -> None:
    r = _commit(api_client)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "oversell_unacknowledged"
