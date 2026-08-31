"""R1 / QA-01 + QA-09: a bulk import door must never ship a weaker guard than its form.

``architecture.md``'s C3 injection seam exists so "the CSV import door and the manual form
run the SAME withdraw guard", and it explicitly rejects "leaving the guard in ``api/``
(legal, but then the bulk door ships a weaker guard than the single-row form)".

Two doors broke that:

* **QA-01** — ``build_cash_movement_preview`` handed the withdraw guard every row that
  PARSED, not every row that would be COMMITTED. A deposit the caller deselected (or one
  the validator itself rejected) therefore funded a withdrawal that was then written alone,
  and the pool went negative through a door whose whole purpose is to refuse exactly that.
* **QA-09** — ``build_fx_preview`` had no pool probe and no currency check at all, while
  ``POST /api/cash/fx`` enforces both.

Both are asserted here through the REAL ``/api/import/*`` seam, against the manual door's
verdict on the identical movement, because parity between the two doors is the property —
not the message text of either one alone.
"""

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import insert_cash_movement
from portfolio_dash.shared.enums import Currency
from tests.conftest import DashboardClientFactory

_CASH_HEADER = "account,date,kind,ccy,amount,acq_home_amount,note\n"
_FX_HEADER = "account,date,from_ccy,from_amount,to_ccy,to_amount\n"


def _balance(client: TestClient, account_id: str, ccy: str) -> str:
    for row in client.get("/api/cash").json()["balances"]:
        if row["account_id"] == account_id and row["ccy"] == ccy:
            amount: str = row["amount"]
            return amount
    return "0"


def _commit(client: TestClient, kind: str, csv_text: str, **extra: Any) -> Any:
    body: dict[str, Any] = {"kind": kind, "csv_text": csv_text, "ack_warnings": True}
    body.update(extra)
    return client.post("/api/import/commit", json=body)


def _preview_rows(client: TestClient, kind: str, csv_text: str) -> list[dict[str, Any]]:
    response = client.post("/api/import/preview", json={"kind": kind, "csv_text": csv_text})
    assert response.status_code == 200, response.text
    rows: list[dict[str, Any]] = response.json()["rows"]
    return rows


# --- QA-01: the batch handed to the guard is the set of rows that will be WRITTEN --------


_FUNDED_WITHDRAW = (
    _CASH_HEADER
    + "tw_broker,2026-01-01,deposit,TWD,100000,,\n"
    + "tw_broker,2026-02-01,withdraw,TWD,60000,,\n"
)


def test_a_deselected_deposit_cannot_fund_the_withdrawal_that_is_written_alone(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The QA-01 reproduction: commit row 1 only, and the deposit that funds it is not
    written. The withdrawal must be refused, not booked into an empty pool."""
    client = dashboard_client_factory(seed_accounts)
    response = _commit(client, "cash", _FUNDED_WITHDRAW, select=[1])
    assert response.status_code == 200, response.text
    out = response.json()
    assert out["written"] == 0, f"wrote an unfunded withdrawal: {out}"
    assert _balance(client, "tw_broker", "TWD") == "0", "the pool was driven negative"


def test_the_refused_row_says_why_rather_than_reading_as_a_deselection(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """``skipped`` means "the caller did not tick it"; a refusal must land in ``rejected``
    with the guard's own zh reason (``ImportSummary``'s four buckets)."""
    client = dashboard_client_factory(seed_accounts)
    out = _commit(client, "cash", _FUNDED_WITHDRAW, select=[1]).json()
    assert out.get("rejected") == 1, out
    reject = out["rejected_rows"][0]
    assert reject["kind"] == "withdraw_insufficient_balance"
    assert "出金" in reject["message"]


def test_the_manual_door_refuses_the_identical_movement(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """Parity, stated as the comparison it is: the same withdrawal against the same (empty)
    ledger is a 422 through the form. The bulk door must not answer 200/written."""
    client = dashboard_client_factory(seed_accounts)
    response = client.post("/api/cash/movements", json={
        "account_id": "tw_broker", "date": "2026-02-01", "kind": "withdraw",
        "ccy": "TWD", "amount": "60000"})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "withdraw_insufficient_balance"


def test_selecting_both_rows_still_imports_the_self_funding_file(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """E1a must survive the fix: a first import into a FRESH ledger contains its own
    funding, so the withdrawal is validated against its siblings THAT ARE BEING WRITTEN."""
    client = dashboard_client_factory(seed_accounts)
    response = _commit(client, "cash", _FUNDED_WITHDRAW, select=[0, 1])
    assert response.status_code == 200, response.text
    assert response.json()["written"] == 2, response.text
    assert _balance(client, "tw_broker", "TWD") == "40000"


def test_omitting_the_selection_still_imports_the_self_funding_file(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """``select`` omitted means "all of them" for every other caller (the AI door, undo);
    that path must stay byte-compatible."""
    client = dashboard_client_factory(seed_accounts)
    response = _commit(client, "cash", _FUNDED_WITHDRAW)
    assert response.status_code == 200, response.text
    assert response.json()["written"] == 2, response.text
    assert _balance(client, "tw_broker", "TWD") == "40000"


def test_file_order_is_still_irrelevant(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The timeline is date-ordered, so the withdrawal may precede its funding in the file."""
    client = dashboard_client_factory(seed_accounts)
    reversed_file = (
        _CASH_HEADER
        + "tw_broker,2026-02-01,withdraw,TWD,60000,,\n"
        + "tw_broker,2026-01-01,deposit,TWD,100000,,\n"
    )
    response = _commit(client, "cash", reversed_file)
    assert response.status_code == 200, response.text
    assert response.json()["written"] == 2, response.text


