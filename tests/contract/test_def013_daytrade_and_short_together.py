"""DEF-013 (functional test manual B-10, owner ruling 2026-09-24): 「當沖＋放空」 together is
ALLOWED (a TW same-day short-then-cover), and every door can express — and the ledger shows —
the pair.

Both flags are persisted COLUMNS of ``transactions`` (``daytrade`` since audit MED-1,
``short_sale`` since 2026-07-31), so the ledger row reads them off itself; no new column and no
parse of ``fee_rule_snapshot`` (which a broker-supplied fee/tax leaves without any rate).

Pinned through the real doors: the manual entry books the pair at the 當沖 rate (0.15%, which
outranks ETF — markets-and-fees.md QA-19) with a declared short; the CSV door carries both
columns; the ledger wire exposes both flags; and the edit door can set and clear each one.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

D = Decimal


def _row(client: TestClient, txn_id: int) -> dict[str, Any]:
    rows = client.get("/api/ledgers/transactions", params={"limit": 500}).json()["rows"]
    return dict(next(r for r in rows if r["id"] == txn_id))


def _sell_body(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"account_id": "tw_broker", "symbol": "2330", "side": "sell",
                            "date": "2026-06-10", "shares": "1500", "price": "600",
                            "daytrade": True, "short_sale": True}
    body.update(over)
    return body


def test_the_manual_door_books_the_pair_at_the_daytrade_rate_as_a_declared_short(
    api_client: TestClient,
) -> None:
    pv = api_client.post("/api/input/manual/preview", json=_sell_body()).json()
    # 1,500 x 600 = 900,000 x 0.15% = 1,350 (floored to whole NT$).
    assert D(pv["tax"]) == D("1350"), pv["tax"]
    assert not [i for i in pv["issues"] if i["code"] == "sell_exceeds_holdings"], (
        "a declared short must not raise 賣超")
    r = api_client.post("/api/input/manual/commit", json=_sell_body())
    assert r.status_code == 201, r.text
    row = _row(api_client, r.json()["txn_id"])
    assert row["daytrade"] is True and row["short_sale"] is True, row
    assert D(row["tax"]) == D("1350")


def test_the_csv_door_carries_both_columns(api_client: TestClient) -> None:
    csv_text = ("account,symbol,side,date,shares,price,daytrade,short_sale\n"
                "tw_broker,2330,sell,2026-06-10,1500,600,1,1\n")
    r = api_client.post("/api/import/commit", json={
        "kind": "transactions", "csv_text": csv_text, "ack_warnings": True})
    assert r.status_code == 200, r.text
    rows = api_client.get("/api/ledgers/transactions", params={"limit": 500}).json()["rows"]
    row = next(x for x in rows if x["side"] == "sell" and x["date"] == "2026-06-10")
    assert row["daytrade"] is True and row["short_sale"] is True, row
    assert D(row["tax"]) == D("1350")


def test_the_edit_door_sets_and_clears_each_flag(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/commit", json=_sell_body(shares="100"))
    txn = int(r.json()["txn_id"])
    base = _row(api_client, txn)
    body = {"account_id": base["account_id"], "symbol": base["symbol"], "side": "sell",
            "date": base["date"], "shares": base["shares"], "price": base["price"],
            "fee": base["fee"], "tax": base["tax"], "note": None}
    cleared = api_client.put(f"/api/ledgers/transactions/{txn}",
                             json={**body, "daytrade": False, "short_sale": False})
    assert cleared.status_code == 200, cleared.text
    row = _row(api_client, txn)
    assert row["daytrade"] is False and row["short_sale"] is False
    # 當沖 is a fee-bearing field: clearing it re-prices the sell at the 現股 0.3%.
    assert D(row["tax"]) == D("180"), row["tax"]
    again = api_client.put(f"/api/ledgers/transactions/{txn}",
                           json={**body, "daytrade": True, "short_sale": True})
    assert again.status_code == 200, again.text
    row = _row(api_client, txn)
    assert row["daytrade"] is True and row["short_sale"] is True
    assert D(row["tax"]) == D("90")


def test_the_printed_ledger_marks_the_pair_too(api_client: TestClient) -> None:
    """The 交易帳本 print report renders the same rows: the flags travel with them."""
    r = api_client.post("/api/input/manual/commit", json=_sell_body())
    assert r.status_code == 201, r.text
    html = api_client.post("/api/export/ledgers-report", json={}).content.decode("utf-8")
    sell_row = next(row for row in html.split("<tr>") if "1,500" in row)
    assert "當沖" in sell_row and "放空" in sell_row, sell_row
    buy_row = next(row for row in html.split("<tr>") if "1,000" in row and "買" in row)
    assert "當沖" not in buy_row and "放空" not in buy_row, buy_row
