"""DEF-008 (functional test manual A-05 / A-06, 2026-09-23): ONE overdraft sentence.

The verifier's two withdrawals, as the owner read them:

* 3,033,799 TWD against a 3,033,798 pool —
  「出金金額 3033799 TWD 超過 TW Broker 的 TWD 帳戶現金 3033798.0000 — 出金不可透支…」:
  no day, the English ``accounts.name``, and the pool at four decimals;
* a back-dated 4,000,000 —
  「此筆出金會使 TW Broker 的 TWD 現金於 2026-07-17 降至 -253820.0000（出金日早於資金到位）…」.

Five producers told that one fact (the withdraw and 換匯 doors' two branches each, plus the
edit/delete door's ``negative_cash``). They now share ``validate.cash_dip_sentence``: the
account as a ``{account:<id>}`` token (``shared/account_ref.py``), THE DAY in every branch —
the withdrawal's own day when that day is short (「出金當日」) — and the figure at the
currency's minor unit with thousands and a U+2212 minus.

The expectations are LITERALS on purpose: a pin that derives from the code it guards has
stopped being a pin.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import insert_cash_movement
from portfolio_dash.data_ingestion.validate import cash_amount_text, cash_dip_sentence
from portfolio_dash.shared.enums import Currency
from tests.conftest import DashboardClientFactory

_NOW = datetime(2026, 9, 23, 12, 0, tzinfo=ZoneInfo("Asia/Taipei"))


def _dep(conn: sqlite3.Connection, acct: str, day: date, ccy: Currency, amt: str,
         kind: str = "DEPOSIT") -> None:
    insert_cash_movement(conn, account_id=acct, move_date=day, kind=kind, ccy=ccy,
                         amount=Decimal(amt))


def _same_day(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    _dep(conn, "tw_broker", date(2026, 9, 1), Currency.TWD, "3033798")
    _dep(conn, "schwab", date(2026, 9, 1), Currency.USD, "12345.60")
    _dep(conn, "schwab", date(2026, 9, 1), Currency.TWD, "1000")


def _backdated(conn: sqlite3.Connection) -> None:
    """1,000,000 on 07-01, all of it spent on 07-17 — so 07-14 has the money, 07-17 does
    not once a 253,820 withdrawal is slipped in before it."""
    seed_accounts(conn)
    _dep(conn, "tw_broker", date(2026, 7, 1), Currency.TWD, "1000000")
    _dep(conn, "tw_broker", date(2026, 7, 17), Currency.TWD, "1000000", kind="WITHDRAW")


def _post(client: TestClient, path: str, body: dict[str, object]) -> dict[str, object]:
    r = client.post(path, json=body)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert isinstance(err, dict)
    return err


# --- the formatter ---------------------------------------------------------------------


def test_cash_amount_text_is_the_minor_unit_with_thousands_and_a_real_minus() -> None:
    assert cash_amount_text(Decimal("3033798.0000"), Currency.TWD) == "3,033,798"
    assert cash_amount_text(Decimal("-253820.0000"), Currency.TWD) == "−253,820"
    assert cash_amount_text(Decimal("-0.40"), Currency.USD) == "−0.40"
    assert cash_amount_text(Decimal("1234.5"), Currency.MYR) == "1,234.50"
    assert cash_amount_text(Decimal("0"), Currency.TWD) == "0"
    # A dip that rounds to zero keeps its digits rather than printing 「降至 −0」.
    assert cash_amount_text(Decimal("-0.4"), Currency.TWD) == "−0.4"


def test_the_sentence_names_the_pool_the_day_and_the_figure() -> None:
    assert cash_dip_sentence(
        what="出金", account_id="tw_broker", ccy=Currency.TWD, on=date(2026, 9, 23),
        low=Decimal("-1.0000"), cause="出金當日",
    ) == "此筆出金會使 {account:tw_broker} 的 TWD 現金於 2026-09-23 降至 −1（出金當日）"
    assert cash_dip_sentence(
        what="", account_id="schwab", ccy=Currency.USD, on=None, low=Decimal("-5"),
        cause=None,
    ) == "此筆會使 {account:schwab} 的 USD 現金於某時點降至 −5.00"


# --- the withdraw door: both branches ----------------------------------------------------


def test_a_same_day_overdraft_names_its_own_day_and_the_resulting_dip(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_same_day, now=_NOW)
    err = _post(client, "/api/cash/movements", {
        "account_id": "tw_broker", "date": "2026-09-23", "kind": "withdraw",
        "ccy": "TWD", "amount": "3033799"})
    assert err["code"] == "withdraw_insufficient_balance"
    assert err["message"] == (
        "此筆出金會使 {account:tw_broker} 的 TWD 現金於 2026-09-23 降至 −1（出金當日）"
        "— 出金不可透支，請先補登入金或換匯")


def test_a_same_day_overdraft_in_usd_prints_cents_with_thousands(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_same_day, now=_NOW)
    err = _post(client, "/api/cash/movements", {
        "account_id": "schwab", "date": "2026-09-23", "kind": "withdraw",
        "ccy": "USD", "amount": "22345.60"})
    assert err["message"] == (
        "此筆出金會使 {account:schwab} 的 USD 現金於 2026-09-23 降至 −10,000.00（出金當日）"
        "— 出金不可透支，請先補登入金或換匯")


def test_a_backdated_overdraft_names_the_day_the_pool_bottoms(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_backdated, now=_NOW)
    err = _post(client, "/api/cash/movements", {
        "account_id": "tw_broker", "date": "2026-07-14", "kind": "withdraw",
        "ccy": "TWD", "amount": "253820"})
    assert err["message"] == (
        "此筆出金會使 {account:tw_broker} 的 TWD 現金於 2026-07-17 降至 −253,820"
        "（出金日早於資金到位）— 出金不可透支，請先補登入金或換匯")


# --- the 換匯 door: the same sentence --------------------------------------------------


def test_a_same_day_fx_overdraft_uses_the_same_sentence(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_same_day, now=_NOW)
    err = _post(client, "/api/cash/fx", {
        "account_id": "schwab", "date": "2026-09-23", "from_ccy": "TWD",
        "from_amt": "1001", "to_ccy": "USD", "to_amt": "31"})
    assert err["code"] == "fx_insufficient_balance"
    assert err["message"] == (
        "此筆換匯會使 {account:schwab} 的 TWD 現金於 2026-09-23 降至 −1（換匯當日）"
        "— 換匯不可透支（不提供融資）")


# --- the edit/delete door: negative_cash -------------------------------------------------


def test_deleting_the_funding_deposit_names_the_account_by_token(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_backdated, now=_NOW)
    rows = client.get("/api/cash").json()["movements"]["rows"]
    funding = next(r for r in rows if r["kind"] == "deposit")
    r = client.delete(f"/api/cash/movements/{funding['id']}")
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "negative_cash"
    assert err["message"] == (
        "此筆會使 {account:tw_broker} 的 TWD 現金於 2026-07-17 降至 −1,000,000 "
        "— 通常代表漏記入金或換匯；確認無誤可強制寫入")


# --- the other account sentences on these doors -----------------------------------------


def test_unknown_accounts_on_the_cash_doors_use_the_one_shared_sentence(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_same_day, now=_NOW)
    fx = client.post("/api/cash/fx", json={
        "account_id": "zz_nope", "date": "2026-09-23", "from_ccy": "TWD",
        "from_amt": "1", "to_ccy": "USD", "to_amt": "1"})
    assert fx.status_code == 400 and fx.json()["error"]["message"] == "帳戶 zz_nope 不存在"
    acq = client.get("/api/cash/acq-rate",
                     params={"account_id": "zz_nope", "ccy": "USD", "on": "2026-09-23"})
    assert acq.status_code == 400 and acq.json()["error"]["message"] == "帳戶 zz_nope 不存在"
    stmt = client.get("/api/cash/statement", params={"account": "zz_nope"})
    assert stmt.status_code == 404, stmt.text
    assert stmt.json()["error"]["message"] == "帳戶 zz_nope 不存在"


def test_the_rebate_door_names_a_non_rebate_account_by_token(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_same_day, now=_NOW)
    r = client.post("/api/rebates/confirm", json={
        "account_id": "schwab", "month": "2026-08", "amount": "10"})
    assert r.status_code == 400, r.text
    msg = r.json()["error"]["message"]
    assert "{account:schwab}" in msg and "無折讓款設定" in msg, msg
