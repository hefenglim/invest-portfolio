"""The TRACKED instrument universe — ONE definition of 「封存 = 停止追蹤」 for every scope.

封存 (``instruments.archived``, FU-D13) is the owner's "stop tracking": the symbol stays
registered, so every money figure is untouched, but nothing is FETCHED or COMPUTED for it on
the owner's behalf any more. Before DEF-064 (owner ruling ⑦, 2026-09-25) that meaning was
written out once per scope, and one scope forgot it:

* the quote / history / dividend-event worklist (``scheduler/jobs.py::build_worklist``) —
  ``WHERE COALESCE(archived,0)=0`` in its own SQL;
* the insight ``all_registered`` universe (DEF-059), the signal scan, the news "all" scope and
  the alert inputs — ``not i.archived`` over ``list_instruments``, each in its own module;
* the five external-snapshot jobs (FinMind chips / valuation / fundamentals, consensus,
  fundamentals union) via ``pricing/ingest.py::tw_universe`` / ``all_universe`` — **no filter
  at all**, so an archived symbol kept spending FinMind / yfinance / Finnhub quota every day.

Every one of them now reads :func:`tracked_instruments` / :func:`tracked_symbols`, so a
new scope cannot pick a different answer, and :data:`TRACKED_SQL` is the only spelling of
the predicate in SQL.

**A held symbol is never excluded**, and not because a scope unions the held set in: the
invariant 「持有 ⇒ 未封存」 is upheld at the WRITE side — the archive guard refuses a symbol
that holds a position today or on any later ledger date, and every ledger write that can give
a symbol shares re-activates it (``data_ingestion/holdings.py::holds_position``, used by both).
That is what lets ``pricing/`` (which may not replay the book) filter on the flag alone.

Borrowed table (``architecture.md`` table-read convention): ``instruments``, owned by
``data_ingestion/store.py``, read here by direct SQL. ``shared/`` imports nothing internal
except itself, so every layer — ``pricing/`` included — reaches this without a lateral import.
"""

import sqlite3
from dataclasses import dataclass

from portfolio_dash.shared.enums import Market

#: The one SQL spelling of "tracked" (not archived). ``COALESCE`` keeps a row written before
#: the column existed (NULL on a hand-migrated DB) tracked, which is the column's own default.
TRACKED_SQL = "COALESCE(archived, 0) = 0"


@dataclass(frozen=True)
class TrackedInstrument:
    """One tracked registry row, as a fetch scope needs it (board as stored, ``""`` if unset)."""

    symbol: str
    market: Market
    board: str


def tracked_instruments(
    conn: sqlite3.Connection, *, market: Market | None = None
) -> list[TrackedInstrument]:
    """Every NON-archived registered instrument (optionally one market), ordered by symbol.

    Reads ``instruments`` (a ``data_ingestion`` table) by direct SQL — see the module doc.
    """
    sql = f"SELECT symbol, market, board FROM instruments WHERE {TRACKED_SQL}"  # noqa: S608
    params: tuple[str, ...] = ()
    if market is not None:
        sql += " AND market = ?"
        params = (market.value,)
    sql += " ORDER BY symbol"
    return [
        TrackedInstrument(symbol=r[0], market=Market(r[1]), board=r[2] or "")
        for r in conn.execute(sql, params).fetchall()
    ]


def tracked_symbols(conn: sqlite3.Connection, *, market: Market | None = None) -> list[str]:
    """The symbols of :func:`tracked_instruments`, in the same order."""
    return [t.symbol for t in tracked_instruments(conn, market=market)]
