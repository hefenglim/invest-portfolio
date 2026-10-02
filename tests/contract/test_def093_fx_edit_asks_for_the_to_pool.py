"""DEF-093 (owner ruling A, 2026-10-03): editing a conversion asks about the TO-pool too.

The verifier's R15 observation: ``PUT /api/ledgers/fx/{id}`` ran only the hard FROM-pool rule
(FU-D34). Editing a 換入金額 from 319,000 to 1,000 took the money out of the Schwab TWD pool and
left 07-19 / 07-20 at −199,000 / −204,000 with no question, while deleting the same row asked.
The edit now asks the delete door's question for every pool it touches (ack-able), and the
from-pool's hard refusal is unchanged.

Golden schwab: TWD → USD 32,000 → 1,000 on 2026-01-08, and the USD paid for AAPL on 01-10 (USD
0 after it). Each case funds the TWD side first (100,000 on 01-02), so the from-pool's hard
rule passes and only the to-pool is in question.
"""

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import insert_cash_movement
from portfolio_dash.shared.enums import Currency


def _golden_fx(client: TestClient) -> dict[str, Any]:
    rows: list[dict[str, Any]] = client.get("/api/ledgers/fx", params={"limit": 500}).json()["rows"]
    (fx,) = [r for r in rows if r["account_id"] == "schwab" and r["date"] == "2026-01-08"]
    return fx


def _fund_twd(conn: sqlite3.Connection) -> None:
    insert_cash_movement(conn, account_id="schwab", move_date=date(2026, 1, 2),
                         kind="DEPOSIT", ccy=Currency.TWD, amount=Decimal("100000"))
    conn.commit()


def _edit(client: TestClient, fx: dict[str, Any], **change: Any) -> Any:
    body = {"account_id": fx["account_id"], "date": fx["date"], "from_ccy": fx["from_ccy"],
            "from_amt": fx["from_amt"], "to_ccy": fx["to_ccy"], "to_amt": fx["to_amt"],
            **change}
    return client.put(f"/api/ledgers/fx/{fx['id']}", json=body)


def test_a_smaller_to_amount_that_strands_a_later_spend_asks(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """1,000 USD in, 1,000 spent on AAPL two days later: 500 in leaves USD at −500 from 01-10."""
    _fund_twd(golden_db)
    fx = _golden_fx(api_client)
    r = _edit(api_client, fx, to_amt="500")
    assert r.status_code == 422, r.json()
    err = r.json()["error"]
    assert err["code"] == "negative_cash"
    assert err["message"] == (
        "此筆會使 {account:schwab} 的 USD 現金於 2026-01-10 降至 −500.00 — "
        "通常代表漏記入金或換匯；確認無誤可強制寫入")
    assert _golden_fx(api_client)["to_amt"] == fx["to_amt"]   # nothing written
    ok = _edit(api_client, fx, to_amt="500", ack_negative=True)
    assert ok.status_code == 200, ok.json()
    assert Decimal(_golden_fx(api_client)["to_amt"]) == Decimal("500")


def test_an_edit_that_strands_nothing_does_not_ask(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    _fund_twd(golden_db)
    fx = _golden_fx(api_client)
    assert _edit(api_client, fx, to_amt="1200").status_code == 200


def test_the_from_pool_stays_a_hard_refusal_an_ack_cannot_pass(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """Spending 200,000 TWD the pool never held is still FU-D34's hard 422, ack or not."""
    _fund_twd(golden_db)
    fx = _golden_fx(api_client)
    for extra in ({}, {"ack_negative": True}):
        r = _edit(api_client, fx, from_amt="200000", to_amt="6200", **extra)
        assert r.status_code == 422, r.json()
        assert r.json()["error"]["code"] == "fx_insufficient_balance"