def test_a_hard_invalid_deposit_cannot_fund_a_withdrawal_it_will_never_join(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The half of QA-01 that needs no ``select`` at all.

    The deposit carries a negative acquisition cost, so ``validate_cash_movement`` refuses
    it (``acq_cost_not_positive``) and it is never written — but it parsed, so the old
    ``batch`` still counted its 10,000 USD toward the withdrawal in the same file.
    """
    client = dashboard_client_factory(seed_accounts)
    csv_text = (
        _CASH_HEADER
        + "schwab,2026-01-01,deposit,USD,10000,-1,\n"
        + "schwab,2026-02-01,withdraw,USD,6000,,\n"
    )
    rows = _preview_rows(client, "cash", csv_text)
    assert rows[0]["status"] == "error", rows[0]
    assert rows[1]["status"] == "error", (
        "a withdrawal was funded by a deposit the same preview rejects: " + repr(rows[1]))
    response = _commit(client, "cash", csv_text)
    assert response.status_code == 200, response.text
    assert response.json()["written"] == 0
    assert _balance(client, "schwab", "USD") == "0"


# --- QA-09: the FX CSV door runs the guards POST /api/cash/fx runs -----------------------


def _seed_schwab_twd(conn: sqlite3.Connection) -> None:
    """A schwab TWD pool with 100,000 in it — enough to fund a small conversion, far short
    of the 320,000 one the tests below attempt."""
    seed_accounts(conn)
    insert_cash_movement(conn, account_id="schwab", move_date=date(2026, 1, 1),
                         kind="DEPOSIT", ccy=Currency.TWD, amount=Decimal("100000"))


_OVERDRAFT_FX = _FX_HEADER + "schwab,2026-02-01,TWD,320000,USD,10000\n"


def test_the_fx_csv_door_refuses_a_conversion_the_pool_cannot_cover(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed_schwab_twd)
    rows = _preview_rows(client, "fx", _OVERDRAFT_FX)
    assert rows[0]["status"] == "error", rows[0]
    response = _commit(client, "fx", _OVERDRAFT_FX)
    assert response.status_code == 200, response.text
    assert response.json()["written"] == 0, "the bulk door financed an overdraft"
    assert _balance(client, "schwab", "TWD") == "100000"


def test_the_manual_fx_door_refuses_the_identical_conversion(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed_schwab_twd)
    response = client.post("/api/cash/fx", json={
        "account_id": "schwab", "date": "2026-02-01",
        "from_ccy": "TWD", "from_amt": "320000", "to_ccy": "USD", "to_amt": "10000"})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "fx_insufficient_balance"


def test_the_fx_csv_door_refuses_a_currency_the_account_may_not_hold(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """audit C2 — tw_broker settles and is funded in TWD, so an MYR leg is not its money."""
    client = dashboard_client_factory(_seed_schwab_twd)
    csv_text = _FX_HEADER + "tw_broker,2026-02-01,TWD,1000,MYR,140\n"
    rows = _preview_rows(client, "fx", csv_text)
    assert rows[0]["status"] == "error", rows[0]
    assert "MYR" in rows[0]["reason"]
    assert _commit(client, "fx", csv_text).json()["written"] == 0


def test_a_deselected_fx_row_cannot_fund_the_conversion_written_alone(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """QA-01's invariant applied to the door being fixed one over: the sibling conversion
    that credits the USD pool is deselected, so it cannot pay for the one that spends it."""
    client = dashboard_client_factory(_seed_schwab_twd)
    csv_text = (
        _FX_HEADER
        + "schwab,2026-01-05,TWD,32000,USD,1000\n"
        + "schwab,2026-01-06,USD,1000,TWD,32100\n"
    )
    response = _commit(client, "fx", csv_text, select=[1])
    assert response.status_code == 200, response.text
    assert response.json()["written"] == 0, response.text
    assert _balance(client, "schwab", "USD") == "0"


def test_a_self_funding_fx_file_still_imports(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """E1a for the FX door: the conversion that funds the USD pool is in the same file."""
    client = dashboard_client_factory(_seed_schwab_twd)
    csv_text = (
        _FX_HEADER
        + "schwab,2026-01-05,TWD,32000,USD,1000\n"
        + "schwab,2026-01-06,USD,1000,TWD,32100\n"
    )
    response = _commit(client, "fx", csv_text)
    assert response.status_code == 200, response.text
    assert response.json()["written"] == 2, response.text
    assert _balance(client, "schwab", "USD") == "0"
    assert _balance(client, "schwab", "TWD") == "100100"


def test_a_clean_fx_file_is_unaffected(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The guard must not turn an ordinary funded conversion into a rejection."""
    client = dashboard_client_factory(_seed_schwab_twd)
    csv_text = _FX_HEADER + "schwab,2026-01-05,TWD,32000,USD,1000\n"
    assert _preview_rows(client, "fx", csv_text)[0]["status"] == "ok"
    assert _commit(client, "fx", csv_text).json()["written"] == 1
    assert _balance(client, "schwab", "USD") == "1000"
