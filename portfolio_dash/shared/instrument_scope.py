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

**An option contract is never tracked** (options spec §6.2, 2026-10-09): an open contract is
valued at its last trade price from the ledger (R2), so no provider is ever asked for its quote,
history, dividends, news, chips or fundamentals. ``TRACKED_SQL`` therefore also reads
``asset_type = 'stock'`` — a HELD contract stays out, and that is the point: unlike archiving,
this exclusion is by kind, not by the owner's choice. The bypasses that reach a provider for ONE
symbol (the quick-add, a restore, the gap backfill, ``refresh_instrument_quote``, the Alpha
Vantage held universe, the held-only news run) refuse a contract themselves, through
:func:`is_option_symbol` / :func:`stock_instruments` and :data:`OPTION_REGISTRATION_REFUSAL`;
``tests/contract/test_options_excluded_from_stock_readers.py`` scans for every other reader.

Borrowed table (``architecture.md`` table-read convention): ``instruments``, owned by
``data_ingestion/store.py``, read here by direct SQL. ``shared/`` imports nothing internal
except itself, so every layer — ``pricing/`` included — reaches this without a lateral import.
"""

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from portfolio_dash.shared.enums import Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.options import is_option_symbol

#: The refusal of every stock registration door for an option contract (options spec §6.2):
#: a contract is created by the trade door (``register_option_contract``, OPT-D7), never by
#: the quote-gated quick-add or a restore.
OPTION_REGISTRATION_REFUSAL = "選擇權合約由交易登錄建立"


#: Re-exported: :func:`shared.options.is_option_symbol` is the one definition.
__all__ = ["OPTION_REGISTRATION_REFUSAL", "TRACKED_SQL", "TrackedInstrument",
           "is_option_symbol", "stock_instruments", "tracked_instruments"]


def stock_instruments(instruments: Iterable[Instrument]) -> list[Instrument]:
    """*instruments* without the option contracts, order kept — the registry read every stock
    reader takes (OPT-D10: target weights, the held universes, the quote-job threshold)."""
    return [i for i in instruments if not i.is_option]


#: The one SQL spelling of "tracked": not archived, and a stock. ``COALESCE`` keeps a row
#: written before the ``archived`` column existed (NULL on a hand-migrated DB) tracked, which
#: is the column's own default; ``asset_type`` is ``NOT NULL DEFAULT 'stock'`` (options spec §3),
#: so every pre-options row migrated in as a stock and only ``register_option_contract`` writes
#: ``'option'``.
TRACKED_SQL = "COALESCE(archived, 0) = 0 AND asset_type = 'stock'"


@dataclass(frozen=True)
class TrackedInstrument:
    """One tracked registry row, as a fetch scope needs it (board as stored, ``""`` if unset)."""

    symbol: str
    market: Market
    board: str


def tracked_instruments(
    conn: sqlite3.Connection, *, market: Market | None = None
) -> list[TrackedInstrument]:
    """Every NON-archived registered STOCK (optionally one market), ordered by symbol — never
    an option contract (:data:`TRACKED_SQL`).

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
