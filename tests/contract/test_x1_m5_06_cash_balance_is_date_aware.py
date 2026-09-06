"""M5-06 (owner ruling 2026-09-06, option C): a cash balance is DATE-AWARE.

Measured on 2026-09-02: a deposit dated ``2099-01-01`` was written (201) and the 資金管理 page's
balance jumped by its full amount at once, while the dashboard's total-net-worth trend — whose
``daily_cash_series`` walks the calendar and stops at ``as_of`` — did not move on "today" at
all. Two screens disagreed about how much cash there is today by exactly the future row.

The ruling: 「今天的餘額」 only counts flows dated on or before today. Concretely:

* ``GET /api/cash`` balances (and the negative-pool list and the reporting total that derive
  from them) are as of the app clock's date, which the envelope now states as ``as_of``.
* ``GET /api/cash/statement`` still lists EVERY row — hiding a written row would make the
  ledger look like it lost it — but ``current_balance`` / the per-ccy ``balances`` are as of
  today, and each row carries ``future`` so the frontend can draw the line.
* The withdraw guard's covering balance is the pool's balance ON THE WITHDRAWAL'S OWN DATE
  (the equity side's ``shares_through`` precedent), and the running minimum keeps EVERY row,
  future ones included — otherwise a 2099 deposit could fund a withdrawal today whenever a
  pre-existing dip silences the running-minimum branch. That hole is closed here, and the
  counter-evidence (a future-dated flow still enters the running minimum) is pinned beside it.

"Today" is ``api.deps.get_now`` → ``shared.clock.app_now`` (Asia/Taipei) — never
``date.today()`` — which is what lets these tests freeze it at GOLDEN_NOW (2026-06-11).
"""

from datetime import date
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import StoredCashMovement
from portfolio_dash.portfolio.cash import cash_balances
from portfolio_dash.shared.enums import Currency
from tests.conftest import DashboardClientFactory
from tests.contract.test_spec17_financials import seed_full

_TODAY = "2026-06-11"  # GOLDEN_NOW's date

_FAR_FUTURE_DEPOSIT = {
    "account_id": "tw_broker", "date": "2099-01-01", "kind": "deposit",
    "ccy": "TWD", "amount": "777777", "note": "M5-06 far-future deposit",
}


def _cash(client: TestClient) -> dict[str, Any]:
    body: dict[str, Any] = client.get("/api/cash", params={"limit": 500}).json()
    return body


def _pools(body: dict[str, Any]) -> list[tuple[str, str, str]]:
    return [(b["account_id"], b["ccy"], b["amount"]) for b in body["balances"]]


def _mv(d: str, kind: str, amount: str, mid: int) -> StoredCashMovement:
    return StoredCashMovement(id=mid, account_id="acc", date=date.fromisoformat(d),
                              kind=kind, ccy=Currency.USD, amount=Decimal(amount), note=None)


# --- the finding: two screens, one answer ------------------------------------------------


