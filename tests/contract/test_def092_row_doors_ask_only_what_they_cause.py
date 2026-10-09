"""DEF-092 (owner ruling A, 2026-10-02): a single-row cash edit / delete asks only about a dip
it CAUSES.

The verifier's R14 observation: these ack-able doors reported ANY dip in the would-be pool
and called it 「此筆會使…」. Deleting a withdrawal from the Schwab TWD pool — which only raises
balances — read 「此筆會使…於 2026-01-12 降至 −220,000」, a dip that was there before and has
nothing to do with the row, while the import-batch undo already asked only about a dip the
change caused (``shared/cash_dip.py::caused_dip``). The three doors now share that rule.

Golden schwab TWD: −32,000 from the 2026-01-08 conversion, nothing after it — the unrelated
older dip every case below sits beside.
"""

from fastapi.testclient import TestClient


def _post(client: TestClient, body: dict[str, str]) -> int:
    r = client.post("/api/cash/movements", json={"account_id": "schwab", "ccy": "TWD", **body})
    assert r.status_code == 201, r.json()
    return int(r.json()["id"])


def test_deleting_a_withdrawal_beside_an_older_dip_does_not_ask(api_client: TestClient) -> None:
    _post(api_client, {"date": "2026-03-01", "kind": "deposit", "amount": "100000"})
    out = _post(api_client, {"date": "2026-04-01", "kind": "withdraw", "amount": "10000"})
    r = api_client.delete(f"/api/cash/movements/{out}")
    assert r.status_code == 200, r.json()


def test_an_edit_that_opens_a_new_stretch_still_asks_and_names_it(
    api_client: TestClient,
) -> None:
    """Cutting the 03-01 deposit from 100,000 to 20,000 leaves 03-01 at −12,000 and 04-01 at
    −22,000 — a stretch the edit opens, so it asks, naming that stretch, not 01-08."""
    dep = _post(api_client, {"date": "2026-03-01", "kind": "deposit", "amount": "100000"})
    _post(api_client, {"date": "2026-04-01", "kind": "withdraw", "amount": "10000"})
    r = api_client.put(f"/api/cash/movements/{dep}", json={
        "account_id": "schwab", "date": "2026-03-01", "kind": "deposit", "ccy": "TWD",
        "amount": "20000"})
    assert r.status_code == 422, r.json()
    err = r.json()["error"]
    assert err["code"] == "negative_cash"
    assert err["message"].startswith(
        "此筆會使 {account:schwab} 的 TWD 現金自 2026-03-01 起為負，最低於 2026-04-01 降至 "
        "−22,000"), err["message"]
    ok = api_client.put(f"/api/cash/movements/{dep}", json={
        "account_id": "schwab", "date": "2026-03-01", "kind": "deposit", "ccy": "TWD",
        "amount": "20000", "ack_negative": True})
    assert ok.status_code == 200, ok.json()


def test_deleting_a_conversion_beside_an_older_dip_does_not_ask(api_client: TestClient) -> None:
    """31,000 TWD → 1,000 USD on 03-05: deleting it raises TWD and returns USD to 0 — no new
    dip in either pool, so the older TWD dip (01-08) is not quoted."""
    _post(api_client, {"date": "2026-03-01", "kind": "deposit", "amount": "100000"})
    fx = api_client.post("/api/cash/fx", json={
        "account_id": "schwab", "date": "2026-03-05", "from_ccy": "TWD", "from_amt": "31000",
        "to_ccy": "USD", "to_amt": "1000"})
    assert fx.status_code == 201, fx.json()
    r = api_client.delete(f"/api/ledgers/fx/{fx.json()['id']}")
    assert r.status_code == 200, r.json()
