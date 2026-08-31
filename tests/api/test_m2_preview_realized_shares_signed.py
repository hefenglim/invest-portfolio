"""E-2: ``position_preview.realized_shares`` went NEGATIVE on an oversell into a short.

``_position_preview``'s undeclared-oversell branch took ``from_long, to_short =
held_shares, _ZERO``. ``held_shares`` is the SIGNED holding (``build_book`` reports an open
declared short as ``pos.shares - pos.short_shares``), so a sell that lands on an existing
short put a negative share count on the wire — e.g. held −5, sell 20 undeclared →
``"realized_shares": "-5"``.

The replay books **no realized row at all** in that branch (the basis is discarded, 待釐清),
and there are no long shares to realize when the position is already short: the honest value
is the LONG portion, which is ``max(held_shares, 0)`` — the ``long_shares`` local the sibling
branch three lines above already computes for exactly this reason.

Scope, stated so the next reader does not widen it by accident: only that ONE field moves.
``cost_removed`` / ``realized_pnl`` are already ``None`` on this branch, and the projected
totals already special-case the short (the replay zeroes only the LONG totals; an open short
keeps its proceeds). Both are asserted below against ``build_book`` itself, so a fix that
"tidied" them would fail here rather than pass quietly.
"""

import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.api.routers.input_center import ManualBody, _position_preview
from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_transaction,
    load_ledger_bundle,
    upsert_instrument,
)
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side

D = Decimal
_TSLA = Instrument(symbol="TSLA", market=Market.US, quote_ccy=Currency.USD,
                   sector="Auto", name="Tesla")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    bootstrap_db(conn)
    seed_accounts(conn)
    upsert_instrument(conn, _TSLA)
    return conn


def _tx(conn: sqlite3.Connection, side: Side, qty: str, price: str, d: date, *,
        short: bool = False) -> None:
    insert_transaction(conn, account_id="schwab", symbol="TSLA", side=side,
                       quantity=D(qty), price=D(price), fees=D("0"), tax=D("0"),
                       trade_date=d, short_sale=short)


def _body(qty: str, price: str, *, short: bool = False) -> ManualBody:
    return ManualBody(account_id="schwab", symbol="TSLA", side="SELL",
                      date=date(2026, 8, 1), shares=D(qty), price=D(price),
                      short_sale=short)


def _preview(conn: sqlite3.Connection, body: ManualBody) -> dict[str, object]:
    """Fee/tax 0 so the arithmetic below is the branch's, not the fee engine's."""
    out = _position_preview(conn, body, D("0"), D("0"), body.shares * body.price)
    assert out is not None
    return out


def _booked_position(conn: sqlite3.Connection) -> tuple[Decimal, Decimal, Decimal] | None:
    """(shares, original_avg, adjusted_avg) for schwab/TSLA after the ledger is replayed."""
    book = build_book(load_ledger_bundle(conn), allow_oversell=True)
    for h in book.holdings:
        if (h.account_id, h.symbol) == ("schwab", "TSLA"):
            return h.shares, h.original_avg, h.adjusted_avg
    return None


def _booked_realized(conn: sqlite3.Connection, on: date) -> list[Decimal]:
    return [r.realized for r in build_book(load_ledger_bundle(conn),
                                           allow_oversell=True).realized.rows
            if r.sell_date == on]


# --- the finding -------------------------------------------------------------------------

def test_an_oversell_into_an_open_short_realizes_no_shares() -> None:
    """The blueprint's case, verbatim: held −5, then an UNDECLARED sell of 20.

    ``build_book`` takes this row down the oversell branch (``ev.quantity 20 > pos.shares 0``)
    and emits no realized row, so nothing realizes — least of all a negative number of shares.
    """
    conn = _conn()
    _tx(conn, Side.SELL, "5", "100", date(2026, 4, 1), short=True)      # held −5
    body = _body("20", "300")                                          # undeclared
    pp = _preview(conn, body)

    assert pp["realized_shares"] == "0", f"negative/false realized share count: {pp}"
    assert pp["oversell"] is True
    assert pp["realized_pnl"] is None and pp["cost_removed"] is None
    assert pp["short_opened"] is None          # this branch opens nothing; it discards
    assert pp["remain_shares"] == "-25"

    # The ledger agrees that nothing realized on that date, which is what makes "0" honest.
    _tx(conn, Side.SELL, "20", "300", body.date)
    assert _booked_realized(conn, body.date) == []
    conn.close()


