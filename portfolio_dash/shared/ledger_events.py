"""Same-day ordering for the ledger replay — one named, spaced enum.

``cost_basis.build_book`` sorts its event stream by ``(date, priority)``. Those priorities
used to be bare literals written in two places (the ``events.append`` calls) with a third
copy in the docstring. Inserting corporate actions between opening and buy would have
renumbered buy/sell/dividend, and an agent that updated two of the three copies produces a
**silently mis-ordered replay** — the worst kind of defect this project has: a wrong number
that looks right.

So the priorities are named and spaced by 10. The next event type inserts between two
existing ones without touching any stored value, and there is exactly one place to read.

The stress-audit oracle deliberately keeps its OWN copy (spec §7.4): an oracle that imports
the implementation's ordering cannot detect an error in it.
"""

from enum import IntEnum


class EventPriority(IntEnum):
    """Same-day replay order. Lower runs first.

    ``CORPORATE_ACTION`` sits between OPENING and BUY because an action re-denominates a
    position that already exists (opening inventory has been seeded) and must be applied
    before that day's trades, whose quantities are quoted in post-action terms.
    """

    OPENING = 0
    CORPORATE_ACTION = 10
    BUY = 20
    SELL = 30
    DIVIDEND = 40
