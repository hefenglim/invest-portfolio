import sqlite3
from datetime import date
from decimal import Decimal

from portfolio_dash.data_ingestion.store import insert_transaction, list_transactions
from portfolio_dash.shared.models.enums import Side


def test_insert_and_list_roundtrip(conn: sqlite3.Connection) -> None:
    tid = insert_transaction(
        conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
        quantity=Decimal("1000"), price=Decimal("600"), fees=Decimal("855"),
        tax=Decimal("0"), trade_date=date(2026, 6, 1),
        fee_rule_snapshot={"brokerage": "0.001425"}, note="first buy")
    assert isinstance(tid, int) and tid > 0
    rows = list_transactions(conn, account_id="tw_broker")
    assert len(rows) == 1
    r = rows[0]
    assert r.symbol == "2330" and r.side is Side.BUY and r.quantity == Decimal("1000")
    assert r.price == Decimal("600") and r.fees == Decimal("855")
    assert r.fee_rule_snapshot == {"brokerage": "0.001425"} and r.note == "first buy"


def test_daytrade_flag_persists_roundtrip(conn: sqlite3.Connection) -> None:
    # MED-1: the daytrade flag is stored on the row and read back (default False otherwise).
    tid = insert_transaction(
        conn, account_id="tw_broker", symbol="2330", side=Side.SELL,
        quantity=Decimal("1"), price=Decimal("600"), fees=Decimal("20"),
        tax=Decimal("0"), trade_date=date(2026, 6, 1), daytrade=True)
    plain = insert_transaction(
        conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
        quantity=Decimal("1"), price=Decimal("600"), fees=Decimal("20"),
        tax=Decimal("0"), trade_date=date(2026, 6, 2))
    by_id = {r.id: r for r in list_transactions(conn, account_id="tw_broker")}
    assert by_id[tid].daytrade is True
    assert by_id[plain].daytrade is False  # default preserved


def test_list_ascending_by_date_and_filters(conn: sqlite3.Connection) -> None:
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=Decimal("1"), price=Decimal("1"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 6, 3))
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=Decimal("1"), price=Decimal("1"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 6, 1))
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("1"), price=Decimal("1"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 6, 2))
    tw = list_transactions(conn, account_id="tw_broker")
    assert [r.trade_date for r in tw] == [date(2026, 6, 1), date(2026, 6, 3)]  # ascending
    aapl = list_transactions(conn, symbol="AAPL")
    assert len(aapl) == 1 and aapl[0].account_id == "schwab"
