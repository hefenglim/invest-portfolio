"""Is a cash pool below zero — and did a change put it there? (DEF-090 / DEF-091)

A pool's timeline here is its END-OF-DAY running balance, one point per day that has a line
(``portfolio/cash.py::running_eod``). Within a day credits are booked before debits, so a
day's lowest point IS its end-of-day balance and nothing finer is needed.

Two questions, one owner, so every cash guard answers them the same way:

* :func:`first_dip` — is the timeline below zero anywhere? (the edit / delete doors'
  ack-able ``negative_cash`` check, which deliberately also reports a dip it did not cause)
* :func:`new_dip` — did a change make a day negative, or a negative day more negative? (the
  hard withdraw and 換匯 guards, and the import-batch undo). Compared DAY BY DAY. The old
  rule compared the whole timeline's lowest point before and after, so a new negative
  stretch shallower than an older, unrelated dip went through — DEF-090: the Schwab TWD pool
  sat at −220,000 from 2026-01-12, a withdrawal back-dated to 07-17 left 07-19 and 07-20 at
  −148,000 / −153,000, and it was written. A dip the change does not touch still never
  blocks it: on every day before the change, and on every day it leaves alone, the two
  timelines are equal.

Both answer with :class:`Dip` — the FIRST day below zero, and the lowest point with its day.
Owner ruling 2026-10-01 (DEF-091, 2B): the message names both, because the first day is when
the money must be there by and the lowest point is how much is missing. It used to name only
the lowest day (M5-07), which for a withdrawal dated 2026-01-05 read 07-19 while the pool was
already short from 01-07.

Pure (dates and Decimals in, a Dip out), so ``data_ingestion`` and ``api`` share it from
``shared/`` without either importing the other.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

_ZERO = Decimal(0)

#: (day, end-of-day balance), date-ordered, one point per day that has a line.
Series = Sequence[tuple[date, Decimal]]


@dataclass(frozen=True)
class Dip:
    """Where a pool is short: ``first`` day below zero, and the ``low`` reached on ``low_on``
    (the earliest day it is reached — the day the money was first that short)."""

    first: date
    low: Decimal
    low_on: date


def _filled(series: Series, days: Sequence[date]) -> list[Decimal]:
    """The balance at the end of each of *days*: the last point on or before it, else 0."""
    out: list[Decimal] = []
    bal = _ZERO
    i = 0
    for day in days:
        while i < len(series) and series[i][0] <= day:
            bal = series[i][1]
            i += 1
        out.append(bal)
    return out


def _dip(points: Sequence[tuple[date, Decimal]]) -> Dip | None:
    """The Dip over already-selected short days, or None when there are none."""
    if not points:
        return None
    low_on, low = points[0]
    for day, bal in points[1:]:
        if bal < low:
            low_on, low = day, bal
    return Dip(first=points[0][0], low=low, low_on=low_on)


def first_dip(eod: Series) -> Dip | None:
    """The pool's own dip: the first day below zero and its lowest point, or None."""
    return _dip([(day, bal) for day, bal in eod if bal < _ZERO])


def _paired(before: Series, after: Series) -> tuple[list[date], list[Decimal], list[Decimal]]:
    days = sorted({d for d, _ in before} | {d for d, _ in after})
    return days, _filled(before, days), _filled(after, days)


def new_dip(before: Series, after: Series) -> Dip | None:
    """The dip a change makes or worsens: the days *after* is below zero AND below *before* —
    the HARD withdraw / 換匯 rule (money that is not there on a day cannot leave on it).

    ``None`` when the change leaves every day either non-negative or no lower than it was —
    so a dip the change does not touch never blocks it, and a deposit (which only raises
    balances) is never refused."""
    days, was, now = _paired(before, after)
    return _dip([(day, n) for day, b, n in zip(days, was, now, strict=True)
                 if n < _ZERO and n < b])


def caused_dip(before: Series, after: Series) -> Dip | None:
    """The dip a CORRECTION causes, for the ack-able scoped check (audit H3, the import-batch
    undo): a day that was not negative and now is, or a day pushed below the pool's old
    lowest point. Deepening a shortfall that was already there, without either, does not
    ask — the golden tw_broker pool is short from its first buy, and undoing a 1,000 deposit
    months later must not ask the owner to acknowledge that. The old comparison (lowest point
    before vs after) asked only in the second case, so a new negative stretch shallower than
    an older dip went unasked (DEF-090's blind spot)."""
    days, was, now = _paired(before, after)
    old_low = min([_ZERO, *was])
    return _dip([(day, n) for day, b, n in zip(days, was, now, strict=True)
                 if n < _ZERO and (b >= _ZERO or n < old_low)])
