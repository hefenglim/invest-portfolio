"""Benchmark counterfactual (R4 / AI-D43): 「同一筆錢、同樣的日期，買指數會是多少？」

The review named this the highest-value missing capability, and the reason is simple: every
return figure in this app is unanchored without it. XIRR 15% is excellent or dismal depending
entirely on what the market did over the same period, and nothing here answered that.

**What this computes.** Take the portfolio's own reporting-currency flow stream — the SAME
one :mod:`portfolio.timeseries` builds for ``net_invested``, each flow already converted at its
own trade-date FX — and spend each flow on that market's benchmark index at that day's close
instead. Value the resulting units at the last close. The difference against the portfolio's
own result is the excess.

**Scope: lifetime, one number** (owner ruling 2026-08-26). The windowed comparison is already
answered by :func:`portfolio.twr.build_overlay`; a second, differently-scoped counterfactual
would be two answers to one question — the AI-D2 defect class. A windowed version would also
need a 「what is the opening capital at the window start」 convention (cost? market value? they
differ) that nothing else in the app needs.

⚠ **Compare against B, never against A.** ``ReturnSummary.total_return`` (A) applies FX to each
currency's GAIN and never to its principal (AI-D41). This counterfactual buys its units with
reporting-currency money converted at each flow's own trade-date rate, exactly as
``trend.net_invested`` does — so only **B** (``total_value − net_invested``) is measured on the
same ruler. Comparing the counterfactual to A would silently contrast two different treatments
of the principal's FX and attribute the difference to the market.

**Honesty guards** (they are the point of the module, not decorations):

* A market with no benchmark in the registry — MY today (AI-D22) — is **named** in
  ``uncovered_markets``, and its money still counts in ``uncovered_ratio``. Dropping it would
  silently compare a three-market portfolio against a two-market counterfactual and call the
  difference skill.
* A flow that predates the benchmark's first stored close, or lands on a non-positive close,
  is **uncovered** rather than skipped, for the same reason.
* ``uncovered_ratio > 0`` means the headline is about only part of the money. The caller must
  degrade the label accordingly — the same discipline as ``covered_ratio`` (F2) in the FX pool.
* No flows, or every flow uncovered, is ``available=False`` **with a reason** — never a zero,
  which would read as 「the index went nowhere」 rather than 「there was nothing to compare」.

Pure: stdlib + ``shared`` only, no connection, ``Decimal`` throughout. The caller supplies the
benchmark closes ALREADY in the reporting currency (via ``twr.convert_closes`` — the AI-D43
落點) and already split-re-expressed via ``price_basis.series_in``.
"""

from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from portfolio_dash.shared.enums import Market

_ZERO = Decimal("0")


@dataclass(frozen=True)
class ReportingFlow:
    """One net-invested flow, already in the reporting currency at its own trade-date FX.

    ``amount`` is positive for money going in (opening cost, buy incl. fees+tax) and negative
    for money coming back (sale net proceeds, cash dividend) — the ``timeseries`` sign
    convention, unchanged, because the whole comparison rests on both sides consuming the
    identical stream.
    """

    on: date
    market: Market
    amount: Decimal
    #: The instrument this flow belongs to, so a per-symbol drawer can filter the stream
    #: without rebuilding it. Unused by the portfolio-level counterfactual.
    symbol: str = ""


@dataclass(frozen=True)
class MarketLeg:
    """One market's share of the counterfactual, so the UI can show where it came from."""

    market: Market
    units: Decimal
    terminal_value: Decimal
    net_invested: Decimal
    last_close_on: date


@dataclass(frozen=True)
class Counterfactual:
    """The lifetime answer, or an honest refusal to give one."""

    available: bool
    reason: str | None
    terminal_value: Decimal | None
    net_invested: Decimal | None
    #: ``terminal_value − net_invested`` — comparable to B (含匯兌總損益), never to A.
    benchmark_return: Decimal | None
    uncovered_markets: tuple[str, ...]
    #: |uncovered flow| / |all flow|, by gross magnitude. ``1`` when nothing could be placed.
    uncovered_ratio: Decimal | None
    by_market: tuple[MarketLeg, ...]


_NO_FLOWS = "尚無投入流量，無法計算基準對照"
_NONE_PLACEABLE = "所有投入流量都落在基準指數的歷史之外或缺少基準，無法計算基準對照"


def _close_at_or_before(
    closes: Sequence[tuple[date, Decimal]], on: date
) -> Decimal | None:
    """The latest close at-or-before ``on``; ``None`` when the series starts later.

    Returns ``None`` for a non-positive close as well: a zero or negative index level is a
    data defect, and it can be neither a denominator (units bought) nor an honest valuation.
    """
    i = bisect_right([d for d, _ in closes], on)
    if i == 0:
        return None
    value = closes[i - 1][1]
    return value if value > _ZERO else None


def counterfactual(
    flows: Sequence[ReportingFlow],
    closes_by_market: Mapping[Market, Sequence[tuple[date, Decimal]]],
) -> Counterfactual:
    """Spend ``flows`` on each market's benchmark instead, and value the result today.

    ``closes_by_market`` holds each market's benchmark closes, ascending, **already in the
    reporting currency**. A market absent from the mapping has no benchmark and its flows are
    reported as uncovered rather than dropped.
    """
    gross_all = sum((abs(f.amount) for f in flows), _ZERO)
    if not flows:
        return Counterfactual(
            available=False, reason=_NO_FLOWS, terminal_value=None, net_invested=None,
            benchmark_return=None, uncovered_markets=(), uncovered_ratio=None, by_market=(),
        )

    units: dict[Market, Decimal] = {}
    invested: dict[Market, Decimal] = {}
    uncovered_gross = _ZERO
    uncovered: set[str] = set()

    for flow in flows:
        closes = closes_by_market.get(flow.market)
        level = None if not closes else _close_at_or_before(closes, flow.on)
        if level is None:
            # No benchmark for this market, or the flow predates its history, or the close
            # on that date is not a usable level. Named, never silently omitted.
            uncovered_gross += abs(flow.amount)
            uncovered.add(flow.market.value)
            continue
        units[flow.market] = units.get(flow.market, _ZERO) + flow.amount / level
        invested[flow.market] = invested.get(flow.market, _ZERO) + flow.amount

    ratio = (uncovered_gross / gross_all) if gross_all > _ZERO else _ZERO

    legs: list[MarketLeg] = []
    for market in sorted(units, key=lambda m: m.value):
        closes = closes_by_market[market]
        last_on, last_level = closes[-1]
        legs.append(MarketLeg(
            market=market,
            units=units[market],
            terminal_value=units[market] * last_level,
            net_invested=invested[market],
            last_close_on=last_on,
        ))

    if not legs:
        return Counterfactual(
            available=False, reason=_NONE_PLACEABLE, terminal_value=None, net_invested=None,
            benchmark_return=None, uncovered_markets=tuple(sorted(uncovered)),
            uncovered_ratio=ratio, by_market=(),
        )

    terminal = sum((leg.terminal_value for leg in legs), _ZERO)
    net = sum((leg.net_invested for leg in legs), _ZERO)
    return Counterfactual(
        available=True, reason=None, terminal_value=terminal, net_invested=net,
        benchmark_return=terminal - net,
        uncovered_markets=tuple(sorted(uncovered)), uncovered_ratio=ratio,
        by_market=tuple(legs),
    )


__all__ = ["Counterfactual", "MarketLeg", "ReportingFlow", "counterfactual"]
