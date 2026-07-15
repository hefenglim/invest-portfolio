"""Regression tests: ledger correction audit trail (M9) + transaction price cap (L11).

The dividends / fx_conversions / opening_inventory audit-capture assertions (LOW-4 a & d)
mirror the transactions probes below so every correction seam is proven to snapshot its
pre-mutation row into ``ledger_audit`` before writing.
"""

import sqlite3
from datetime import date
from decimal import Decimal

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.schema import create_tables
from portfolio_dash.data_ingestion.store import (
    delete_dividend,
    delete_fx_conversion,
    delete_opening,
    delete_transaction,
    get_transaction,
    insert_dividend,
    insert_fx_conversion,
    insert_transaction,
    list_ledger_audit,
    list_transactions,
    update_dividend,
    update_fx_conversion,
    update_transaction,
    upsert_instrument,
    upsert_opening,
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
                       tax=Decimal("0"), trade_date=date(2026, 1, 5), daytrade=False)
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


# --- LOW-4a: dividends audit-capture (mirror the transactions probes) ----------


def _setup_div(conn: sqlite3.Connection) -> int:
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW,
                                       quote_ccy=Currency.TWD, sector="Tech", name="TSMC"))
    return insert_dividend(conn, account_id="tw_broker", symbol="2330",
                           div_date=date(2026, 3, 1), div_type="CASH",
                           gross=Decimal("5000"), withholding=Decimal("0"),
                           net=Decimal("5000"))


def test_ledger_audit_captures_before_on_dividend_update(conn: sqlite3.Connection) -> None:
    div_id = _setup_div(conn)
    update_dividend(conn, div_id, account_id="tw_broker", symbol="2330",
                    div_date=date(2026, 3, 1), div_type="CASH", gross=Decimal("6000"),
                    withholding=Decimal("0"), net=Decimal("6000"))
    audit = list_ledger_audit(conn, table_name="dividends")
    assert len(audit) == 1 and audit[0]["action"] == "update"
    assert audit[0]["row_id"] == str(div_id)
    assert '"net": "5000"' in str(audit[0]["before_json"])  # ORIGINAL captured


def test_ledger_audit_captures_before_on_dividend_delete(conn: sqlite3.Connection) -> None:
    div_id = _setup_div(conn)
    delete_dividend(conn, div_id)
    audit = list_ledger_audit(conn, table_name="dividends")
    assert len(audit) == 1 and audit[0]["action"] == "delete"
    assert '"gross": "5000"' in str(audit[0]["before_json"])


# --- LOW-4d: fx_conversions + opening_inventory audit-capture ------------------


def _setup_fx(conn: sqlite3.Connection) -> int:
    seed_accounts(conn)
    return insert_fx_conversion(conn, account_id="schwab", date=date(2026, 1, 8),
                                from_ccy=Currency.TWD, from_amount=Decimal("32000"),
                                to_ccy=Currency.USD, to_amount=Decimal("1000"))


def test_ledger_audit_captures_before_on_fx_update(conn: sqlite3.Connection) -> None:
    fx_id = _setup_fx(conn)
    update_fx_conversion(conn, fx_id, account_id="schwab", date=date(2026, 1, 8),
                         from_ccy=Currency.TWD, from_amount=Decimal("33000"),
                         to_ccy=Currency.USD, to_amount=Decimal("1000"))
    audit = list_ledger_audit(conn, table_name="fx_conversions")
    assert len(audit) == 1 and audit[0]["action"] == "update"
    assert audit[0]["row_id"] == str(fx_id)
    assert '"from_amount": "32000"' in str(audit[0]["before_json"])


def test_ledger_audit_captures_before_on_fx_delete(conn: sqlite3.Connection) -> None:
    fx_id = _setup_fx(conn)
    delete_fx_conversion(conn, fx_id)
    audit = list_ledger_audit(conn, table_name="fx_conversions")
    assert len(audit) == 1 and audit[0]["action"] == "delete"
    assert '"to_amount": "1000"' in str(audit[0]["before_json"])


def _setup_opening(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW,
                                       quote_ccy=Currency.TWD, sector="Tech", name="TSMC"))
    upsert_opening(conn, account_id="tw_broker", symbol="2330", shares=Decimal("100"),
                   original_avg_cost=Decimal("500"), original_cost_total=Decimal("50000"),
                   build_date=date(2026, 1, 1))


def test_ledger_audit_captures_before_on_opening_update(conn: sqlite3.Connection) -> None:
    _setup_opening(conn)
    # a second upsert on the same (account, symbol) key hits the update path -> audits prior.
    upsert_opening(conn, account_id="tw_broker", symbol="2330", shares=Decimal("200"),
                   original_avg_cost=Decimal("505"), original_cost_total=Decimal("101000"),
                   build_date=date(2026, 1, 1))
    audit = list_ledger_audit(conn, table_name="opening_inventory")
    assert len(audit) == 1 and audit[0]["action"] == "update"
    assert audit[0]["row_id"] == "tw_broker/2330"
    assert '"shares": "100"' in str(audit[0]["before_json"])


def test_ledger_audit_captures_before_on_opening_delete(conn: sqlite3.Connection) -> None:
    _setup_opening(conn)
    delete_opening(conn, "tw_broker", "2330")
    audit = list_ledger_audit(conn, table_name="opening_inventory")
    assert len(audit) == 1 and audit[0]["action"] == "delete"
    assert '"original_cost_total": "50000"' in str(audit[0]["before_json"])


# --- MED-1: legacy DB (transactions table without daytrade) migrates -----------


def test_legacy_transactions_table_migrates_daytrade(conn: sqlite3.Connection) -> None:
    """A pre-daytrade DB gains the column via create_tables and reads back False."""
    seed_accounts(conn)
    conn.execute("DROP TABLE transactions")
    conn.execute(
        "CREATE TABLE transactions ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL, symbol TEXT NOT NULL,"
        "side TEXT NOT NULL, quantity TEXT NOT NULL, price TEXT NOT NULL, fees TEXT NOT NULL,"
        "tax TEXT NOT NULL, trade_date TEXT NOT NULL, fee_rule_snapshot TEXT, note TEXT)"
    )
    conn.execute(
        "INSERT INTO transactions (account_id, symbol, side, quantity, price, fees, tax, "
        "trade_date) VALUES ('tw_broker','2330','BUY','1','1','0','0','2026-01-05')"
    )
    conn.commit()
    create_tables(conn)  # ensure_schema path: adds the missing daytrade column
    rows = list_transactions(conn)
    assert len(rows) == 1 and rows[0].daytrade is False
