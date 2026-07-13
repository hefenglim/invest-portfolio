"""Unit tests for the single-ledger CSV builder (export.ledgers.build_ledger_csv).

Reconciliation channel over the raw ledger tables: source-precision `SELECT *` dumps,
range-filtered on the tab's date column. Covers the kind→table map, BOM/CRLF framing,
column sets, date-range filters, and CSV escaping of commas/quotes in free-text cells.
"""

import sqlite3
from datetime import date
from decimal import Decimal

from portfolio_dash.data_ingestion.store import insert_transaction
from portfolio_dash.export.ledgers import LEDGER_KINDS, build_ledger_csv
from portfolio_dash.shared.models.enums import Side
from tests.conftest import _seed_golden, init_golden_base


def _golden_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_golden_base(conn)
    _seed_golden(conn)
    conn.commit()
    return conn


def _text(conn: sqlite3.Connection, *, kind: str, frm: str | None = None,
          to: str | None = None) -> str:
    art = build_ledger_csv(conn, kind=kind, frm=frm, to=to)
    assert art.content[:3] == b"\xef\xbb\xbf"  # UTF-8 BOM (Excel)
    return art.content[3:].decode("utf-8")


def test_kinds_map_covers_the_four_tabs() -> None:
    assert set(LEDGER_KINDS) == {"transactions", "dividends", "fx", "opening"}
    assert LEDGER_KINDS["transactions"] == ("transactions", "trade_date")
    assert LEDGER_KINDS["fx"] == ("fx_conversions", "date")
    assert LEDGER_KINDS["opening"] == ("opening_inventory", "build_date")


def test_transactions_header_is_raw_db_columns() -> None:
    conn = _golden_conn()
    try:
        text = _text(conn, kind="transactions")
    finally:
        conn.close()
    header = text.split("\r\n", 1)[0]
    assert header.startswith(
        "id,account_id,symbol,side,quantity,price,fees,tax,trade_date")
    # golden: two BUYs (2330 @500, AAPL @100) at source precision
    assert "2330,BUY,1000,500," in text
    assert "AAPL,BUY,10,100," in text


def test_filename_carries_kind_and_range_tag() -> None:
    conn = _golden_conn()
    try:
        art = build_ledger_csv(conn, kind="dividends", frm="2026-01-01", to="2026-12-31")
        assert art.filename == "ledger_dividends_2026-01-01_2026-12-31.csv"
        art2 = build_ledger_csv(conn, kind="fx", frm=None, to=None)
        assert art2.filename == "ledger_fx_all_all.csv"
    finally:
        conn.close()


def test_date_range_filters_on_the_tabs_date_column() -> None:
    conn = _golden_conn()
    try:
        # 2330 buy is 2026-01-05, AAPL buy is 2026-01-10; from=2026-01-08 drops 2330.
        text = _text(conn, kind="transactions", frm="2026-01-08")
        assert "AAPL" in text
        assert "2330" not in text
        # to=2026-01-06 keeps only 2330.
        text2 = _text(conn, kind="transactions", to="2026-01-06")
        assert "2330" in text2
        assert "AAPL" not in text2
    finally:
        conn.close()


def test_escaping_of_commas_and_quotes_in_free_text() -> None:
    conn = _golden_conn()
    try:
        insert_transaction(
            conn, account_id="tw_broker", symbol="2330", side=Side.SELL,
            quantity=Decimal("100"), price=Decimal("600"), fees=Decimal("0"),
            tax=Decimal("0"), trade_date=date(2026, 6, 10), note='a,b"c')
        conn.commit()
        text = _text(conn, kind="transactions")
        # The csv writer must quote the field and double the embedded quote.
        assert '"a,b""c"' in text
    finally:
        conn.close()


def test_empty_table_yields_header_only() -> None:
    conn = _golden_conn()
    try:
        text = _text(conn, kind="opening")  # golden seeds no opening inventory
    finally:
        conn.close()
    lines = [ln for ln in text.split("\r\n") if ln]
    assert len(lines) == 1  # header, no data rows
    assert lines[0].startswith("account_id,symbol,shares")
