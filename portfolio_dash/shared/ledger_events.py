"""Same-day ordering for the ledger replay — one named, spaced enum.

``cost_basis.build_book`` sorts its event stream by ``(date, priority, sequence)``. Those
priorities used to be bare literals written in two places (the ``events.append`` calls) with
a third copy in the docstring. Inserting corporate actions between opening and buy would
have renumbered buy/sell/dividend, and an agent that updated two of the three copies
produces a **silently mis-ordered replay** — the worst kind of defect this project has: a
wrong number that looks right.

So the priorities are named and spaced by 10. The next event type inserts between two
existing ones without touching any stored value, and there is exactly one place to read.

**Trades of one day book in WRITE order, not buys-before-sells (DEF-012, 2026-09-23).** Until
then ``BUY = 20 < SELL = 30``, so a buy entered AFTER a same-day sell was replayed BEFORE
it: the functional test's B-18 (2884 — buy 200@44, sell 100@45, buy 100@46, all on one day)
previewed the sell at realized −1,580 and then booked −1,223 once the third row landed,
with the position's cost at 17,070 where the entry order gives 16,713.33. A transaction has
no time-of-day column, so the ONLY evidence of intraday order is the row id — the order the
owner entered them, or the order the statement listed them — and that is what the replay
now follows: ``TRADE`` is one priority for both sides, and the third sort key is the row's
position in the id-ordered ledger (``store.list_transactions`` selects ``ORDER BY
trade_date ASC, id ASC``). The manual preview appends its draft LAST (highest id), so a
preview and the replay that follows the write see the same day in the same order.

``BUY`` and ``SELL`` are kept as ALIASES of ``TRADE`` for the read paths that name the side
(``data_ingestion/holdings.py``, ``api/routers/symbol.py``); they are the same member and the
same integer, and any code that compared them was comparing the retired rule.

The stress-audit oracle deliberately keeps its OWN copy (spec §7.4): an oracle that imports
the implementation's ordering cannot detect an error in it.
"""

from enum import IntEnum


class EventPriority(IntEnum):
    """Same-day replay order. Lower runs first; equal priority books in ledger id order.

    ``CORPORATE_ACTION`` sits between OPENING and TRADE because an action re-denominates a
    position that already exists (opening inventory has been seeded) and must be applied
    before that day's trades, whose quantities are quoted in post-action terms. DIVIDEND
    stays after the day's trades: a payout dated on a trade date reaches the position the
    day's trades leave behind.
    """

    OPENING = 0
    CORPORATE_ACTION = 10
    TRADE = 20
    #: Aliases of ``TRADE`` (same member). A buy and a sell of one day no longer have a
    #: relative rank — their ledger ids do.
    BUY = 20
    SELL = 20
    DIVIDEND = 40
