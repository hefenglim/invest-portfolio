"""Regression tests: ledger correction audit trail (M9) + transaction price cap (L11)."""

import sqlite3
from datetime import date
from decimal import Decimal

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    delete_transaction,
    get_transaction,
    insert_transaction,
    list_ledger_audit,
    update_transaction,
    upsert_instrument,
)
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side


def _setup(conn: sqlite3.Connection) -> int:
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW,
                                       quote_ccy=Currency.TWD, sector="Tech", name="TSMC"))
    return insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                              quantity=Decimal("100"), price=Decimal("600"),
                              fees=Decimal("20"), tax=Decimal("0"), trade_date=date(2026, 1, 5))


def test_ledger_audit_captures_before_on_update(conn: sqlite3.Connection) -> None:
    txn_id = _setup(conn)
    update_transaction(conn, txn_id, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=Decimal("200"), price=Decimal("605"), fees=Decimal("20"),
                       tax=Decimal("0"), trade_date=date(2026, 1, 5))
    audit = list_ledger_audit(conn, table_name="transactions")
    assert len(audit) == 1
    assert audit[0]["action"] == "update"
    assert audit[0]["row_id"] == str(txn_id)
    assert '"quantity": "100"' in str(audit[0]["before_json"])  # ORIGINAL value captured


def test_ledger_audit_captures_before_on_delete(conn: sqlite3.Connection) -> None:
    txn_id = _setup(conn)
    delete_transaction(conn, txn_id)
    audit = list_ledger_audit(conn, table_name="transactions")
    assert len(audit) == 1 and audit[0]["action"] == "delete"
    assert '"price": "600"' in str(audit[0]["before_json"])


def test_transaction_price_capped_to_4dp(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW,
                                       quote_ccy=Currency.TWD, sector="Tech", name="TSMC"))
    txn_id = insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                                quantity=Decimal("1"), price=Decimal("305.364990234375"),
                                fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 5))
    row = get_transaction(conn, txn_id)
    assert row is not None and row.price == Decimal("305.3650")  # 4 dp, ROUND_HALF_UP


def test_clean_price_stored_byte_identical(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW,
                                       quote_ccy=Currency.TWD, sector="Tech", name="TSMC"))
    txn_id = insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                                quantity=Decimal("1"), price=Decimal("600.50"),
                                fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 5))
    row = get_transaction(conn, txn_id)
    assert row is not None and row.price == Decimal("600.50")  # within cap -> unchanged
