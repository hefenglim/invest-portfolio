import sqlite3
from datetime import date
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
                      target_low=Decimal("36.50"), target_high=Decimal("42.80"), is_etf=True)
    upsert_instrument(conn, inst)
    got = get_instrument(conn, "0056")
    assert got is not None and got.target_low == Decimal("36.50") and got.is_etf is True
    assert got.target_high == Decimal("42.80")


def test_instrument_target_high_defaults_none_and_clears(conn: sqlite3.Connection) -> None:
    # FU-D28: target_high is optional and independently clearable (upsert writes None).
    inst = Instrument(symbol="2454", market=Market.TW, quote_ccy=Currency.TWD,
                      sector="Semis", name="MediaTek", board="TWSE",
                      target_high=Decimal("1200"))
    upsert_instrument(conn, inst)
    got = get_instrument(conn, "2454")
    assert got is not None and got.target_low is None and got.target_high == Decimal("1200")
    upsert_instrument(conn, got.model_copy(update={"target_high": None}))
    cleared = get_instrument(conn, "2454")
    assert cleared is not None and cleared.target_high is None


# ------------------------------------------------------- D44: target_set_at (the stamp)
#
# The stamp exists to answer ONE question — "does this band predate that split?" — so the
# tests below are all about the boundary of when it moves. It must move on a band change
# and on nothing else: a stamp that moved on any write would date the ROW, and the D44
# finding (which compares it against the action date) would go permanently silent on a
# ledger where anything ever edits an instrument. That failure is invisible — the finding
# simply stops firing — which is why it is pinned here rather than left to the finding's
# own tests.

def _tw(
    symbol: str, name: str, *,
    low: Decimal | None = None, high: Decimal | None = None,
    set_at: date | None = None,
) -> Instrument:
    """A TW instrument, built with TYPED arguments rather than a ``**dict`` splat.

    The splat was the obvious shorthand and mypy rejects it under strict: a dict literal
    widens to ``dict[str, object]``, so every enum and Decimal field it feeds becomes an
    ``arg-type`` error. Naming the parameters keeps the constructor checkable, which is the
    point of running the type gate over ``tests`` at all."""
    return Instrument(symbol=symbol, market=Market.TW, quote_ccy=Currency.TWD,
                      board="TWSE", sector="Semis", name=name,
                      target_low=low, target_high=high, target_set_at=set_at)


def _stamped(conn: sqlite3.Connection, symbol: str) -> date | None:
    got = get_instrument(conn, symbol)
    assert got is not None
    return got.target_set_at


def test_a_band_written_at_registration_carries_its_date(conn: sqlite3.Connection) -> None:
    upsert_instrument(conn, _tw("2330", "TSMC", low=Decimal("800")),
                      today=date(2026, 1, 5))
    assert _stamped(conn, "2330") == date(2026, 1, 5)


def test_no_band_means_no_date(conn: sqlite3.Connection) -> None:
    """Never a date for a band that is not there — the finding reads a bare date as a claim."""
    upsert_instrument(conn, _tw("2330", "TSMC"), today=date(2026, 1, 5))
    assert _stamped(conn, "2330") is None


def test_an_UNRELATED_edit_does_not_move_the_stamp(conn: sqlite3.Connection) -> None:
    """★ The load-bearing one. Rename it, classify it, flag it as an ETF — the band did not
    move, so its date must not either."""
    upsert_instrument(
        conn, _tw("0056", "高股息", low=Decimal("36.5"), high=Decimal("42.8")),
        today=date(2026, 1, 5))
    stored = get_instrument(conn, "0056")
    assert stored is not None
    upsert_instrument(
        conn,
        stored.model_copy(update={"name": "元大高股息", "sector": "Financials",
                                  "industry": "Asset Management", "is_etf": True}),
        today=date(2026, 6, 30),
    )
    assert _stamped(conn, "0056") == date(2026, 1, 5)


def test_changing_either_leg_moves_the_stamp(conn: sqlite3.Connection) -> None:
    upsert_instrument(
        conn, _tw("0056", "高股息", low=Decimal("36.5"), high=Decimal("42.8")),
        today=date(2026, 1, 5))
    stored = get_instrument(conn, "0056")
    assert stored is not None
    upsert_instrument(conn, stored.model_copy(update={"target_high": Decimal("45")}),
                      today=date(2026, 6, 30))
    assert _stamped(conn, "0056") == date(2026, 6, 30)


def test_clearing_the_band_clears_the_stamp(conn: sqlite3.Connection) -> None:
    upsert_instrument(conn, _tw("2454", "MediaTek", high=Decimal("1200")),
                      today=date(2026, 1, 5))
    stored = get_instrument(conn, "2454")
    assert stored is not None
    upsert_instrument(conn, stored.model_copy(update={"target_high": None}),
                      today=date(2026, 6, 30))
    assert _stamped(conn, "2454") is None


def test_the_model_field_is_read_only_and_cannot_forge_a_date(
    conn: sqlite3.Connection,
) -> None:
    """One owner (``upsert_instrument``). A caller handing in its own ``target_set_at`` is
    ignored — otherwise "when was this band set?" would have as many answers as writers."""
    upsert_instrument(
        conn, _tw("2330", "TSMC", low=Decimal("800"), set_at=date(2019, 1, 1)),
        today=date(2026, 1, 5))
    assert _stamped(conn, "2330") == date(2026, 1, 5)


def test_a_legacy_row_migrates_in_with_no_date(conn: sqlite3.Connection) -> None:
    """A DB that predates the column: the band is real, its date is unknowable, and the
    honest value is NULL. The finding reads that as "make no claim", never as "old"."""
    upsert_instrument(conn, _tw("LEGACY", "Legacy", low=Decimal("10")),
                      today=date(2026, 1, 5))
    conn.execute("UPDATE instruments SET target_set_at=NULL WHERE symbol='LEGACY'")
    conn.commit()  # …the state a pre-column DB migrates in with
    assert _stamped(conn, "LEGACY") is None
    # …and an unrelated re-save does NOT invent one.
    stored = get_instrument(conn, "LEGACY")
    assert stored is not None
    upsert_instrument(conn, stored.model_copy(update={"name": "Legacy Co"}),
                      today=date(2026, 6, 30))
    assert _stamped(conn, "LEGACY") is None


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
