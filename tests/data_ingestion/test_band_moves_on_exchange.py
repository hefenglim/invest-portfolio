"""D47 — an EXCHANGE carries the owner's price-alert band to the new ticker.

Owner ruling 2026-08-15: 「ticker更換簡單一些可以直接換名字就好，其他都不動」. So the band moves
and its values do not change — an EXCHANGE re-keys a position, it does not re-denominate one
(§5.1's price re-expression is SPLIT-scoped, which is why D44 is a separate decision).

The three things that are easy to get wrong, and are therefore pinned here:

* it is a **move**, not a copy — ``target_cross`` fires for every REGISTERED symbol with a
  band, held or watched, so a band left on a dead ticker keeps alerting forever;
* it **never overwrites** a band the owner set on the destination themselves;
* ``target_set_at`` **rides across unchanged**, because the band's value did not change and
  re-deriving it would tell D44 that this band is newer than the next split.
"""

import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.store import (
    get_instrument,
    move_target_band,
    pending_band_move,
    upsert_instrument,
)
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument

D = Decimal
SET_ON = date(2026, 3, 1)


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    for sym in ("OLD", "NEW", "OTHER"):
        upsert_instrument(c, Instrument(symbol=sym, market=Market.US,
                                        quote_ccy=Currency.USD, sector="Tech", name=sym))
    c.commit()
    return c


def _band(
    conn: sqlite3.Connection, symbol: str, *,
    low: str | None = None, high: str | None = None, on: date = SET_ON,
) -> None:
    inst = get_instrument(conn, symbol)
    assert inst is not None
    upsert_instrument(
        conn,
        inst.model_copy(update={"target_low": D(low) if low else None,
                                "target_high": D(high) if high else None}),
        today=on,
    )


def test_the_band_moves_and_the_source_is_cleared(conn: sqlite3.Connection) -> None:
    """★ Clearing the source is the half that makes this a fix rather than a duplicate."""
    _band(conn, "OLD", low="180", high="240")
    moved = move_target_band(conn, from_symbol="OLD", to_symbol="NEW")

    assert moved is not None and moved.target_low == D("180")
    new = get_instrument(conn, "NEW")
    old = get_instrument(conn, "OLD")
    assert new is not None and (new.target_low, new.target_high) == (D("180"), D("240"))
    assert old is not None and (old.target_low, old.target_high) == (None, None)


def test_the_values_are_NOT_restated(conn: sqlite3.Connection) -> None:
    """「其他都不動」. A ratio on the EXCHANGE row changes the share count, not what the
    owner meant by "alert me at 180" — and D44 is the decision about re-denomination, which
    this deliberately is not."""
    _band(conn, "OLD", low="180")
    move_target_band(conn, from_symbol="OLD", to_symbol="NEW")
    new = get_instrument(conn, "NEW")
    assert new is not None and new.target_low == D("180")


def test_the_date_rides_across_unchanged(conn: sqlite3.Connection) -> None:
    """★ The reason this writes the columns directly instead of going through
    ``upsert_instrument``, which would DERIVE the stamp and set it to today. The band is
    the same band; re-dating it would make D44 read it as newer than a later split and go
    silent on exactly the case it exists for."""
    _band(conn, "OLD", low="180", on=date(2019, 5, 6))
    move_target_band(conn, from_symbol="OLD", to_symbol="NEW")
    new = get_instrument(conn, "NEW")
    assert new is not None and new.target_set_at == date(2019, 5, 6)
    # …and the source keeps no orphan date for a band it no longer has.
    old = get_instrument(conn, "OLD")
    assert old is not None and old.target_set_at is None


def test_a_band_the_owner_already_set_is_never_overwritten(
    conn: sqlite3.Connection,
) -> None:
    """The destination's band is the owner's judgement about the destination security. No
    merge rule could be right, so nothing happens — to either symbol."""
    _band(conn, "OLD", low="180")
    _band(conn, "NEW", low="42")
    assert move_target_band(conn, from_symbol="OLD", to_symbol="NEW") is None

    new = get_instrument(conn, "NEW")
    old = get_instrument(conn, "OLD")
    assert new is not None and new.target_low == D("42")
    assert old is not None and old.target_low == D("180")   # …and the source is untouched


def test_nothing_to_move_is_a_quiet_no_op(conn: sqlite3.Connection) -> None:
    assert move_target_band(conn, from_symbol="OLD", to_symbol="NEW") is None
    assert move_target_band(conn, from_symbol="OLD", to_symbol="MISSING") is None
    _band(conn, "OLD", low="180")
    # Same symbol both ends is a SPLIT's shape (E20) and never a rename.
    assert move_target_band(conn, from_symbol="OLD", to_symbol="OLD") is None


def test_moving_twice_is_idempotent(conn: sqlite3.Connection) -> None:
    """One event, N account rows (D13), and the CSV door calls this per row. The second
    call finds a cleared source and does nothing — which is also what makes it safe to put
    in the row writer rather than in each caller."""
    _band(conn, "OLD", low="180")
    assert move_target_band(conn, from_symbol="OLD", to_symbol="NEW") is not None
    assert move_target_band(conn, from_symbol="OLD", to_symbol="NEW") is None
    new = get_instrument(conn, "NEW")
    assert new is not None and new.target_low == D("180")


def test_the_preview_predicate_agrees_with_the_write_and_changes_nothing(
    conn: sqlite3.Connection,
) -> None:
    """★ The form promises the move before saving. If the promise and the write used
    different conditions, the sentence on screen would be one the writer does not honour —
    and there would be no test that could tell, because each half passes alone."""
    _band(conn, "OLD", low="180", high="240")
    promised = pending_band_move(conn, from_symbol="OLD", to_symbol="NEW")
    assert promised is not None
    # Reading must not write: the preview runs on every keystroke.
    still = get_instrument(conn, "OLD")
    assert still is not None and still.target_low == D("180")

    performed = move_target_band(conn, from_symbol="OLD", to_symbol="NEW")
    assert performed == promised

    # …and once it has happened, the predicate stops promising it.
    assert pending_band_move(conn, from_symbol="OLD", to_symbol="NEW") is None


def test_the_move_can_defer_its_commit_to_an_enclosing_batch(
    conn: sqlite3.Connection,
) -> None:
    """★ A mid-batch commit defeats ``commit_preview``'s all-or-nothing gate SILENTLY.

    The CSV door writes every row with ``commit=False`` and commits once, rolling the whole
    batch back if any row fails. A band that committed on its own would then survive an
    EXCHANGE that never happened — and no test of the *rows* could see it, because the rows
    are correctly absent. Proven by doing the rollback here.
    """
    _band(conn, "OLD", low="180")
    assert move_target_band(conn, from_symbol="OLD", to_symbol="NEW", commit=False)
    conn.rollback()

    old = get_instrument(conn, "OLD")
    new = get_instrument(conn, "NEW")
    assert old is not None and old.target_low == D("180")   # …exactly where it started
    assert new is not None and new.target_low is None


def test_the_child_registration_can_defer_its_commit_too(
    conn: sqlite3.Connection,
) -> None:
    """Same gate, the other D48 half: an instrument left behind by a rolled-back import is a
    phantom the owner never asked for — and `register_instrument` writes the row and its
    ``board_status`` as two statements, so the deferred commit is what keeps those atomic."""
    from portfolio_dash.data_ingestion.register import autoregister_spinoff_child

    assert autoregister_spinoff_child(
        conn, parent_symbol="OLD", child_symbol="KID", commit=False) is not None
    conn.rollback()
    assert get_instrument(conn, "KID") is None
