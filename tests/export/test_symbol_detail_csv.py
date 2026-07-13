"""Unit tests for the symbol-detail dividend CSV builder (export.symbol_detail).

Reconciliation channel over the dividend ledger (``list_dividends``) — the SAME rows the
symbol drawer's 配息史 renders. Unknown symbol -> None (router answers 400).
"""

import sqlite3
from datetime import date
from decimal import Decimal

from portfolio_dash.data_ingestion.store import insert_dividend
from portfolio_dash.export.symbol_detail import build_symbol_detail_csv
from tests.conftest import _seed_golden, init_golden_base

_COLS = "date,type,gross,withholding,net,reinvest_shares,reinvest_price,ccy"


def _golden_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_golden_base(conn)
    _seed_golden(conn)
    conn.commit()
    return conn


def test_unknown_symbol_returns_none() -> None:
    conn = _golden_conn()
    try:
        assert build_symbol_detail_csv(conn, symbol="ZZZZ") is None
    finally:
        conn.close()


def test_known_symbol_dividend_row_at_source_precision() -> None:
    conn = _golden_conn()
    try:
        art = build_symbol_detail_csv(conn, symbol="2330")
    finally:
        conn.close()
    assert art is not None
    assert art.filename == "2330_dividends.csv"
    assert art.content[:3] == b"\xef\xbb\xbf"
    text = art.content[3:].decode("utf-8")
    assert text.split("\r\n", 1)[0] == _COLS
    # golden 2330 cash dividend: 2026-03-01 gross 5000 / withhold 0 / net 5000, TWD.
    assert "2026-03-01,cash,5000,0,5000,,,TWD" in text


def test_known_symbol_no_dividends_header_only() -> None:
    conn = _golden_conn()
    try:
        art = build_symbol_detail_csv(conn, symbol="AAPL")  # golden AAPL has no dividends
    finally:
        conn.close()
    assert art is not None
    text = art.content[3:].decode("utf-8")
    lines = [ln for ln in text.split("\r\n") if ln]
    assert lines == [_COLS]


def test_reinvest_columns_populated_for_drip() -> None:
    conn = _golden_conn()
    try:
        insert_dividend(conn, account_id="schwab", symbol="AAPL", div_date=date(2026, 4, 1),
                        div_type="DRIP", gross=Decimal("10"), withholding=Decimal("3"),
                        net=Decimal("7"), reinvest_shares=Decimal("0.05"),
                        reinvest_price=Decimal("140"))
        conn.commit()
        art = build_symbol_detail_csv(conn, symbol="AAPL")
    finally:
        conn.close()
    assert art is not None
    text = art.content[3:].decode("utf-8")
    assert "2026-04-01,drip,10,3,7,0.05,140,USD" in text
