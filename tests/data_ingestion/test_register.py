import sqlite3

from portfolio_dash.data_ingestion.register import register_instrument
from portfolio_dash.data_ingestion.store import get_instrument
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument


def _inst(symbol: str, market: Market, ccy: Currency, board: str = "") -> Instrument:
    return Instrument(
        symbol=symbol, market=market, quote_ccy=ccy, sector="X", name=symbol, board=board
    )


def test_us_board_empty_no_flag(conn: sqlite3.Connection) -> None:
    d = register_instrument(conn, _inst("AAPL", Market.US, Currency.USD), confirm=True)
    assert d.instrument.board == "" and not d.issues and d.written
    got = get_instrument(conn, "AAPL")
    assert got is not None and got.board == ""


def test_my_board_kl(conn: sqlite3.Connection) -> None:
    d = register_instrument(conn, _inst("3182", Market.MY, Currency.MYR), confirm=True)
    assert d.instrument.board == ".KL" and d.written


def test_tw_board_probed(conn: sqlite3.Connection) -> None:
    d = register_instrument(
        conn, _inst("8299", Market.TW, Currency.TWD), prober=lambda s: "TPEx", confirm=True
    )
    assert d.instrument.board == "TPEx" and not d.issues
    got = get_instrument(conn, "8299")
    assert got is not None and got.board == "TPEx"


def test_tw_unresolved_flagged_but_writes(conn: sqlite3.Connection) -> None:
    d = register_instrument(
        conn, _inst("9999", Market.TW, Currency.TWD), prober=lambda s: None, confirm=True
    )
    assert d.instrument.board == ""
    assert any(i.kind == "board_unresolved" for i in d.issues)
    assert d.written  # soft flag does not block registration


def test_no_confirm_does_not_write(conn: sqlite3.Connection) -> None:
    d = register_instrument(
        conn, _inst("2330", Market.TW, Currency.TWD), prober=lambda s: "TWSE", confirm=False
    )
    assert d.instrument.board == "TWSE" and not d.written
    assert get_instrument(conn, "2330") is None


def test_preset_board_respected_no_probe(conn: sqlite3.Connection) -> None:
    calls: list[str] = []

    def prober(symbol: str) -> str | None:
        calls.append(symbol)
        return "TWSE"

    d = register_instrument(
        conn, _inst("8299", Market.TW, Currency.TWD, board="TPEx"), prober=prober, confirm=True
    )
    assert d.instrument.board == "TPEx" and calls == []  # pre-set board respected; no probe
