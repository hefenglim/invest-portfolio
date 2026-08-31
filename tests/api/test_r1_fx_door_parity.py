"""R1 / QA-10 + QA-11: the換匯 edit/delete doors, and the running minimum the POST door skipped.

* **QA-10** — ``PUT /api/ledgers/fx/{id}`` validated only ``>0`` and ``from_ccy != to_ccy``,
  and ``DELETE`` validated nothing at all, while ``POST /api/cash/fx`` enforces an
  allowed-currency check (audit C2) AND a balance guard (FU-D34). ``web/ledger.js`` renders
  free currency selects and free amount inputs on the 交易帳本 換匯 row, so the weaker pair
  is three clicks away.
* **QA-11** — ``POST /api/cash/fx`` compared the sell amount against the END balance only.
  A back-dated conversion that leaves the pool at −320,000 for two months was accepted,
  because the aggregate at the end is 0. The withdraw guard next door has used the
  date-ordered running minimum since audit C3.

The end-balance check and its message are DELIBERATELY kept — the comment above it chose
``cash_balances`` for display consistency with the 賬戶現金 line. The running-minimum test is
added ALONGSIDE it, not in place of it.
"""

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import insert_cash_movement, insert_fx_conversion
from portfolio_dash.shared.enums import Currency
from tests.conftest import DashboardClientFactory


def _balance(client: TestClient, account_id: str, ccy: str) -> str:
    for row in client.get("/api/cash").json()["balances"]:
        if row["account_id"] == account_id and row["ccy"] == ccy:
            amount: str = row["amount"]
            return amount
    return "0"


def _error_code(response: Any) -> str | None:
    body = response.json()
    if isinstance(body, dict) and "error" in body:
        code: str = body["error"]["code"]
        return code
    return None


def _seed_funded_conversion(conn: sqlite3.Connection) -> None:
    """schwab: TWD 320,000 deposited 2026-01-01, converted to USD 10,000 on 2026-01-05.

    End state: TWD 0, USD 10,000 — one stored conversion (id 1) for the edit/delete doors
    to act on.
    """
    seed_accounts(conn)
    insert_cash_movement(conn, account_id="schwab", move_date=date(2026, 1, 1),
                         kind="DEPOSIT", ccy=Currency.TWD, amount=Decimal("320000"))
    insert_fx_conversion(conn, account_id="schwab", date=date(2026, 1, 5),
                         from_ccy=Currency.TWD, from_amount=Decimal("320000"),
                         to_ccy=Currency.USD, to_amount=Decimal("10000"))


# --- QA-10a: PUT /api/ledgers/fx/{id} ---------------------------------------------------


