"""DEF-090 / DEF-091: a cash guard compares the pool DAY BY DAY, and names the first short day.

DEF-090 (verifier R13, A-06): the Schwab TWD pool sat at −220,000 from 2026-01-12; a withdrawal
back-dated to 07-17 left 07-19 and 07-20 at −148,000 / −153,000 and was written, because the
guard compared only the timeline's lowest point before and after (−220,000 both times). The
same comparison sat in the 換匯 guard and in the import-batch undo's ack-able check.

DEF-091 (owner ruling 2B, 2026-10-01): the refusal names the FIRST day the pool is short and
its lowest point — the day the money must be there by, and how much is missing. It named the
lowest day only (M5-07).

Golden schwab TWD pool: −32,000 from the 2026-01-08 conversion, nothing after it. Each case
below first funds it on 03-01, so the old dip (01-08…02-28) is older and DEEPER than anything
the case then creates — exactly DEF-090's shape.
"""

import sqlite3
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import insert_cash_movement
from portfolio_dash.shared.cash_dip import Dip, caused_dip, new_dip
from portfolio_dash.shared.enums import Currency

D = date.fromisoformat


def _n(v: str) -> Decimal:
    return Decimal(v)


# --- the pure rule -----------------------------------------------------------------------

_OLD = [(D("2026-01-12"), _n("-220000")), (D("2026-07-01"), _n("1000")),
        (D("2026-07-17"), _n("501000")), (D("2026-07-19"), _n("200000")),
        (D("2026-07-20"), _n("195000")), (D("2026-07-21"), _n("2195000"))]
#: The verifier's repro: 348,000 out on 07-17.
_NEW = [(D("2026-01-12"), _n("-220000")), (D("2026-07-01"), _n("1000")),
        (D("2026-07-17"), _n("153000")), (D("2026-07-19"), _n("-148000")),
        (D("2026-07-20"), _n("-153000")), (D("2026-07-21"), _n("1847000"))]


def test_a_new_stretch_shallower_than_an_older_dip_is_found() -> None:
    assert new_dip(_OLD, _NEW) == Dip(first=D("2026-07-19"), low=_n("-153000"),
                                      low_on=D("2026-07-20"))
    assert caused_dip(_OLD, _NEW) == new_dip(_OLD, _NEW)


def test_an_untouched_older_dip_is_never_reported_as_new() -> None:
    assert new_dip(_OLD, _OLD) is None
    assert caused_dip(_OLD, _OLD) is None


def test_deepening_a_day_that_was_already_short() -> None:
    """Hard guards refuse it; the ack-able undo asks only below the pool's old lowest point."""
    before = [(D("2026-01-05"), _n("-500")), (D("2026-02-01"), _n("-400"))]
    after = [(D("2026-01-05"), _n("-500")), (D("2026-02-01"), _n("-450"))]
    assert new_dip(before, after) == Dip(D("2026-02-01"), _n("-450"), D("2026-02-01"))
    assert caused_dip(before, after) is None
    deeper = [(D("2026-01-05"), _n("-500")), (D("2026-02-01"), _n("-600"))]
    assert caused_dip(before, deeper) == Dip(D("2026-02-01"), _n("-600"), D("2026-02-01"))


def test_a_deposit_is_never_a_dip() -> None:
    raised = [(d, b + _n("10")) for d, b in _OLD]
    assert new_dip(_OLD, raised) is None and caused_dip(_OLD, raised) is None


# --- the doors ---------------------------------------------------------------------------


def _fund(client: TestClient) -> None:
    """schwab TWD: +100,000 on 03-01 (→ 68,000), −50,000 on 04-01 (→ 18,000), −5,000 on
    04-10 (→ 13,000). The older −32,000 dip stays the pool's lowest point throughout."""
    for body in ({"date": "2026-03-01", "kind": "deposit", "amount": "100000"},
                 {"date": "2026-04-01", "kind": "withdraw", "amount": "50000"},
                 {"date": "2026-04-10", "kind": "withdraw", "amount": "5000"}):
        r = client.post("/api/cash/movements", json={
            "account_id": "schwab", "ccy": "TWD", **body})
        assert r.status_code == 201, r.json()


