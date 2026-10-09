"""A stored corporate-action row too malformed to be a :class:`CorporateAction`.

**The defect this file exists for was measured, not imagined (2026-08-11).** The three
`StoredCorporateAction` → `CorporateAction` conversions all raised on a bad row, and the
one in ``store.load_ledger_bundle`` sits **above** ``build_book``'s graceful refusal path.
So a single row with a non-integer ratio term did not degrade to 待釐清 — it took the whole
dashboard down with a `ValidationError`:

    RAISED: ValidationError
    1 validation error for CorporateAction

E6 rejects such a row at validation, but E21 already establishes that rows reach the ledger
behind entry validation, and — until W7 — ``validate_corporate_action`` has **zero**
production callers, so nothing enforced E6 at all.

**The resolution is neither of the two obvious ones.** Raising is a 500 on every page
(`domain-ledger.md`'s never-guess rule is about *prices*; the never-500 rule is absolute).
Dropping is worse than the 500 and the old docstrings said so correctly: a silently omitted
action produces a share count wrong by the ratio that looks entirely normal. The third
option is the one this codebase already invented for exactly this shape of problem —
**record and flag**: the row becomes a ``Book.unapplied_actions`` entry with a zh reason,
which blanks XIRR portfolio-wide with a named cause (D38 invariant 2) and marks the
position 待釐清. Not silent, not fatal, and no new vocabulary.
"""

import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.store import (
    insert_transaction,
    load_ledger_bundle,
    upsert_instrument,
)
from portfolio_dash.portfolio.cost_basis import UnbookableLedgerError, build_book
from portfolio_dash.shared.corporate_actions import ActionIndex
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side

D = Decimal
ACTION_DAY = date(2026, 6, 15)


def _write_raw_action(conn: sqlite3.Connection, **over: object) -> None:
    """Write a row straight to the table, bypassing every validator.

    That is not a contrived test hook: it is E21's stated reachable state (a hand edit),
    and until W7 wires ``validate_corporate_action`` it is also what the CSV importer and
    the API would do.
    """
    row: dict[str, object] = {
        "account_id": "schwab", "date": ACTION_DAY.isoformat(), "kind": "SPLIT",
        "from_symbol": "AAA", "to_symbol": "AAA",
        "ratio_to": "3", "ratio_from": "1", "cost_carry": None, "note": None,
    }
    row.update(over)
    conn.execute(
        "INSERT INTO corporate_actions (account_id,date,kind,from_symbol,to_symbol,"
        "ratio_to,ratio_from,cost_carry,note) VALUES (?,?,?,?,?,?,?,?,?)",
        tuple(row[k] for k in ("account_id", "date", "kind", "from_symbol", "to_symbol",
                               "ratio_to", "ratio_from", "cost_carry", "note")),
    )
    conn.commit()


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    c.execute(
        "INSERT INTO accounts (account_id, name, broker, settlement_ccy, funding_ccy, "
        "fee_rule_set, dividend_model) VALUES "
        "('schwab','Schwab','Schwab','USD','TWD','schwab_us','drip_us')"
    )
    for sym in ("AAA", "BBB"):
        upsert_instrument(c, Instrument(symbol=sym, market=Market.US,
                                        quote_ccy=Currency.USD, sector="Tech", name=sym))
    insert_transaction(c, account_id="schwab", symbol="AAA", side=Side.BUY,
                       quantity=D("100"), price=D("50"), fees=D("1"), tax=D("0"),
                       trade_date=date(2026, 1, 10))
    insert_transaction(c, account_id="schwab", symbol="BBB", side=Side.BUY,
                       quantity=D("80"), price=D("20"), fees=D("1"), tax=D("0"),
                       trade_date=date(2026, 1, 12))
    c.commit()
    return c


# --- the load must not raise -----------------------------------------------------------

# Each is a row the domain model refuses, arrived at a different way.
_MALFORMED = [
    pytest.param({"ratio_to": "0.2857"}, id="non-integer ratio (the measured case)"),
    pytest.param({"ratio_from": "0"}, id="zero denominator — a div-by-zero in the replay"),
    pytest.param({"ratio_to": "-3"}, id="negative term"),
    pytest.param({"kind": "MERGER"}, id="unknown kind"),
    pytest.param({"kind": "SPINOFF", "cost_carry": "1.5"}, id="cost_carry outside [0,1]"),
]

# NOT in the list above, and the distinction is the point: a SPLIT whose `to_symbol`
# differs from its `from_symbol` (**E20**) converts perfectly well. `CorporateAction`'s
# validator is deliberately **structural only** — the invariants every downstream formula
# needs in order not to divide by zero or scale by a rounded quotient. Ledger-consistency
# rules that need a user-facing zh message belong to `data_ingestion/validate.py`, and its
# own docstring names E20 as an example. This file tests the structural boundary; it must
# not quietly widen it.


