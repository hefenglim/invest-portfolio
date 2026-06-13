import sqlite3
from decimal import Decimal

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.register import register_instrument
from portfolio_dash.data_ingestion.store import get_instrument, upsert_instrument
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    return c


def test_instrument_new_fields_default(conn: sqlite3.Connection) -> None:
    inst = Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                      sector="Semis", name="TSMC", board="TWSE")
    assert inst.target_low is None and inst.is_etf is False
    upsert_instrument(conn, inst)
    got = get_instrument(conn, "2330")
    assert got is not None and got.target_low is None and got.is_etf is False


def test_instrument_fields_round_trip(conn: sqlite3.Connection) -> None:
    inst = Instrument(symbol="0056", market=Market.TW, quote_ccy=Currency.TWD,
                      sector="ETF", name="高股息", board="TWSE",
                      target_low=Decimal("36.50"), is_etf=True)
    upsert_instrument(conn, inst)
    got = get_instrument(conn, "0056")
    assert got is not None and got.target_low == Decimal("36.50") and got.is_etf is True


def test_register_sets_board_status_unresolved_for_tw_without_board(
    conn: sqlite3.Connection,
) -> None:
    inst = Instrument(symbol="8069", market=Market.TW, quote_ccy=Currency.TWD,
                      sector="Optoelectronics", name="元太")
    draft = register_instrument(conn, inst, prober=lambda _s: None, confirm=True)
    assert draft.written is True
    row = conn.execute("SELECT board, board_status FROM instruments WHERE symbol='8069'").fetchone()
    assert row["board"] == "" and row["board_status"] == "unresolved"


def test_register_sets_board_status_resolved_for_us(conn: sqlite3.Connection) -> None:
    inst = Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
                      sector="Tech", name="Apple")
    register_instrument(conn, inst, confirm=True)
    row = conn.execute("SELECT board, board_status FROM instruments WHERE symbol='AAPL'").fetchone()
    assert row["board"] == "" and row["board_status"] == "resolved"