def test_withdraw_that_opens_a_later_stretch_is_refused_and_names_both_days(
    api_client: TestClient,
) -> None:
    """30,000 out on 03-15 is covered on its own day (68,000) but leaves 04-01 at −12,000 and
    04-10 at −17,000 — shallower than the old −32,000, so the old comparison wrote it."""
    _fund(api_client)
    r = api_client.post("/api/cash/movements", json={
        "account_id": "schwab", "date": "2026-03-15", "kind": "withdraw",
        "ccy": "TWD", "amount": "30000"})
    assert r.status_code == 422, r.json()
    err = r.json()["error"]
    assert err["code"] == "withdraw_insufficient_balance"
    assert err["message"] == (
        "此筆出金會使 {account:schwab} 的 TWD 現金自 2026-04-01 起為負，最低於 2026-04-10 降至 "
        "−17,000（出金日早於資金到位）— 出金不可透支，請先補登入金或換匯")
    # A withdrawal that leaves every later day covered is still written.
    ok = api_client.post("/api/cash/movements", json={
        "account_id": "schwab", "date": "2026-03-15", "kind": "withdraw",
        "ccy": "TWD", "amount": "10000"})
    assert ok.status_code == 201, ok.json()


def test_withdraw_short_on_its_own_day_names_that_day_and_the_lowest(
    api_client: TestClient,
) -> None:
    """The covering-balance branch: 80,000 on 03-15 is short that day (68,000) and lowest on
    04-10 (−67,000)."""
    _fund(api_client)
    r = api_client.post("/api/cash/movements", json={
        "account_id": "schwab", "date": "2026-03-15", "kind": "withdraw",
        "ccy": "TWD", "amount": "80000"})
    assert r.status_code == 422, r.json()
    assert r.json()["error"]["message"] == (
        "此筆出金會使 {account:schwab} 的 TWD 現金自 2026-03-15 起為負，最低於 2026-04-10 降至 "
        "−67,000（出金當日）— 出金不可透支，請先補登入金或換匯")


def test_fx_that_opens_a_later_stretch_is_refused(api_client: TestClient) -> None:
    _fund(api_client)
    r = api_client.post("/api/cash/fx", json={
        "account_id": "schwab", "date": "2026-03-15", "from_ccy": "TWD", "from_amt": "30000",
        "to_ccy": "USD", "to_amt": "950"})
    assert r.status_code == 422, r.json()
    err = r.json()["error"]
    assert err["code"] == "fx_insufficient_balance"
    assert ("自 2026-04-01 起為負，最低於 2026-04-10 降至 −17,000（換匯日早於資金到位）"
            in err["message"]), err["message"]


def test_batch_undo_that_opens_a_later_stretch_asks_first(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """A cash batch deposits 20,000 on 03-01; a hand withdrawal of 80,000 on 04-01 spends it.
    Undoing the batch leaves 04-01 at −12,000 — shallower than the old −32,000, so the old
    comparison deleted it without asking."""
    insert_cash_movement(golden_db, account_id="schwab", move_date=D("2026-02-20"),
                         kind="DEPOSIT", ccy=Currency.TWD, amount=_n("100000"))
    r = api_client.post("/api/import/commit", json={
        "kind": "cash", "csv_text": "account,date,kind,ccy,amount\n"
                                    "schwab,2026-03-01,DEPOSIT,TWD,20000\n",
        "ack_warnings": True, "source_name": "t.csv"})
    assert r.status_code == 200, r.text
    batch = r.json()["import_batch_id"]
    insert_cash_movement(golden_db, account_id="schwab", move_date=D("2026-04-01"),
                         kind="WITHDRAW", ccy=Currency.TWD, amount=_n("80000"))
    r = api_client.delete(f"/api/import/batches/{batch}")
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "negative_cash"
    assert "{account:schwab} 的 TWD 現金於 2026-04-01 降至 −12,000" in err["message"], err
    assert api_client.delete(
        f"/api/import/batches/{batch}?ack_negative=true").status_code == 200