@pytest.mark.parametrize("bad", _MALFORMED)
def test_a_malformed_row_does_not_raise_out_of_the_ledger_load(
    conn: sqlite3.Connection, bad: dict[str, object]
) -> None:
    """``load_ledger_bundle`` is above every never-500 guard, so it must not raise."""
    _write_raw_action(conn, **bad)
    bundle = load_ledger_bundle(conn)          # pre-fix: ValidationError
    assert bundle.actions == []
    assert len(bundle.unreadable_actions) == 1
    assert [t.symbol for t in bundle.transactions] == ["AAA", "BBB"]  # the rest survives


def test_the_unreadable_row_names_itself(conn: sqlite3.Connection) -> None:
    """A bare count would force the UI to say "something went wrong"."""
    _write_raw_action(conn, ratio_to="0.2857")
    (bad,) = load_ledger_bundle(conn).unreadable_actions
    assert (bad.account_id, bad.date, bad.from_symbol) == ("schwab", ACTION_DAY, "AAA")
    assert "0.2857" in bad.reason and "整數" in bad.reason


# --- and it reaches the same channel every other refusal uses --------------------------


def test_the_dashboard_path_records_it_instead_of_dying(conn: sqlite3.Connection) -> None:
    """Skip + record + flag — the ``_reject`` contract, reached from one layer up."""
    _write_raw_action(conn, ratio_to="0.2857")
    book = build_book(load_ledger_bundle(conn), allow_oversell=True)
    (u,) = book.unapplied_actions
    assert (u.account_id, u.from_symbol, u.date) == ("schwab", "AAA", ACTION_DAY)
    assert "0.2857" in u.reason
    # …and the position it names is marked, so the drawer has something to render.
    aaa = next(h for h in book.holdings if h.symbol == "AAA")
    assert aaa.unbookable_action is True
    assert aaa.shares == D("100")            # untouched: the ratio was never applied
    # Containment: the symbol with no action is untouched and unflagged (D38 invariant 1).
    bbb = next(h for h in book.holdings if h.symbol == "BBB")
    assert bbb.unbookable_action is False and bbb.shares == D("80")


def test_the_strict_path_still_raises(conn: sqlite3.Connection) -> None:
    """Import / 試算 must refuse loudly — the same split every other refusal makes."""
    _write_raw_action(conn, ratio_to="0.2857")
    with pytest.raises(UnbookableLedgerError, match="0.2857"):
        build_book(load_ledger_bundle(conn), allow_oversell=False)


def test_an_unreadable_row_is_date_filtered_by_through(conn: sqlite3.Connection) -> None:
    """The trend replays one bundle per day; a row must not haunt days before its own.

    ``through`` is where a new ledger gets forgotten — silently, because a book missing a
    ledger still builds.
    """
    _write_raw_action(conn, ratio_to="0.2857")
    bundle = load_ledger_bundle(conn)
    assert bundle.through(ACTION_DAY).unreadable_actions != []
    assert bundle.through(ACTION_DAY.replace(day=14)).unreadable_actions == []


# --- the other two conversions ---------------------------------------------------------


def test_the_action_index_survives_a_malformed_row(conn: sqlite3.Connection) -> None:
    """``ActionIndex.from_stored`` feeds the share walk AND the scheduler's price factor.

    Raising there takes down a scheduled price refresh — a background job whose failure the
    owner sees only as prices that stopped updating.
    """
    from portfolio_dash.data_ingestion.store import list_corporate_actions

    _write_raw_action(conn, ratio_to="0.2857")
    index = ActionIndex.from_stored(list_corporate_actions(conn))
    assert index.all == ()
    assert len(index.unreadable) == 1


def test_the_scheduler_factor_builder_survives_it(conn: sqlite3.Connection) -> None:
    """A malformed row contributes NO factor — correct, and safe, because the same row is
    simultaneously blanking XIRR and flagging the position through ``build_book``. The
    owner is warned; they are not warned by a crashed job."""
    from portfolio_dash.scheduler.jobs import split_factor_fn

    _write_raw_action(conn, ratio_to="0.2857")
    factor_of = split_factor_fn(conn)
    assert factor_of("AAA", after=date(2026, 6, 1), through=date(2026, 6, 30)) == D(1)


# --- containment ------------------------------------------------------------------------


def test_a_clean_ledger_is_untouched(conn: sqlite3.Connection) -> None:
    """The whole mechanism is invisible when nothing is wrong (D38 invariant 1)."""
    _write_raw_action(conn)                     # the DEFAULT row is a valid 3-for-1
    bundle = load_ledger_bundle(conn)
    assert bundle.unreadable_actions == []
    assert len(bundle.actions) == 1
    book = build_book(bundle, allow_oversell=True)
    assert book.unapplied_actions == []
    assert next(h for h in book.holdings if h.symbol == "AAA").shares == D("300")
