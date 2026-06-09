import sqlite3

from portfolio_dash.scheduler.jobs import build_worklist
from portfolio_dash.shared.enums import Market


def _add(conn: sqlite3.Connection, symbol: str, market: str, board: str | None) -> None:
    conn.execute(
        "INSERT INTO instruments (symbol, market, quote_ccy, sector, name, board) "
        "VALUES (?, ?, 'X', NULL, NULL, ?)",
        (symbol, market, board),
    )
    conn.commit()


def test_worklist_board_default_by_market(conn: sqlite3.Connection) -> None:
    _add(conn, "AAPL", "US", None)
    _add(conn, "3182", "MY", None)
    _add(conn, "2330", "TW", None)
    instruments, _ = build_worklist(conn, None)
    by_symbol = {i.symbol: i.board for i in instruments}
    assert by_symbol == {"AAPL": "", "3182": ".KL", "2330": "TWSE"}


def test_worklist_uses_stored_board(conn: sqlite3.Connection) -> None:
    _add(conn, "8299", "TW", "TPEx")  # resolved earlier and stored
    instruments, _ = build_worklist(conn, Market.TW)
    assert [(i.symbol, i.board) for i in instruments] == [("8299", "TPEx")]


def test_worklist_market_filter_and_fx_pairs(conn: sqlite3.Connection) -> None:
    _add(conn, "AAPL", "US", None)
    _add(conn, "2330", "TW", None)
    instruments, fx_pairs = build_worklist(conn, Market.US)
    assert [i.symbol for i in instruments] == ["AAPL"]
    assert fx_pairs  # the reporting-currency pairs are always returned
