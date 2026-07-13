"""Unit tests for the realized-P&L CSV builder (export.realized.build_realized_csv).

The builder computes NO numbers: it serializes ``build_dashboard(...).realized.rows``
(the ledger-replay core). Tests assert the CSV faithfully mirrors the core output at
source precision (reconciliation grade) — never re-deriving money.
"""

import sqlite3
from datetime import date
from decimal import Decimal

from portfolio_dash.data_ingestion.store import insert_transaction
from portfolio_dash.export.realized import build_realized_csv
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.wire import decimal_str
from tests.conftest import GOLDEN_NOW, _seed_golden, init_golden_base

_COLS = ("account_id,symbol,quote_ccy,sell_date,shares_sold,proceeds_net,"
         "original_cost_removed,adjusted_cost_removed,realized")


def _conn_with_sell() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_golden_base(conn)
    _seed_golden(conn)
    # Sell 100 of the 2330 lot (buy 1000@500, cash div 5000 already reduces adjusted).
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.SELL,
                       quantity=Decimal("100"), price=Decimal("600"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 6, 10))
    conn.commit()
    return conn


def test_header_and_bom() -> None:
    conn = _conn_with_sell()
    try:
        art = build_realized_csv(conn, now=GOLDEN_NOW, reporting=Currency.TWD)
    finally:
        conn.close()
    assert art.content[:3] == b"\xef\xbb\xbf"
    text = art.content[3:].decode("utf-8")
    assert text.split("\r\n", 1)[0] == _COLS
    assert art.filename == "realized_pnl_2026-06-11.csv"


def test_rows_mirror_the_core_at_source_precision() -> None:
    conn = _conn_with_sell()
    try:
        data = build_dashboard(conn, now=GOLDEN_NOW, reporting=Currency.TWD)
        art = build_realized_csv(conn, now=GOLDEN_NOW, reporting=Currency.TWD)
    finally:
        conn.close()
    text = art.content[3:].decode("utf-8")
    assert data.realized.rows, "fixture must produce at least one realized row"
    for r in data.realized.rows:
        # Every computed Decimal appears EXACTLY as the core emits it (decimal_str).
        assert decimal_str(r.realized) in text
        assert decimal_str(r.proceeds_net) in text
        assert decimal_str(r.adjusted_cost_removed) in text
        assert f"{r.account_id},{r.symbol},{r.quote_ccy.value}," in text


def test_empty_when_no_sells_header_only() -> None:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_golden_base(conn)
    _seed_golden(conn)  # golden has no sells -> no realized rows
    conn.commit()
    try:
        art = build_realized_csv(conn, now=GOLDEN_NOW, reporting=Currency.TWD)
    finally:
        conn.close()
    text = art.content[3:].decode("utf-8")
    body = [ln for ln in text.split("\r\n") if ln and not ln.startswith("#")]
    assert body == [_COLS]  # header only, no data rows
