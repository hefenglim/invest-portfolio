"""M5-07: an overdraft refusal names THE DAY the pool would go negative, not 「某時點」.

``portfolio/cash.py::running_statement`` has always produced the date-ordered ``(line,
balance)`` pairs the running minimum is read off, so the day was one comparison away. The
three message sites — the withdraw guard (``validate._withdraw_issues``), the 換匯 guard
(``fx_import.fx_balance_issues``) and the ack-able ``negative_cash`` envelope
(``api/routers/cash.py``) — now all quote it. Several days sharing the same low name the
EARLIEST one.
"""

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import StoredCashMovement
from portfolio_dash.portfolio.cash import pool_lines, running_min
from portfolio_dash.shared.enums import Currency


def _mv(d: str, kind: str, amount: str) -> StoredCashMovement:
    return StoredCashMovement(id=0, account_id="acc", date=date.fromisoformat(d),
                              kind=kind, ccy=Currency.USD, amount=Decimal(amount), note=None)


def _lines(rows: list[StoredCashMovement]) -> list:  # type: ignore[type-arg]
    return pool_lines("acc", Currency.USD, rows, [], [], [], {})


# --- the pure function ------------------------------------------------------------------


def test_running_low_names_the_earliest_day_of_a_tied_minimum() -> None:
    from portfolio_dash.portfolio.cash import running_low
    rows = [_mv("2026-02-01", "WITHDRAW", "500"),   # -500 on 02-01
            _mv("2026-03-01", "DEPOSIT", "700"),    # +200
            _mv("2026-04-01", "WITHDRAW", "700")]   # -500 again on 04-01 (tied)
    assert running_low(_lines(rows)) == (Decimal("-500"), date(2026, 2, 1))
    assert running_min(_lines(rows)) == Decimal("-500")  # unchanged reading


def test_running_low_of_a_pool_that_never_dips_has_no_day() -> None:
    from portfolio_dash.portfolio.cash import running_low
    rows = [_mv("2026-01-01", "DEPOSIT", "700"), _mv("2026-02-01", "WITHDRAW", "500")]
    assert running_low(_lines(rows)) == (Decimal("0"), None)
    assert running_low([]) == (Decimal("0"), None)


# --- the three message sites --------------------------------------------------------------


def test_withdraw_guard_names_the_day_a_later_flow_is_stranded(api_client: TestClient) -> None:
    """moomoo USD: 1,000 in on 06-01, 1,000 out on 06-20 (exact). A 400 withdrawal on 06-10
    is covered on its own day but strands the 06-20 one — the day named is 06-20."""
    for body in ({"date": "2026-06-01", "kind": "deposit", "amount": "1000"},
                 {"date": "2026-06-20", "kind": "withdraw", "amount": "1000"}):
        r = api_client.post("/api/cash/movements", json={
            "account_id": "moomoo_my", "ccy": "USD", **body})
        assert r.status_code == 201, r.json()
    r = api_client.post("/api/cash/movements", json={
        "account_id": "moomoo_my", "date": "2026-06-10", "kind": "withdraw",
        "ccy": "USD", "amount": "400"})
    assert r.status_code == 422, r.json()
    msg = r.json()["error"]["message"]
    assert "於 2026-06-20 降至 -400" in msg, msg
    assert "某時點" not in msg


def test_fx_guard_names_the_day(api_client: TestClient) -> None:
    """schwab TWD: 50,000 in on 01-01, the golden 32,000 conversion out on 01-08. Converting
    40,000 on 01-05 is covered on 01-05 but leaves −22,000 on 01-08."""
    assert api_client.post("/api/cash/movements", json={
        "account_id": "schwab", "date": "2026-01-01", "kind": "deposit",
        "ccy": "TWD", "amount": "50000"}).status_code == 201
    r = api_client.post("/api/cash/fx", json={
        "account_id": "schwab", "date": "2026-01-05", "from_ccy": "TWD", "from_amt": "40000",
        "to_ccy": "USD", "to_amt": "1250"})
    assert r.status_code == 422, r.json()
    msg = r.json()["error"]["message"]
    assert r.json()["error"]["code"] == "fx_insufficient_balance"
    assert "於 2026-01-08 降至 -22000" in msg, msg
    assert "某時點" not in msg


def test_negative_cash_envelope_names_the_day(api_client: TestClient) -> None:
    """Deleting the golden 2330 dividend (5,000 on 03-01) leaves tw_broker TWD at −500,000
    from the 01-05 buy onward; the ack-able envelope names 01-05."""
    dep = api_client.post("/api/cash/movements", json={
        "account_id": "tw_broker", "date": "2026-06-01", "kind": "deposit",
        "ccy": "TWD", "amount": "600000"})
    assert dep.status_code == 201
    r = api_client.delete(f"/api/cash/movements/{dep.json()['id']}")
    assert r.status_code == 422, r.json()
    msg = r.json()["error"]["message"]
    assert r.json()["error"]["code"] == "negative_cash"
    assert "於 2026-01-05 降至 -500000" in msg, msg
    assert "某時點" not in msg
