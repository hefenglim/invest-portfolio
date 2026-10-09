"""Options W3 — ``data_ingestion/manual.py``'s option helpers (spec §4.2, plan W3).

The two-leg assignment is written in ONE database transaction — a failed second leg rolls the
first back — and a hard issue on either leg writes nothing; the close-row builder the expiry
inbox reuses derives its side and quantity from the position; the in-memory contract draft a
preview uses equals what the registrar writes.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion import manual
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.manual import (
    assignment_stock_leg,
    close_row_input,
    enter_pair,
    enter_transaction,
    option_contract_draft,
)
from portfolio_dash.data_ingestion.register import register_option_contract
from portfolio_dash.data_ingestion.store import (
    get_instrument,
    insert_transaction,
    list_transactions,
    upsert_instrument,
)
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.options import parse_option_symbol

PUT = "XYZ261218P00050000"
CALL = "XYZ261218C00060000"
EXPIRY = date(2026, 12, 18)


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    seed_accounts(c)
    upsert_instrument(c, Instrument(symbol="XYZ", market=Market.US, quote_ccy=Currency.USD,
                                    sector="Tech", name="XYZ Corp"))
    yield c
    c.close()


def _short(conn: sqlite3.Connection) -> None:
    register_option_contract(conn, PUT)
    insert_transaction(conn, account_id="schwab", symbol=PUT, side=Side.SELL,
                       quantity=Decimal("200"), price=Decimal("1.5"), fees=Decimal("1.31"),
                       tax=Decimal("0"), trade_date=date(2026, 11, 2), short_sale=True)


def test_the_preview_draft_is_what_the_registrar_writes(conn: sqlite3.Connection) -> None:
    terms = parse_option_symbol(PUT)
    assert terms is not None
    draft = option_contract_draft(terms)
    stored = register_option_contract(conn, PUT)
    fields = ("symbol", "market", "quote_ccy", "sector", "name", "board", "is_etf",
              "etf_flag_unknown", "asset_type")
    assert {f: getattr(draft, f) for f in fields} == {f: getattr(stored, f) for f in fields}


def test_close_row_input_derives_side_and_the_whole_position(conn: sqlite3.Connection) -> None:
    _short(conn)
    inp = close_row_input(conn, account_id="schwab", symbol=PUT, close_reason="expired",
                          trade_date=EXPIRY)
    assert (inp.side, inp.quantity, inp.price, inp.fee, inp.tax, inp.short_sale) == (
        Side.BUY, Decimal("200"), Decimal("0"), Decimal("0"), Decimal("0"), False)
    draft = enter_transaction(conn, inp, confirm=True, today=EXPIRY)
    assert draft.written and draft.fee_rule_snapshot == {"source": "close_reason"}


def test_the_stock_leg_follows_right_and_direction() -> None:
    put, call = parse_option_symbol(PUT), parse_option_symbol(CALL)
    assert put is not None and call is not None

    def leg(reason: str, terms: Any) -> tuple[Side, Decimal, str]:
        from portfolio_dash.data_ingestion.validate import TxnInput

        opt = TxnInput(account_id="schwab", symbol="x", side=Side.BUY,
                       quantity=Decimal("200"), price=Decimal("0"), trade_date=EXPIRY,
                       close_reason=reason)  # type: ignore[arg-type]
        s = assignment_stock_leg(opt, terms)
        return s.side, s.price, s.symbol

    assert leg("assigned", put) == (Side.BUY, Decimal("50"), "XYZ")
    assert leg("assigned", call) == (Side.SELL, Decimal("60"), "XYZ")
    assert leg("exercised", call) == (Side.BUY, Decimal("60"), "XYZ")
    assert leg("exercised", put) == (Side.SELL, Decimal("50"), "XYZ")


def test_the_pair_writes_both_rows(conn: sqlite3.Connection) -> None:
    _short(conn)
    opt_inp = close_row_input(conn, account_id="schwab", symbol=PUT, close_reason="assigned",
                              trade_date=EXPIRY)
    terms = parse_option_symbol(PUT)
    assert terms is not None
    opt, stock = enter_pair(conn, opt_inp, assignment_stock_leg(opt_inp, terms),
                            confirm=True, today=EXPIRY)
    assert opt.written and stock.written
    assert [t.symbol for t in list_transactions(conn, account_id="schwab")][-2:] == [PUT, "XYZ"]


def test_a_failed_second_leg_rolls_the_first_back(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _short(conn)
    before = len(list_transactions(conn))
    opt_inp = close_row_input(conn, account_id="schwab", symbol=PUT, close_reason="assigned",
                              trade_date=EXPIRY)
    terms = parse_option_symbol(PUT)
    assert terms is not None
    real = insert_transaction
    calls = {"n": 0}

    def flaky(*args: Any, **kwargs: Any) -> int:
        calls["n"] += 1
        if calls["n"] == 2:
            raise sqlite3.OperationalError("disk I/O error")
        return real(*args, **kwargs)

    monkeypatch.setattr(manual, "insert_transaction", flaky)
    with pytest.raises(sqlite3.OperationalError):
        enter_pair(conn, opt_inp, assignment_stock_leg(opt_inp, terms), confirm=True,
                   today=EXPIRY)
    assert calls["n"] == 2
    assert len(list_transactions(conn)) == before


def test_a_hard_issue_on_either_leg_writes_nothing(conn: sqlite3.Connection) -> None:
    _short(conn)
    opt_inp = close_row_input(conn, account_id="schwab", symbol=PUT, close_reason="assigned",
                              trade_date=EXPIRY, quantity=Decimal("300"))   # beyond the short
    terms = parse_option_symbol(PUT)
    assert terms is not None
    before = len(list_transactions(conn))
    opt, stock = enter_pair(conn, opt_inp, assignment_stock_leg(opt_inp, terms),
                            confirm=True, today=EXPIRY)
    assert not opt.written and not stock.written
    assert len(list_transactions(conn)) == before


def test_a_preview_registers_nothing(conn: sqlite3.Connection) -> None:
    from portfolio_dash.data_ingestion.validate import TxnInput

    inp = TxnInput(account_id="schwab", symbol=PUT, side=Side.SELL, quantity=Decimal("100"),
                   price=Decimal("1"), trade_date=date(2026, 11, 2), short_sale=True)
    draft = enter_transaction(conn, inp, confirm=False)
    assert draft.registers_contract and draft.instrument is not None
    assert get_instrument(conn, PUT) is None
