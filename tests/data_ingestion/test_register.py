import sqlite3

import pytest

from portfolio_dash.data_ingestion.register import (
    autoregister_spinoff_child,
    register_instrument,
    spinoff_child_draft,
)
from portfolio_dash.data_ingestion.store import get_instrument, upsert_instrument
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


# --- NEW-21 (2026-09-06): the SPINOFF child's ETF flag is UNANSWERED, not "no" ------------


def _parent(*, is_etf: bool) -> Instrument:
    return Instrument(
        symbol="PRNT", market=Market.TW, quote_ccy=Currency.TWD, sector="Industrials",
        name="Parent Co", board="TWSE", is_etf=is_etf,
    )


def test_spinoff_child_draft_leaves_the_etf_flag_unanswered() -> None:
    """The child is a DIFFERENT company (the function's own docstring says so for name and
    sector), so nobody has answered whether it is an ETF. The model default ``False/False``
    reads as "answered: not an ETF", which is why AI-D40's ``etf_flag_unknown`` soft issue
    never fired on a spun-off TW ETF's first sell — it was taxed at 現股 0.3% with no
    disclosure. ``etf_flag_unknown=True`` is the same posture ``quick_register`` takes when
    it is handed ``is_etf=None``."""
    child = spinoff_child_draft(_parent(is_etf=False), "CHLD")
    assert child.etf_flag_unknown is True
    assert child.is_etf is False  # the fee engine still needs a number — a DISCLOSED one


def test_autoregistered_spinoff_child_persists_the_unknown_flag(conn: sqlite3.Connection) -> None:
    """Through the persisted path (D48a), and with a parent that IS an ETF: the flag is not
    inherited either way — it is left unanswered."""
    upsert_instrument(conn, _parent(is_etf=True))
    created = autoregister_spinoff_child(conn, parent_symbol="PRNT", child_symbol="CHLD")
    assert created is not None
    stored = get_instrument(conn, "CHLD")
    assert stored is not None
    assert stored.etf_flag_unknown is True
    assert stored.is_etf is False  # not copied from the parent: a fact about another company


@pytest.mark.parametrize("answer", [True, False])
def test_an_explicit_etf_answer_is_still_an_answer(conn: sqlite3.Connection, answer: bool) -> None:
    """Counter-proof: a registration that ANSWERS the question keeps ``etf_flag_unknown``
    False — NEW-21's fix touches only the SPINOFF draft, never an explicit registration."""
    inst = Instrument(
        symbol="0050", market=Market.TW, quote_ccy=Currency.TWD, sector="ETF", name="ETF 0050",
        board="TWSE", is_etf=answer, etf_flag_unknown=False,
    )
    register_instrument(conn, inst, prober=None, confirm=True)
    stored = get_instrument(conn, "0050")
    assert stored is not None
    assert stored.is_etf is answer
    assert stored.etf_flag_unknown is False
