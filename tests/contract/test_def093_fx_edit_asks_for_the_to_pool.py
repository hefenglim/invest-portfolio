"""DEF-093 (owner ruling A, 2026-10-03): editing a conversion asks about the TO-pool too.

The verifier's R15 observation: ``PUT /api/ledgers/fx/{id}`` ran only the hard FROM-pool rule
(FU-D34). Editing a 換入金額 from 319,000 to 1,000 took the money out of the Schwab TWD pool and
left 07-19 / 07-20 at −199,000 / −204,000 with no question, while deleting the same row asked.
The edit now asks the delete door's question for every pool it touches (ack-able), and the
from-pool's hard refusal is unchanged.

Golden schwab: TWD → USD 32,000 → 1,000 on 2026-01-08, and the USD paid for AAPL on 01-10 (USD
0 after it). Each case funds the TWD side first (100,000 on 01-02), so the from-pool's hard
rule passes and only the to-pool is in question.

The last three cases are the verifier's R16 note ①: three mutations of the guard that the
first cases let through — checking only the NEW account's pools, asking on any deeper day
(``new_dip`` instead of ``caused_dip``), and asking whenever the pool holds any negative day.
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


def test_moving_the_row_to_another_account_asks_for_the_account_it_leaves(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """Re-booked as a moomoo_my MYR → USD conversion: schwab keeps the AAPL buy, loses the USD.

    The new account's pools are both fine (MYR funded, USD only gains), so a guard that reads
    only ``edited.account_id`` writes this with no question.
    """
    insert_cash_movement(golden_db, account_id="moomoo_my", move_date=date(2026, 1, 2),
                         kind="DEPOSIT", ccy=Currency.MYR, amount=Decimal("10000"))
    golden_db.commit()
    fx = _golden_fx(api_client)
    r = _edit(api_client, fx, account_id="moomoo_my", from_ccy="MYR", from_amt="4400")
    assert r.status_code == 422, r.json()
    err = r.json()["error"]
    assert err["code"] == "negative_cash"
    assert err["message"] == (
        "此筆會使 {account:schwab} 的 USD 現金於 2026-01-10 降至 −1,000.00 — "
        "通常代表漏記入金或換匯；確認無誤可強制寫入")
    assert _golden_fx(api_client)["account_id"] == "schwab"   # nothing written


def _schwab_usd_already_short(conn: sqlite3.Connection) -> None:
    """USD −5,000 from 01-03 (the old low), −500 from 01-06, +500 on 01-08, −500 from 01-10."""
    _fund_twd(conn)
    insert_cash_movement(conn, account_id="schwab", move_date=date(2026, 1, 3),
                         kind="WITHDRAW", ccy=Currency.USD, amount=Decimal("5000"))
    insert_cash_movement(conn, account_id="schwab", move_date=date(2026, 1, 6),
                         kind="DEPOSIT", ccy=Currency.USD, amount=Decimal("4500"))
    conn.commit()


def test_deepening_an_already_short_day_above_the_old_low_does_not_ask(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """800 in takes 01-10 from −500 to −700: deeper, not new, and above the −5,000 old low.

    The delete door's rule (``caused_dip``) does not ask; the hard rule (``new_dip``) would.
    """
    _schwab_usd_already_short(golden_db)
    fx = _golden_fx(api_client)
    r = _edit(api_client, fx, to_amt="800")
    assert r.status_code == 200, r.json()
    assert Decimal(_golden_fx(api_client)["to_amt"]) == Decimal("800")


def test_an_old_unrelated_negative_day_does_not_make_every_edit_ask(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """1,200 in only adds money; the pool's −5,000 on 01-03 is not this edit's doing."""
    _schwab_usd_already_short(golden_db)
    fx = _golden_fx(api_client)
    r = _edit(api_client, fx, to_amt="1200")
    assert r.status_code == 200, r.json()
    assert Decimal(_golden_fx(api_client)["to_amt"]) == Decimal("1200")
