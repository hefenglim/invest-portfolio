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

_COLS = "date,type,gross,withholding,net,reinvest_shares,reinvest_price,ccy,counts_from"
_DAY = date(2026, 6, 11)  # the golden valuation day


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
        assert build_symbol_detail_csv(conn, symbol="ZZZZ", today=_DAY) is None
    finally:
        conn.close()


def test_known_symbol_dividend_row_at_source_precision() -> None:
    conn = _golden_conn()
    try:
        art = build_symbol_detail_csv(conn, symbol="2330", today=_DAY)
    finally:
        conn.close()
    assert art is not None
    assert art.filename == "2330_dividends.csv"
    assert art.content[:3] == b"\xef\xbb\xbf"
    text = art.content[3:].decode("utf-8")
    assert text.split("\r\n", 1)[0] == _COLS
    # golden 2330 cash dividend: 2026-03-01 gross 5000 / withhold 0 / net 5000, TWD.
    assert "2026-03-01,cash,5000,0,5000,,,TWD," in text


def test_known_symbol_no_dividends_header_only() -> None:
    conn = _golden_conn()
    try:
        art = build_symbol_detail_csv(conn, symbol="AAPL", today=_DAY)  # golden AAPL: none
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
        art = build_symbol_detail_csv(conn, symbol="AAPL", today=_DAY)
    finally:
        conn.close()
    assert art is not None
    text = art.content[3:].decode("utf-8")
    assert "2026-04-01,drip,10,3,7,0.05,140,USD," in text


def test_a_dividend_that_does_not_count_yet_says_from_when() -> None:
    """DEF-056 R5: the drawer's 配息史 badges a dividend dated after the valuation day, so the
    CSV that reconciles that table carries the same flag — the day it starts to count, from
    its EFFECTIVE date (a 配股 counts from its ex-date), empty once it does."""
    conn = _golden_conn()
    try:
        insert_dividend(conn, account_id="tw_broker", symbol="2330", div_date=date(2026, 7, 1),
                        div_type="CASH", gross=Decimal("18200"), withholding=Decimal("0"),
                        net=Decimal("18200"), ex_date=date(2026, 6, 5))
        insert_dividend(conn, account_id="tw_broker", symbol="2330", div_date=date(2026, 7, 1),
                        div_type="STOCK", gross=Decimal("0"), withholding=Decimal("0"),
                        net=Decimal("0"), reinvest_shares=Decimal("50"),
                        ex_date=date(2026, 6, 10))
        conn.commit()
        ahead = build_symbol_detail_csv(conn, symbol="2330", today=_DAY)
        on_the_day = build_symbol_detail_csv(conn, symbol="2330", today=date(2026, 7, 1))
    finally:
        conn.close()
    assert ahead is not None and on_the_day is not None
    rows = [ln for ln in ahead.content[3:].decode("utf-8").split("\r\n")[1:] if ln]
    assert "2026-07-01,cash,18200,0,18200,,,TWD,2026-07-01" in rows
    assert "2026-07-01,stock,0,0,0,50,,TWD," in rows  # its ex-date is past: it counts
    assert "2026-03-01,cash,5000,0,5000,,,TWD," in rows
    later = on_the_day.content[3:].decode("utf-8").split("\r\n")[1:]
    assert all(ln.endswith(",") for ln in later if ln), later
