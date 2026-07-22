"""Batch B T3: rebate relevance keys off the BOUND rule sets, not the account scalar.

T3 swapped ``api/rebates.py::_rebate_accounts`` and the ``routers/rebates.py`` confirm gate
from the ``accounts.fee_rule_set`` scalar to :func:`rule_sets_for`: an account is
rebate-relevant iff ANY of its bound rule sets has ``rebate_rate > 0``. These tests use a
synthetic DUAL-RULE account whose scalar rule set does NOT rebate but one of whose bound
rule sets (``tw``) does — so only the binding-aware path recognises it.
"""

import sqlite3
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from portfolio_dash.api.rebates import _rebate_accounts
from portfolio_dash.data_ingestion.store import insert_transaction
from portfolio_dash.shared.models.enums import Side


def _add_account(
    conn: sqlite3.Connection, account_id: str, bindings: list[tuple[str, str]], scalar: str
) -> None:
    """Insert a TWD account whose SCALAR fee_rule_set is *scalar* and bind each (market,
    rule_set) in *bindings*. dividend_model is irrelevant to rebate relevance here."""
    conn.execute(
        "INSERT INTO accounts (account_id, name, broker, settlement_ccy, funding_ccy, "
        "fee_rule_set, dividend_model) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (account_id, account_id, account_id, "TWD", "TWD", scalar, "cash"),
    )
    conn.executemany(
        "INSERT INTO account_market_rules (account_id, market, fee_rule_set, dividend_model) "
        "VALUES (?, ?, ?, ?)",
        [(account_id, mkt, rule, "cash") for mkt, rule in bindings],
    )
    conn.commit()


def test_rebate_accounts_relevant_when_any_bound_set_rebates(
    golden_db: sqlite3.Connection,
) -> None:
    # scalar 'schwab' (rebate 0), but bound TW->tw (rebate 0.77) and US->schwab (0).
    _add_account(golden_db, "hybrid", [("TW", "tw"), ("US", "schwab")], scalar="schwab")
    accts = _rebate_accounts(golden_db)
    assert "hybrid" in accts
    # The rebating bound set (tw @ 0.77) supplies the rate — NOT the non-rebate scalar.
    assert accts["hybrid"][1] == Decimal("0.77")


def test_rebate_accounts_excludes_all_nonrebate_bindings(
    golden_db: sqlite3.Connection,
) -> None:
    # Both bound sets rebate 0 -> the account is NOT rebate-relevant (behaviour-identical to
    # the merged Moomoo account today: neither moomoo set rebates).
    _add_account(golden_db, "dual_us_my", [("US", "moomoo_us"), ("MY", "moomoo_my")],
                 scalar="moomoo_us")
    accts = _rebate_accounts(golden_db)
    assert "dual_us_my" not in accts
    # The seeded single-market accounts are unchanged: tw_broker rebates, moomoo sets do not.
    assert accts["tw_broker"][1] == Decimal("0.77")
    assert "moomoo_my_us" not in accts and "moomoo_my_my" not in accts


def test_confirm_gate_accepts_dual_rule_rebate_account(
    golden_db: sqlite3.Connection, api_client: TestClient
) -> None:
    """The confirm rebate-relevance gate (routers/rebates.py) must accept the dual-rule
    account: its bound 'tw' set rebates, though the scalar does not."""
    _add_account(golden_db, "hybrid", [("TW", "tw"), ("US", "schwab")], scalar="schwab")
    # A fee-bearing trade in a PAST month (GOLDEN_NOW is 2026-06-11) -> the month is pending.
    insert_transaction(golden_db, account_id="hybrid", symbol="2330", side=Side.BUY,
                       quantity=Decimal("1000"), price=Decimal("500"), fees=Decimal("142"),
                       tax=Decimal("0"), trade_date=date(2026, 5, 5))
    golden_db.commit()
    resp = api_client.post("/api/rebates/confirm",
                           json={"account_id": "hybrid", "month": "2026-05", "amount": "100"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["account_id"] == "hybrid" and body["month"] == "2026-05"
    assert body["ccy"] == "TWD"


def test_confirm_gate_rejects_nonrebate_account(
    golden_db: sqlite3.Connection, api_client: TestClient
) -> None:
    """A dual-rule account with NO rebating bound set is rejected by the confirm gate."""
    _add_account(golden_db, "dual_us_my", [("US", "moomoo_us"), ("MY", "moomoo_my")],
                 scalar="moomoo_us")
    resp = api_client.post("/api/rebates/confirm",
                           json={"account_id": "dual_us_my", "month": "2026-05",
                                 "amount": "100"})
    assert resp.status_code == 400
    assert resp.json()["error"]["field"] == "account_id"
