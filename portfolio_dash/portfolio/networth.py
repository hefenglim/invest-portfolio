"""Total net worth = holdings market value + cash, per day (FU-D29 / deferred C8).

A pure COMPOSITION layer over two already-verified series — it changes no
money-of-record path:

* ``daily_value_series`` (portfolio/timeseries.py) supplies the holdings market
  value per day; it is consumed UNCHANGED (this module never rebuilds it).
* ``cash_balances`` / ``pool_lines`` (portfolio/cash.py) supply the per-(account,
  currency) cash-pool math; this module replays their dated lines into a per-day
  running balance, converts each pool at that day's carry-forward FX (the SAME
  ``_fx_at`` the trend uses, via the shared ``convert`` helper) and sums across
  pools into a single reporting-currency total.

Net worth is a DISPLAY / attribution figure layered on the trend — never a
money-of-record input, feeding no return metric. Consistency is pinned by tests
(tests/portfolio/test_networth.py + tests/contract/test_networth_dashboard.py):
the last complete day reconstructs the verified ``cash_balances`` reporting total,
and composition leaves every pre-existing ``TrendPoint`` field byte-identical.

Incomplete-day rule (mirrors the trend's ``incomplete`` semantics): a day on which
a NON-ZERO pool has no on-or-before FX rate for its currency is marked
``incomplete`` and that pool's value is excluded (never guessed); a ZERO-balance
pool missing FX does NOT poison the day. ``compose_net_worth`` leaves
``net_worth = None`` on an incomplete cash day so the frontend draws an honest gap
rather than a fabricated total.
"""

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from portfolio_dash.portfolio.cash import (
    CashLine,
    _DivRow,
    _FxRow,
    _MovementRow,
    _TxRow,
    cash_balances,
    pool_lines,
    running_statement,
)
from portfolio_dash.portfolio.dashboard_models import TrendPoint, TrendSeries
from portfolio_dash.portfolio.timeseries import FxHistory, _fx_at
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.fx import convert
from portfolio_dash.shared.models.assets import Instrument

_ZERO = Decimal("0")


@dataclass(frozen=True)
class CashDay:
    """Reporting-currency cash total on one day + whether a non-zero pool lacked FX."""

    reporting_total: Decimal
    incomplete: bool


def _day_steps(statement: list[tuple[CashLine, Decimal]]) -> list[tuple[date, Decimal]]:
    """Collapse a date-ordered running statement to end-of-day balances.

    ``running_statement`` is chronological (date asc, same-day credits before debits),
    so the LAST running balance carrying a given date is that day's end-of-day balance.
    Returns an ascending ``(date, balance)`` step list (one entry per distinct date)."""
    by_day: dict[date, Decimal] = {}
    for line, running in statement:
        by_day[line.date] = running
    return sorted(by_day.items())


def _balance_on(steps: list[tuple[date, Decimal]], on: date) -> Decimal:
    """End-of-day running balance at-or-before ``on`` (carry-forward); 0 before day one."""
    idx = bisect_right(steps, on, key=lambda item: item[0])
    if idx == 0:
        return _ZERO
    return steps[idx - 1][1]


def daily_cash_series(
    movements: Sequence[_MovementRow],
    fx_conversions: Sequence[_FxRow],
    transactions: Sequence[_TxRow],
    dividends: Sequence[_DivRow],
    instruments: dict[str, Instrument],
    fx_history: FxHistory,
    reporting: Currency,
    *,
    end: date,
) -> dict[date, CashDay]:
    """Per-day reporting-currency cash total, replaying each pool's dated lines.

    Returns a dense ``{day: CashDay}`` over ``[first_line_date, end]``. For each day
    every pool's carry-forward end-of-day balance is converted at that day's FX and
    summed; a non-zero pool with no on-or-before FX marks the day incomplete and is
    excluded from the sum (a zero-balance pool missing FX is silently skipped). The
    reporting pool needs no FX. Unregistered symbols are skipped exactly as
    ``cash_balances`` skips them (an unbookable row never poisons the series)."""
    # Enumerate every touched (account, ccy) pool via the verified balance engine.
    pool_keys = sorted(
        cash_balances(movements, fx_conversions, transactions, dividends, instruments),
        key=lambda k: (k[0], k[1].value),
    )
    if not pool_keys:
        return {}

    # Per pool: its currency + ascending end-of-day balance step list.
    pools: list[tuple[Currency, list[tuple[date, Decimal]]]] = []
    first_date: date | None = None
    for account_id, ccy in pool_keys:
        steps = _day_steps(running_statement(pool_lines(
            account_id, ccy, movements, fx_conversions, transactions, dividends,
            instruments)))
        if not steps:
            continue  # pool with only unregistered rows -> no lines
        pools.append((ccy, steps))
        pool_start = steps[0][0]
        first_date = pool_start if first_date is None else min(first_date, pool_start)

    if first_date is None or end < first_date:
        return {}

    out: dict[date, CashDay] = {}
    day = first_date
    while day <= end:
        total = _ZERO
        incomplete = False
        for ccy, steps in pools:
            balance = _balance_on(steps, day)
            if balance == _ZERO:
                continue  # a zero pool contributes nothing and needs no FX
            if ccy == reporting:
                total += balance
                continue
            rate = _fx_at(fx_history, day, ccy, reporting)
            if rate is None:
                incomplete = True  # non-zero pool, no on-or-before FX -> honest gap
                continue
            total += convert(balance, rate)
        out[day] = CashDay(reporting_total=total, incomplete=incomplete)
        day += timedelta(days=1)
    return out


def compose_net_worth(
    trend: TrendSeries, cash_by_date: dict[date, CashDay]
) -> TrendSeries:
    """Enrich each trend point with ``net_worth = total_value + cash_that_day``.

    Pure composition: aligns on the trend's date axis (cash before its first line = 0),
    and returns a NEW series whose points differ from the input ONLY in ``net_worth``
    (every pre-existing field is copied byte-identically). ``net_worth`` is left None on
    a cash-incomplete day (a non-zero pool lacked FX) so the frontend draws a gap rather
    than a fabricated total; on a holdings-incomplete day it mirrors ``total_value`` (the
    same partial value the existing lines draw, flagged by the shared incomplete marker).
    """
    if not trend.points:
        return trend
    new_points: list[TrendPoint] = []
    for point in trend.points:
        cash = cash_by_date.get(point.date)
        if cash is None:
            cash_total, cash_incomplete = _ZERO, False  # date precedes first cash line
        else:
            cash_total, cash_incomplete = cash.reporting_total, cash.incomplete
        net_worth = None if cash_incomplete else point.total_value + cash_total
        new_points.append(point.model_copy(update={"net_worth": net_worth}))
    return trend.model_copy(update={"points": new_points})