def test_the_money_fields_on_that_branch_are_unchanged_and_still_mirror_the_replay() -> None:
    """Counter-evidence that this repair moved ONE field.

    The projected averages are compared against the position ``build_book`` actually holds
    afterwards: the replay zeroes only the LONG totals, so the open short keeps its proceeds
    (−500 over −25 shares = 20). If the fix had zeroed or re-signed anything else, this fails.
    """
    conn = _conn()
    _tx(conn, Side.SELL, "5", "100", date(2026, 4, 1), short=True)
    body = _body("20", "300")
    pp = _preview(conn, body)

    _tx(conn, Side.SELL, "20", "300", body.date)
    booked = _booked_position(conn)
    assert booked is not None
    shares, orig_avg, adj_avg = booked
    assert Decimal(str(pp["remain_shares"])) == shares == D("-25")
    assert Decimal(str(pp["new_original_avg"])) == orig_avg == D("20")
    assert Decimal(str(pp["new_adjusted_avg"])) == adj_avg == D("20")
    conn.close()


def test_a_bigger_short_behind_a_long_shows_the_same_zero() -> None:
    """The R1 file's ``undeclared-into-a-short`` shape: buy 100, declare 150, sell 50.

    The declared sell closes the long and opens a 50-share short, so by the time the
    undeclared sell arrives the signed holding is −50 and no long lot exists at all.
    """
    conn = _conn()
    _tx(conn, Side.BUY, "100", "240", date(2026, 4, 1))
    _tx(conn, Side.SELL, "150", "260", date(2026, 5, 1), short=True)    # held −50
    pp = _preview(conn, _body("50", "300"))
    assert pp["realized_shares"] == "0", pp
    assert pp["oversell"] is True and pp["realized_pnl"] is None
    assert pp["remain_shares"] == "-100"
    conn.close()


# --- the controls: the long branches must not move ---------------------------------------

def test_an_oversell_from_a_LONG_position_still_reports_the_long_shares() -> None:
    """``max(held, 0) == held`` whenever the position is long, so this branch is untouched.

    Hold 100, sell 150 undeclared: the field keeps reporting the 100 that were held. (The
    replay still books no realized row — ``realized_pnl`` is ``None`` and stays ``None``;
    this field answers "how much of the sale came out of the long lot", not "what realized".)
    """
    conn = _conn()
    _tx(conn, Side.BUY, "100", "240", date(2026, 4, 1))
    pp = _preview(conn, _body("150", "300"))
    assert pp["realized_shares"] == "100"
    assert pp["oversell"] is True and pp["realized_pnl"] is None
    conn.close()


def test_the_ordinary_and_declared_branches_are_byte_identical() -> None:
    conn = _conn()
    _tx(conn, Side.BUY, "100", "240", date(2026, 4, 1))
    ordinary = _preview(conn, _body("40", "300"))
    assert ordinary["realized_shares"] == "40"
    assert Decimal(str(ordinary["realized_pnl"])) == D("2400")

    declared = _preview(conn, _body("150", "300", short=True))
    assert declared["realized_shares"] == "100"      # only the long portion realizes
    assert declared["short_opened"] == "50"
    assert Decimal(str(declared["realized_pnl"])) == D("6000")
    conn.close()


# --- the invariant, over every sell shape ------------------------------------------------

@pytest.mark.parametrize(
    ("setup", "qty", "price", "short"),
    [
        pytest.param([("BUY", "100", "240")], "40", "300", False, id="ordinary"),
        pytest.param([("BUY", "100", "240")], "100", "300", False, id="full-exit"),
        pytest.param([("BUY", "100", "240")], "150", "300", False, id="oversell"),
        pytest.param([("BUY", "100", "240")], "150", "300", True, id="declared-partial"),
        pytest.param([("BUY", "100", "240"), ("SHORT", "150", "260")],
                     "50", "300", True, id="short-extension"),
        pytest.param([("BUY", "100", "240"), ("SHORT", "150", "260")],
                     "50", "300", False, id="undeclared-into-a-short"),
        pytest.param([("SHORT", "5", "100")], "20", "300", False, id="oversell-into-short"),
        pytest.param([("SHORT", "5", "100")], "20", "300", True, id="declared-into-short"),
        pytest.param([], "30", "300", True, id="declared-short-from-flat"),
    ],
)
def test_realized_shares_is_never_negative_on_any_sell_shape(
    setup: list[tuple[str, str, str]], qty: str, price: str, short: bool
) -> None:
    """A share COUNT that realizes cannot be below zero on any of the nine shapes.

    Written as an invariant rather than nine expected values because that is the property:
    the field is a quantity taken out of the long lot, and a long lot is never negative.
    """
    conn = _conn()
    for kind, q, p in setup:
        _tx(conn, Side.BUY if kind == "BUY" else Side.SELL, q, p, date(2026, 4, 1),
            short=(kind == "SHORT"))
    pp = _preview(conn, _body(qty, price, short=short))
    assert Decimal(str(pp["realized_shares"])) >= 0, pp
    conn.close()
