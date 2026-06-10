import sqlite3

from portfolio_dash.data_ingestion.store import (
    get_instrument,
    list_instruments,
    upsert_instrument,
)
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument


def test_instrument_board_defaults_empty() -> None:
    inst = Instrument(
        symbol="AAPL", market=Market.US, quote_ccy=Currency.USD, sector="Tech", name="Apple"
    )
    assert inst.board == ""


def test_upsert_get_persists_board(conn: sqlite3.Connection) -> None:
    upsert_instrument(
        conn,
        Instrument(
            symbol="8299", market=Market.TW, quote_ccy=Currency.TWD,
            sector="Tech", name="X", board="TPEx",
        ),
    )
    got = get_instrument(conn, "8299")
    assert got is not None and got.board == "TPEx"


def test_legacy_null_board_reads_as_empty(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO instruments (symbol, market, quote_ccy, sector, name) "
        "VALUES ('2330','TW','TWD','Tech','TSMC')"
    )
    conn.commit()
    got = get_instrument(conn, "2330")
    assert got is not None and got.board == ""
    assert [i.symbol for i in list_instruments(conn)] == ["2330"]