def test_two_screens_agree_on_todays_cash_after_a_far_future_deposit(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The reported defect, end to end: after a 2099 deposit, ``GET /api/cash`` and the
    dashboard's last net-worth point must still agree on today's cash."""
    client = dashboard_client_factory(seed_full)
    before = _cash(client)
    assert client.post("/api/cash/movements", json=_FAR_FUTURE_DEPOSIT).status_code == 201
    after = _cash(client)
    # Every pool byte-identical — the future row is in the ledger, not in today's balance.
    assert _pools(after) == _pools(before)
    assert after["reporting_total"] == before["reporting_total"]
    assert after["negative_pools"] == before["negative_pools"]
    assert after["as_of"] == _TODAY
    assert any(m["date"] == "2099-01-01" for m in after["movements"]["rows"])
    # ...and the dashboard's "today" point reconstructs the SAME cash figure.
    points = client.get("/api/dashboard").json()["trend"]["points"]
    last = next(p for p in reversed(points) if p["net_worth"] is not None)
    assert (Decimal(last["net_worth"]) - Decimal(last["total_value"])
            == Decimal(after["reporting_total"]))


# --- counter-evidence: nothing dated on or before today moves ----------------------------


def test_every_golden_pool_is_byte_identical_with_and_without_the_bound(
    api_client: TestClient,
) -> None:
    """The golden ledger's rows are all dated before GOLDEN_NOW, so the date-aware read must
    reproduce the recorded balances digit for digit."""
    body = _cash(api_client)
    assert _pools(body) == [
        ("schwab", "TWD", "-32000"),
        ("schwab", "USD", "0"),
        ("tw_broker", "TWD", "-495000"),
    ]
    assert body["as_of"] == _TODAY


def test_cash_balances_bound_is_inclusive_and_keeps_the_pool_listed() -> None:
    rows = [_mv("2026-01-01", "DEPOSIT", "1000", 1), _mv("2099-01-01", "DEPOSIT", "777777", 2)]
    # No bound = the whole history, exactly as before.
    assert cash_balances(rows, [], [], [], {}) == {("acc", Currency.USD): Decimal("778777")}
    assert cash_balances(rows, [], [], [], {}, as_of=date(2026, 6, 11)) == {
        ("acc", Currency.USD): Decimal("1000")}
    # Inclusive: a row dated ON the bound counts.
    assert cash_balances(rows, [], [], [], {}, as_of=date(2026, 1, 1)) == {
        ("acc", Currency.USD): Decimal("1000")}
    # A pool whose only rows are in the future is still a pool — listed at 0, not dropped,
    # so the page shows the account and the enumeration downstream is unchanged.
    only_future = [_mv("2099-01-01", "DEPOSIT", "5", 3)]
    assert cash_balances(only_future, [], [], [], {}, as_of=date(2026, 6, 11)) == {
        ("acc", Currency.USD): Decimal("0")}


# --- the statement: every row shown, the balance as of today, future rows flagged --------


def test_statement_current_balance_is_as_of_today_and_future_rows_are_flagged(
    api_client: TestClient,
) -> None:
    assert api_client.post("/api/cash/movements", json=_FAR_FUTURE_DEPOSIT).status_code == 201
    body = api_client.get("/api/cash/statement",
                          params={"account": "tw_broker", "ccy": "TWD"}).json()
    assert body["as_of"] == _TODAY
    assert body["current_balance"] == "-495000"          # not −495000 + 777777
    rows = body["rows"]
    assert rows[0]["date"] == "2099-01-01" and rows[0]["future"] is True
    # Its running balance is still shown — it is a projection past today, and it is labelled.
    assert rows[0]["balance"] == "282777"
    assert all(r["future"] is False for r in rows[1:])
    # The account-level view's per-ccy balances are as of today too.
    acct = api_client.get("/api/cash/statement", params={"account": "tw_broker"}).json()
    assert {b["ccy"]: b["balance"] for b in acct["balances"]}["TWD"] == "-495000"
    assert acct["as_of"] == _TODAY


# --- the guard: the hole a future deposit used to open, and what it must still catch ----


def test_a_future_deposit_cannot_fund_a_withdrawal_today(api_client: TestClient) -> None:
    """tw_broker TWD sits at −495,000 with a pre-existing dip to −500,000 (2026-01-05), which
    silences the running-minimum branch. Before this fix the END balance (+505,000 with a
    2099 deposit) covered a 100 TWD withdrawal dated 2026-06-01 and it was WRITTEN."""
    assert api_client.post("/api/cash/movements", json={
        "account_id": "tw_broker", "date": "2099-01-01", "kind": "deposit",
        "ccy": "TWD", "amount": "1000000"}).status_code == 201
    r = api_client.post("/api/cash/movements", json={
        "account_id": "tw_broker", "date": "2026-06-01", "kind": "withdraw",
        "ccy": "TWD", "amount": "100"})
    assert r.status_code == 422, r.json()
    err = r.json()["error"]
    assert err["code"] == "withdraw_insufficient_balance"
    # The balance quoted is the pool's balance ON THE WITHDRAWAL'S DATE.
    assert "賬戶現金 -495000" in err["message"], err["message"]
    assert _pools(_cash(api_client)) == [
        ("schwab", "TWD", "-32000"), ("schwab", "USD", "0"), ("tw_broker", "TWD", "-495000")]


def test_a_future_dated_flow_still_enters_the_running_minimum(api_client: TestClient) -> None:
    """Counter-evidence: bounding the BALANCE must not bound the timeline. A 2099 withdrawal
    is accepted (its own date is funded), and shrinking the deposit it lives on is still
    refused as ``negative_cash`` — the future row is in the running minimum."""
    dep = api_client.post("/api/cash/movements", json={
        "account_id": "moomoo_my", "date": "2026-06-01", "kind": "deposit",
        "ccy": "USD", "amount": "1000"})
    assert dep.status_code == 201
    assert api_client.post("/api/cash/movements", json={
        "account_id": "moomoo_my", "date": "2099-01-01", "kind": "withdraw",
        "ccy": "USD", "amount": "1000"}).status_code == 201
    r = api_client.put(f"/api/cash/movements/{dep.json()['id']}", json={
        "account_id": "moomoo_my", "date": "2026-06-01", "kind": "deposit",
        "ccy": "USD", "amount": "1"})
    assert r.status_code == 422, r.json()
    assert r.json()["error"]["code"] == "negative_cash"
    assert "-999" in r.json()["error"]["message"]


def test_a_back_dated_withdrawal_before_its_funding_is_still_refused(
    api_client: TestClient,
) -> None:
    """The audit-C3 case the guard was hardened for, unchanged in verdict."""
    assert api_client.post("/api/cash/movements", json={
        "account_id": "moomoo_my", "date": "2026-06-05", "kind": "deposit",
        "ccy": "USD", "amount": "500"}).status_code == 201
    r = api_client.post("/api/cash/movements", json={
        "account_id": "moomoo_my", "date": "2026-06-01", "kind": "withdraw",
        "ccy": "USD", "amount": "400"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "withdraw_insufficient_balance"
