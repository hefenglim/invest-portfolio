"""The read rule: re-express a carried-forward price into a valuation day's share terms.

Spec §5.1(d) · W6b (point reads) · W6c (series reads, D42). :func:`price_in` is the ONE
owner of the arithmetic; :func:`series_in` is the same rule applied to a whole
``get_price_history`` result and calls it point by point. Every consumer — the two
``portfolio/`` point seams and every ``api/`` series read — goes through one
:func:`split_factor`.

A stored close means *"as traded on its own date"* (``data-and-pricing.md``, and the write
seam in ``pricing/store.py`` enforces it). The ledger's share count, however, is in the
valuation day's terms: on the split date the replay applies the ratio immediately, while
``_at_or_before`` may still return the last **pre-split** price until the next refresh
writes one. Multiplying those two directly values the day at ``post-split shares ×
pre-split price`` — inflated by the whole ratio, on the trend *and* on the live dashboard.

    price_in(sym, d)  =  as_traded(pd)  ÷  Π{ ratio(k) : k ∈ keys(sym, pd, d) }

**Divide** — the opposite direction from the write seam, and over a different window. The
write asks *"which splits had the provider already folded into this delivered number?"*
(``(as_of, fetched_at]``) and multiplies them back out to recover the as-traded value; the
read asks *"which splits happened between this price's own date and the day I am valuing?"*
(``(pd, d]``) and divides to move that value into the new denomination. They compose, they
do not compound: the stored invariant sits between them, so a row fetched before a split
and a row fetched after it value the same day identically.

Continuity is then true by construction, which is the whole point (§2.1)::

    day pd  (pre-split):   S     × p
    day d   (post-split): (S×r)  × (p ÷ r)   =   S × p

**This is not guessing a price.** ``domain-ledger.md``'s "live price unobtainable → label
clearly, never guess" governs *market* prices — inventing a level nobody traded at.
Re-expressing a known price into post-split units is arithmetic on the unit of account,
exactly like ``average = total / shares`` correcting itself when the share count changes.
The same trade is quoted in the new denomination; nothing is invented.

**The superseded resolution — mark the split day ``incomplete`` — is measurably worse than
the defect** (trap #8). ``timeseries.py`` does not omit the *day*; it omits the *holding*
and still emits ``total_value``, so the net-worth chart would drop by the position's entire
value on every split date. Replacing an error of ``ratio`` with an error of 100% is not an
improvement. Nothing in this module ever flags a day.

It is a **read-path** transform, never written back to ``prices``, so 重算 stays
authoritative and the stored basis stays as-traded.

**The honest limitation — a symbol the owner never held is never re-expressed.**
``ActionIndex`` is built from the *recorded ledger actions*, which are per account. The
system has **no independent split feed**, so a series for a symbol with no ledger action —
most importantly a **benchmark** (``api/routers/performance.py`` reads
``bench.storage_key``, a ticker nobody holds) — keeps the provider's own basis even if the
real instrument split. That is a known gap, not a bug to route around: inferring a split
from a price gap would be guessing, which ``domain-ledger.md`` forbids, and a heuristic
here would silently rewrite market history. The repair is a recorded action, nothing else.

**Only the close is re-expressed** (D39b). ``volume``, and ``open``/``high``/``low``, have
no ``*_raw`` column of their own, so a factor applied to them could never be restated or
reversed — they keep the provider's basis and are therefore **discontinuous across a
split**. A consumer that compares volumes across a split date (the volume-confirmation
signal) is reading two different denominations; correcting that needs its own raw column,
not a read-path divide.
"""

from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from portfolio_dash.pricing.results import PriceRead
from portfolio_dash.shared.corporate_actions import ActionIndex, split_factor

_ONE = Decimal(1)


def price_in(
    index: ActionIndex,
    symbol: str,
    price: Decimal,
    *,
    priced_on: date,
    valued_on: date,
) -> Decimal:
    """*price*, as traded on ``priced_on``, expressed in ``valued_on``'s share terms.

    ``index`` is built **once per request** and passed in — never one per lookup (trap
    #21): the trend replay calls this once per held symbol per day, and rebuilding the
    grouped action ledger inside the loop would re-read it thousands of times for a
    multi-year series.

    Two short-circuits, in this order and for different reasons:

    1. **No SPLIT on this symbol's series → the untouched pre-existing expression.** This
       is D38 invariant 1's structural containment, not an optimisation: *code that does
       not execute cannot drift, while code that computes an equal answer can*. On a ledger
       with no corporate actions the arithmetic below never runs at all, so a defect in it
       cannot reach a symbol that has no action — which is what makes this change cheap to
       ship. ``tests/portfolio/test_dashboard.py`` plants a landmine on
       :func:`split_factor` to prove it.
    2. **Empty window → the identity.** ``split_factor`` returns an exact ``Decimal(1)``
       when the window catches nothing, and the correction then self-cancels the moment a
       genuine post-split price exists (``pd >= a.date`` → empty product), so it disappears
       without anyone switching it off. Returning *price* unchanged rather than dividing by
       one also keeps the stored representation intact: ``Decimal`` arithmetic can move an
       exponent, and this value flows into displayed money.

    ``split_factor`` is a **quotient** and is therefore correct only for prices, never for
    share counts (§5.1 detail 2, trap #2a) — share counts go through ``apply_ratio``, one
    action at a time. That this function takes a *price* and returns a *price* is the whole
    of its contract.
    """
    if not index.splits_on(symbol):
        return price
    factor = split_factor(index, symbol, after=priced_on, through=valued_on)
    if factor == _ONE:
        return price
    return price / factor


def series_in(
    index: ActionIndex,
    symbol: str,
    points: Sequence[PriceRead],
    *,
    valued_on: date,
) -> list[PriceRead]:
    """A whole ``get_price_history`` result, every close in ``valued_on``'s share terms.

    W6c (D42). :func:`price_in` fixes a price that meets a *share count* from another day;
    this fixes a series whose points are compared **to each other**. A stored close is as
    traded on its own date, so the moment a split falls inside the window the points are in
    two different denominations — and every series consumer is a comparison: a 52-week
    high/low, a moving average, a day-change percentage, a rebased index, a chart. A 7-for-1
    makes every pre-split point read 7x higher, which on the alert path is a **52-week-low
    breach that fires a real notification**. The defect is not new — a provider that
    restates its history mid-backfill produced the same discontinuity — but the action
    ledger is the first thing that can name it, and therefore fix it.

    ``index`` is built **ONCE per request** and threaded in (trap #21). Never one per point
    and never one per symbol: a multi-year series over a registered universe would otherwise
    re-read and re-group the whole action ledger thousands of times.

    The same structural short-circuit as :func:`price_in`, and for the same reason (D38
    invariant 1): with no SPLIT on this symbol the loop below **does not execute** and the
    caller gets its own rows back untouched, so a defect in the re-expression cannot reach a
    symbol that has no action. Code that does not run cannot drift.

    Returns *new* :class:`PriceRead` objects — never mutated in place, because these rows
    come straight from ``pricing.store`` and this is a read-path transform (nothing is ever
    written back to ``prices``). ``volume`` rides along **unchanged**: see the module
    docstring on D39b — it has no raw column, so it cannot be restated or reversed.
    """
    if not index.splits_on(symbol):
        return list(points)
    return [
        p.model_copy(
            update={
                "value": price_in(
                    index, symbol, p.value, priced_on=p.as_of, valued_on=valued_on
                )
            }
        )
        for p in points
    ]
