"""Pure Decimal derivations over external snapshots (spec 20.5).

The calculation core for the chips/sentiment variables: it turns a trailing window
of raw snapshot numbers (institutional net buy, margin balances, monthly revenue,
PER/PBR history, VIX) into the values the LLM reasons about. It lives in
``portfolio/`` (not ``llm_insight/``) for the same reason ``technicals.py`` does:
**the LLM never emits numbers of record** — every signal here is a pure, unit-tested
function and only *assembled* into a prompt downstream.

Discipline (``rules/data-and-pricing.md``):

* Everything is :class:`~decimal.Decimal`. Daily-net sequences are ``list[Decimal]``
  in chronological order (oldest first, newest last) — mirroring ``technicals.py``.
* Every *ratio* returns ``None`` when its denominator is ``<= 0`` (domain-ledger
  discipline), so the caller renders it as missing rather than a fabricated number.
* No I/O, no connection — inputs are already-read values.
"""

from decimal import Decimal

_ZERO = Decimal("0")


def consecutive_buy_days(daily_net: list[Decimal]) -> int:
    """Length of the trailing run of strictly-positive net values (newest last).

    Counts back from the newest end while values are ``> 0``; a zero or negative
    breaks the run. ``0`` when the newest value is not positive or the list is empty.
    """
    count = 0
    for value in reversed(daily_net):
        if value > _ZERO:
            count += 1
        else:
            break
    return count


def consecutive_sell_days(daily_net: list[Decimal]) -> int:
    """Length of the trailing run of strictly-negative net values (newest last)."""
    count = 0
    for value in reversed(daily_net):
        if value < _ZERO:
            count += 1
        else:
            break
    return count


def net_buy_sum(daily_net: list[Decimal], days: int) -> Decimal:
    """Sum of the last ``days`` net values (fewer if the series is shorter)."""
    if days <= 0:
        return _ZERO
    return sum(daily_net[-days:], _ZERO)


def chg_pct(curr: Decimal, prev: Decimal) -> Decimal | None:
    """``(curr - prev) / prev``; ``None`` when ``prev <= 0`` (no honest base)."""
    if prev <= _ZERO:
        return None
    return (curr - prev) / prev


def yoy(curr: Decimal, year_ago: Decimal) -> Decimal | None:
    """Year-over-year change rate; ``None`` when ``year_ago <= 0``."""
    return chg_pct(curr, year_ago)


def mom(curr: Decimal, last_month: Decimal) -> Decimal | None:
    """Month-over-month change rate; ``None`` when ``last_month <= 0``."""
    return chg_pct(curr, last_month)


def percentile(value: Decimal, history: list[Decimal]) -> Decimal | None:
    """Fraction of historical values ``<= value`` (a rank in ``[0, 1]``).

    ``None`` for empty history. Uses ``<=`` so a value at the historical max scores
    ``1`` and below the historical min scores ``0``.
    """
    if not history:
        return None
    at_or_below = sum(1 for h in history if h <= value)
    return Decimal(at_or_below) / Decimal(len(history))


def vix_zone(vix: Decimal) -> str:
    """Classify a VIX level: ``<15`` low · ``15-25`` normal · ``25-35`` elevated · ``>=35`` high."""
    if vix < Decimal("15"):
        return "low"
    if vix < Decimal("25"):
        return "normal"
    if vix < Decimal("35"):
        return "elevated"
    return "high"


__all__ = [
    "chg_pct",
    "consecutive_buy_days",
    "consecutive_sell_days",
    "mom",
    "net_buy_sum",
    "percentile",
    "vix_zone",
    "yoy",
]