def test_editing_a_conversion_beyond_the_pool_is_refused(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """Raising the sell leg to 10x the pool is the same overdraft ``POST /api/cash/fx``
    refuses — through a door that checked only ``> 0``."""
    client = dashboard_client_factory(_seed_funded_conversion)
    response = client.put("/api/ledgers/fx/1", json={
        "account_id": "schwab", "date": "2026-01-05",
        "from_ccy": "TWD", "from_amt": "3200000", "to_ccy": "USD", "to_amt": "100000"})
    assert response.status_code == 422, response.text
    assert _error_code(response) == "fx_insufficient_balance"
    assert _balance(client, "schwab", "TWD") == "0"


def test_the_post_door_refuses_the_same_overdraft(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The comparison that makes the assertion above a PARITY claim, not a preference."""
    client = dashboard_client_factory(_seed_funded_conversion)
    response = client.post("/api/cash/fx", json={
        "account_id": "schwab", "date": "2026-01-05",
        "from_ccy": "TWD", "from_amt": "3200000", "to_ccy": "USD", "to_amt": "100000"})
    assert response.status_code == 422, response.text
    assert _error_code(response) == "fx_insufficient_balance"


def test_editing_a_conversion_into_a_currency_the_account_may_not_hold_is_refused(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """audit C2 — schwab settles in USD and is funded in TWD; MYR is not its money.
    ``web/ledger.js`` offers all three currencies in a free select."""
    client = dashboard_client_factory(_seed_funded_conversion)
    response = client.put("/api/ledgers/fx/1", json={
        "account_id": "schwab", "date": "2026-01-05",
        "from_ccy": "TWD", "from_amt": "320000", "to_ccy": "MYR", "to_amt": "45000"})
    assert response.status_code == 400, response.text
    assert "MYR" in response.json()["error"]["message"]


def test_an_edit_within_the_rows_own_headroom_is_still_allowed(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """Self-exclusion: the edited row's OWN prior effect is stripped before the pool is
    read, so correcting 320,000 to 300,000 must not be blocked by the 320,000 it already
    consumed (the ``exclude_id`` pattern ``edit_movement`` proved)."""
    client = dashboard_client_factory(_seed_funded_conversion)
    response = client.put("/api/ledgers/fx/1", json={
        "account_id": "schwab", "date": "2026-01-05",
        "from_ccy": "TWD", "from_amt": "300000", "to_ccy": "USD", "to_amt": "9500"})
    assert response.status_code == 200, response.text
    assert _balance(client, "schwab", "TWD") == "20000"
    assert _balance(client, "schwab", "USD") == "9500"


def test_an_edit_that_only_changes_the_date_forward_is_still_allowed(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The identical amounts moved to a later date stay funded — the guard must not turn a
    routine correction into a refusal."""
    client = dashboard_client_factory(_seed_funded_conversion)
    response = client.put("/api/ledgers/fx/1", json={
        "account_id": "schwab", "date": "2026-02-05",
        "from_ccy": "TWD", "from_amt": "320000", "to_ccy": "USD", "to_amt": "10100"})
    assert response.status_code == 200, response.text
    assert _balance(client, "schwab", "USD") == "10100"


def test_back_dating_an_edit_before_its_funding_is_refused(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The running-minimum half, on the edit door: the deposit lands 2026-01-01, so a
    conversion moved to 2025-12-01 spends money the pool did not yet hold."""
    client = dashboard_client_factory(_seed_funded_conversion)
    response = client.put("/api/ledgers/fx/1", json={
        "account_id": "schwab", "date": "2025-12-01",
        "from_ccy": "TWD", "from_amt": "320000", "to_ccy": "USD", "to_amt": "10000"})
    assert response.status_code == 422, response.text
    assert _error_code(response) == "fx_insufficient_balance"


# --- QA-10b: DELETE /api/ledgers/fx/{id} ------------------------------------------------


def _seed_spent_conversion(conn: sqlite3.Connection) -> None:
    """The funded conversion above, with the USD it produced already spent.

    Deleting the conversion strands the withdrawal: the USD pool would sit at −10,000.
    """
    _seed_funded_conversion(conn)
    insert_cash_movement(conn, account_id="schwab", move_date=date(2026, 2, 1),
                         kind="WITHDRAW", ccy=Currency.USD, amount=Decimal("10000"))


def test_deleting_a_conversion_that_strands_a_later_withdrawal_needs_an_ack(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The ack-able ``negative_cash`` ``remove_movement`` has applied since audit C3 —
    the neighbouring control on the same ledger page, one row type apart."""
    client = dashboard_client_factory(_seed_spent_conversion)
    response = client.delete("/api/ledgers/fx/1")
    assert response.status_code == 422, response.text
    assert _error_code(response) == "negative_cash"
    assert _balance(client, "schwab", "USD") == "0"  # nothing deleted


def test_the_acked_delete_goes_through(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """A warning, not a wall: the owner may still correct the ledger (this is a correction
    door, and a negative pool is a data problem to be fixed, not a rule to be enforced)."""
    client = dashboard_client_factory(_seed_spent_conversion)
    response = client.delete("/api/ledgers/fx/1?ack_negative=true")
    assert response.status_code == 200, response.text
    assert _balance(client, "schwab", "USD") == "-10000"


def test_deleting_a_conversion_nothing_depends_on_is_untouched(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """No pool goes negative, so no ack is asked for — the common case must not regress."""
    client = dashboard_client_factory(_seed_funded_conversion)
    response = client.delete("/api/ledgers/fx/1")
    assert response.status_code == 200, response.text
    assert _balance(client, "schwab", "TWD") == "320000"
    assert _balance(client, "schwab", "USD") == "0"


# --- QA-11: POST /api/cash/fx must test the running minimum too -------------------------


def _seed_late_deposit(conn: sqlite3.Connection) -> None:
    """schwab funded on 2026-03-01 only. End balance 320,000; nothing before March."""
    seed_accounts(conn)
    insert_cash_movement(conn, account_id="schwab", move_date=date(2026, 3, 1),
                         kind="DEPOSIT", ccy=Currency.TWD, amount=Decimal("320000"))


def test_a_back_dated_conversion_that_dips_the_pool_is_refused(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """QA-11's reproduction: the END balance is 0 either way, so the aggregate check passes
    while the pool sits at −320,000 from January to March."""
    client = dashboard_client_factory(_seed_late_deposit)
    response = client.post("/api/cash/fx", json={
        "account_id": "schwab", "date": "2026-01-15",
        "from_ccy": "TWD", "from_amt": "320000", "to_ccy": "USD", "to_amt": "10000"})
    assert response.status_code == 422, response.text
    assert _error_code(response) == "fx_insufficient_balance"
    assert _balance(client, "schwab", "TWD") == "320000"


def test_the_end_balance_message_is_preserved_for_the_plain_overdraft(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The end-balance check was chosen for display consistency with the 賬戶現金 line and
    is kept: a conversion larger than the balance still answers with THAT message."""
    client = dashboard_client_factory(_seed_late_deposit)
    response = client.post("/api/cash/fx", json={
        "account_id": "schwab", "date": "2026-03-02",
        "from_ccy": "TWD", "from_amt": "400000", "to_ccy": "USD", "to_amt": "12000"})
    assert response.status_code == 422, response.text
    assert _error_code(response) == "fx_insufficient_balance"
    assert "可用餘額" in response.json()["error"]["message"]


def test_a_conversion_on_or_after_its_funding_still_passes(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """Same-day funding counts (credits sort before debits), so the exact-balance
    conversion the FU-D34 comment protects is unaffected."""
    client = dashboard_client_factory(_seed_late_deposit)
    response = client.post("/api/cash/fx", json={
        "account_id": "schwab", "date": "2026-03-01",
        "from_ccy": "TWD", "from_amt": "320000", "to_ccy": "USD", "to_amt": "10000"})
    assert response.status_code == 201, response.text
    assert _balance(client, "schwab", "USD") == "10000"


def test_a_pre_existing_dip_does_not_block_a_covered_conversion(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """Scoped like the withdraw guard: a dip the conversion does not DEEPEN never blocks it
    (``after.low < min(before.low, 0)``), so a ledger already in the red stays correctable."""
    def seed(conn: sqlite3.Connection) -> None:
        seed_accounts(conn)
        # A pre-existing January hole in the TWD pool, filled in February.
        insert_cash_movement(conn, account_id="schwab", move_date=date(2026, 1, 1),
                             kind="WITHDRAW", ccy=Currency.TWD, amount=Decimal("50000"))
        insert_cash_movement(conn, account_id="schwab", move_date=date(2026, 2, 1),
                             kind="DEPOSIT", ccy=Currency.TWD, amount=Decimal("400000"))

    client = dashboard_client_factory(seed)
    response = client.post("/api/cash/fx", json={
        "account_id": "schwab", "date": "2026-03-01",
        "from_ccy": "TWD", "from_amt": "320000", "to_ccy": "USD", "to_amt": "10000"})
    assert response.status_code == 201, response.text
