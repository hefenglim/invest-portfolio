"""The orchestration combiner: assemble one complete DashboardData from SQLite.

Read-only — never fetches. Reads ledgers (data_ingestion), prices/FX (pricing),
calls the calculation cores (portfolio, forex), and assembles the dashboard
contract. Degrades honestly: blended figures become None with freshness reasons;
it never fabricates and never raises on missing market data.

This module introduces the one-way edge ``portfolio -> forex`` (recorded in the
2026-06-10 dashboard-combiner spec); forex imports only ``shared``, so no cycle.
"""

import json
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from itertools import chain, islice
from typing import Protocol

from portfolio_dash.data_ingestion.store import (
    list_accounts,
    list_cash_movements,
    list_dividends,
    list_fx_conversions,
    list_transactions,
    load_ledger_bundle,
)
from portfolio_dash.forex.fx_pnl import compute_fx_summary
from portfolio_dash.forex.results import FXSummary
from portfolio_dash.portfolio import settlement_store
from portfolio_dash.portfolio.allocation import combined_view, sector_allocation
from portfolio_dash.portfolio.benchmark_counterfactual import (
    BenchState,
    Counterfactual,
    ReportingFlow,
    counterfactual,
    finish,
)
from portfolio_dash.portfolio.cash import cash_balances
from portfolio_dash.portfolio.cost_basis import UnbookableLedgerError, build_book, iter_books
from portfolio_dash.portfolio.dashboard_models import (
    BenchmarkComparison,
    BenchmarkMarketLeg,
    DashboardData,
    DividendProjection,
    DividendSummary,
    DividendYearRow,
    ExDividendItem,
    FreshnessReport,
    FxFreshness,
    FxTriangle,
    HistoryState,
    HoldingRow,
    HoldingSubtotal,
    KpiSummary,
    OptionsBlock,
    PriceFreshness,
    TrendPoint,
    TrendSeries,
    UnconvertibleFlow,
    trend_pnl,
    with_trend_pnl,
)
from portfolio_dash.portfolio.dividends import project_dividends
from portfolio_dash.portfolio.networth import compose_net_worth, daily_cash_series
from portfolio_dash.portfolio.options import (
    LastTrade,
    has_open_option,
    last_trade_prices,
    ledger_has_options,
    option_summary,
    option_symbols,
    split_holdings,
    stock_realized,
)
from portfolio_dash.portfolio.pnl import value_holdings
from portfolio_dash.portfolio.price_basis import price_in
from portfolio_dash.portfolio.results import (
    Book,
    CombinedView,
    Holding,
    OversoldPosition,
    RefusedClose,
    ReturnSummary,
    SectorAllocation,
    UnappliedAction,
    UnbookableDividend,
)
from portfolio_dash.portfolio.returns import (
    CashMovementFlow,
    DividendIdentity,
    booked_dividends,
    dividend_key,
    total_return,
    xirr_reporting,
)
from portfolio_dash.portfolio.timeseries import (
    FxHistory,
    PriceHistory,
    build_reporting_flows,
    first_unconvertible_flow,
    trading_financing_cost,
    trend_book_days,
    trend_fx_currencies,
    trend_start,
    trend_symbols,
    trend_unavailable_reason,
    value_series,
)
from portfolio_dash.portfolio.twr import convert_closes
from portfolio_dash.pricing.benchmarks import benchmark_for_market
from portfolio_dash.pricing.results import FxRead, PriceRead
from portfolio_dash.pricing.store import (
    FxSeriesCache,
    get_close_series,
    get_dividend_events,
    get_latest_price,
    get_price_on_or_before,
)
from portfolio_dash.shared import flags, result_cache
from portfolio_dash.shared.account_ref import account_ref
from portfolio_dash.shared.corporate_actions import ActionIndex
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.fx import convert
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import DividendType
from portfolio_dash.shared.models.ledger import (
    Dividend,
    FXConversion,
    LedgerBundle,
    valued_rows_as_of,
)
from portfolio_dash.shared.options import contract_name
from portfolio_dash.shared.sectors import canonical_sector

_ZERO = Decimal("0")
_ONE = Decimal("1")
# History reads start here: stored prices may predate the first ledger event.
_EPOCH = date(1900, 1, 1)

# XIRR annualizes, so the exponent is 365/window: on a window of N days a book gain of
# +131.7% reports 2,749,353% at N=30, 3.3e11% at N=14 and 1.5e133 at N=1. That is the
# arithmetic working correctly on a degenerate input, but it is not a return rate anyone
# can read — and a 136-digit value also pushed the dashboard 1,915px sideways (found on a
# freshly reset instance carrying exactly one same-week trade, 2026-08-05).
# Owner ruling 2026-08-05: below this window the annualized figure is NOT shown; the KPI
# band still carries `total_return_rate`, which is the same information without the
# annualization, so nothing is hidden — only the misleading extrapolation is withheld.
# 30-365 days keeps the existing 短窗參考 badge.
_XIRR_MIN_WINDOW_DAYS = 30


class PriceSource(Protocol):
    """Where a valuation reads a held stock's price (PERF spec §11.2)."""

    def price(self, symbol: str) -> PriceRead | None:
        """The price a holding is valued at, ``stale`` against the valuation clock."""
        ...


class FxSource(Protocol):
    """Where a valuation reads FX (PERF spec §11.2): the "current" rate, the rate on or
    before a flow's day (XIRR), and a pair's stored rows (the triangulation)."""

    def latest(self, base: Currency, quote: Currency) -> FxRead | None:
        """The current rate of *base*/*quote* as stored — direct pair only, no inversion."""
        ...

    def on_or_before(self, base: Currency, quote: Currency, on: date) -> FxRead | None:
        """The stored rate dated on or before *on* (``get_fx_on``)."""
        ...

    def history(self, base: Currency, quote: Currency, start: date, end: date
                ) -> list[FxRead]:
        """The stored rates dated in ``[start, end]`` (``get_fx_history``)."""
        ...


class LatestStored:
    """Today's sources: :func:`get_latest_price` and :func:`get_fx` — the most recent stored
    row, whatever its date, ``stale`` against *now* — and FX through one
    :class:`~portfolio_dash.pricing.store.FxSeriesCache` (one query per directed pair)."""

    def __init__(self, conn: sqlite3.Connection, *, now: datetime,
                 fx_cache: FxSeriesCache | None = None) -> None:
        self._conn = conn
        self._now = now
        self.fx_cache = fx_cache if fx_cache is not None else FxSeriesCache(conn)

    def price(self, symbol: str) -> PriceRead | None:
        return get_latest_price(self._conn, symbol, now=self._now)

    def latest(self, base: Currency, quote: Currency) -> FxRead | None:
        return self.fx_cache.latest(base, quote, now=self._now)

    def on_or_before(self, base: Currency, quote: Currency, on: date) -> FxRead | None:
        return self.fx_cache.on_or_before(base, quote, on)

    def history(self, base: Currency, quote: Currency, start: date, end: date
                ) -> list[FxRead]:
        return self.fx_cache.history(base, quote, start, end)


class OnOrBefore:
    """The sources of a valuation AS AT day D (PERF spec §11.2, PERF-S7): the latest stored
    row dated on or before D, ``stale`` against D — what :class:`LatestStored` answers on a
    database holding no price or FX row after D, at a clock on D. A rate read for a later day
    is the rate at D (nothing after D exists in this world)."""

    def __init__(self, conn: sqlite3.Connection, day: date, *,
                 fx_cache: FxSeriesCache | None = None) -> None:
        self._conn = conn
        self.day = day
        self.fx_cache = fx_cache if fx_cache is not None else FxSeriesCache(conn)

    def price(self, symbol: str) -> PriceRead | None:
        return get_price_on_or_before(self._conn, symbol, on=self.day)

    def latest(self, base: Currency, quote: Currency) -> FxRead | None:
        return self.fx_cache.latest_as_of(base, quote, self.day)

    def on_or_before(self, base: Currency, quote: Currency, on: date) -> FxRead | None:
        return self.fx_cache.on_or_before(base, quote, min(on, self.day))

    def history(self, base: Currency, quote: Currency, start: date, end: date
                ) -> list[FxRead]:
        return self.fx_cache.history(base, quote, start, min(end, self.day))


class RateResolver:
    """Current-FX lookup: identity -> direct pair -> inverted pair -> KeyError.

    Records every requested pair (found or not) for the freshness report. Reads its rates
    from *fx* — :class:`LatestStored` over *conn* unless another source is given (a period
    close passes :class:`OnOrBefore`).
    """

    def __init__(self, conn: sqlite3.Connection | None = None, *, now: datetime | None = None,
                 fx: FxSource | None = None) -> None:
        if fx is None:
            if conn is None or now is None:
                raise TypeError("RateResolver needs a connection and a clock, or a source")
            fx = LatestStored(conn, now=now)
        self._fx: FxSource = fx
        self.reads: dict[tuple[Currency, Currency], FxRead | None] = {}
        # Pairs `_read` answered from the INVERSE stored pair — so `history` reads the series
        # in the same direction the rate was resolved (one owner of the direction rule).
        self._inverted: set[tuple[Currency, Currency]] = set()

    def _read(self, base: Currency, quote: Currency) -> FxRead | None:
        direct = self._fx.latest(base, quote)
        if direct is not None:
            return direct
        inverse = self._fx.latest(quote, base)
        if inverse is not None:
            self._inverted.add((base, quote))
            return FxRead(rate=_ONE / inverse.rate, as_of=inverse.as_of,
                          source=inverse.source, stale=inverse.stale)
        return None

    def history(self, base: Currency, quote: Currency, start: date, end: date
                ) -> list[FxRead]:
        """The stored series of *base*/*quote* in ``[start, end]``, ascending, read in the
        SAME direction :meth:`rate` resolved the pair (direct, else the inverted inverse).

        DEF-077: the triangulation compares its three legs on a common DATE, so it needs
        each leg's rows, not only the latest one — and a leg answered from the inverse pair
        must be inverted exactly as `_read` inverts it (``1 / rate``, full precision). A
        zero stored rate is skipped (a rate of 0 is not a rate).
        """
        if (base, quote) not in self._inverted:
            return self._fx.history(base, quote, start, end)
        return [
            FxRead(rate=_ONE / r.rate, as_of=r.as_of, source=r.source, stale=False)
            for r in self._fx.history(quote, base, start, end)
            if r.rate != _ZERO
        ]

    def rate(self, base: Currency, quote: Currency) -> Decimal:
        if base == quote:
            return _ONE
        key = (base, quote)
        if key not in self.reads:
            self.reads[key] = self._read(base, quote)
        read = self.reads[key]
        if read is None:
            # Surfaces VERBATIM as freshness.xirr_unavailable_reason, which the XIRR KPI
            # badge renders (web/app.js) — so it is a user-facing string, not log text.
            raise KeyError(f"尚無 {base.value}/{quote.value} 匯率資料")
        return read.rate


# M1 — triangular-consistency disclosure over the stored FX rates.
#
# Display scales. The RATES are never touched: these quantize the two DERIVED figures so
# the wire carries a readable number instead of a 28-significant-digit division tail.
# 6 dp matches the FX rate cap in data-and-pricing.md (a rate is not money; the 2-dp rule
# never applies), and 4 dp on a PERCENT is 6 dp on the underlying ratio — the same
# resolution as the legs it is derived from, so the gap can never be "more precise" than
# its own inputs.
_TRI_RATE_DP = Decimal("0.000001")
_TRI_GAP_DP = Decimal("0.0001")
# Tolerance in PERCENT. 0.05% is roughly a retail FX spread: below it the disagreement is
# indistinguishable from two providers quoting a moment apart, above it the reporting total
# visibly depends on which conversion path was used (the measured demo gap is 0.0690%).
_TRI_TOLERANCE_PCT = Decimal("0.05")
# DEF-077: how far before the oldest leg's latest date the common-date search looks. FX legs
# are written on every weekday a provider answers and MYR/TWD is derived on every day both USD
# legs exist (`pricing/cross.py`), so a gap of more than a few days means a leg stopped
# updating — a month is far past any holiday run, and past it there is nothing honest to
# compare (the triangle reads 「無法比較」 and the freshness list already flags the stale leg).
_TRI_LOOKBACK_DAYS = 30
_FxHistory = Callable[[Currency, Currency, date, date], list[FxRead]]


def _fx_triangulation(
    reads: dict[tuple[Currency, Currency], FxRead | None],
    *,
    history: _FxHistory,
    valued_on: date,
) -> list[FxTriangle]:
    """Every triangle closable from the pairs actually READ this request.

    Generic over ``reads`` on purpose — in practice the only triple stored today is
    USD/TWD · USD/MYR · MYR/TWD, but a fourth currency must not need a code change to be
    checked. For each directly-read pair ``(a, c)`` and each other currency ``b`` whose two
    legs ``(a, b)`` and ``(b, c)`` were ALSO read, the two-leg product is compared with the
    direct rate.

    Presence is the only gate: a stale leg still produces a triangle (flagged ``stale``)
    because a stale rate is exactly when a path disagreement is most likely, and hiding the
    check there would blind the reader at the worst moment. A MISSING leg produces nothing —
    the resolver's inverse fallback already means a read exists for either direction, so an
    absent key means the pair was genuinely never asked for, not that it is unknown.

    **Judged on ONE day (DEF-077, owner ruling ⑥ A, 2026-09-26).** The verdict compares the
    three legs on their latest COMMON date not after *valued_on* — when every leg's latest
    read is that day, the reads themselves; otherwise the rows *history* returns (the
    resolver's :meth:`RateResolver.history`, same direction as the read) within
    ``_TRI_LOOKBACK_DAYS`` before the oldest leg's latest date. It used to compare latest row
    against latest row, so a refresh that had moved one leg to a new day measured the day's
    market move and reported it as ``ok: false`` (verifier J-01, 3be67db: −0.0938%).
    Legs whose latest dates differ carry ``dates_differ`` + ``leg_dates``; no common date in
    the window → ``ok: None`` with a reason, never ``False``. The tolerance is unchanged:
    MYR/TWD is derived from the two USD legs (`pricing/cross.py`), so on a common date the
    gap is ~0 within the 6-dp cap and anything past 0.05% is still a row that reached
    ``fx_rates`` by another path — the guard semantics of ``data-and-pricing.md``.

    Reads the resolver's recorded rates (and, when the legs' dates differ, their stored
    history) and mutates nothing.
    """
    have = {k: v for k, v in reads.items() if v is not None}
    # Every currency that appears on either side of a read — the candidate pivots.
    pivots = sorted({c for pair in have for c in pair}, key=lambda x: x.value)
    out: list[FxTriangle] = []
    for (a, c), direct_read in sorted(have.items(), key=lambda kv: (kv[0][0].value,
                                                                   kv[0][1].value)):
        if a == c:
            continue
        for b in pivots:
            if b in (a, c):
                continue
            leg1, leg2 = have.get((a, b)), have.get((b, c))
            if leg1 is None or leg2 is None:
                continue
            if direct_read.rate == _ZERO:  # never divide by a zero rate — 0 is not a rate
                continue
            legs = (((a, b), leg1), ((b, c), leg2), ((a, c), direct_read))
            latest = {r.as_of for _, r in legs}
            base = FxTriangle(
                via=f"{a.value}/{b.value} × {b.value}/{c.value}",
                pair=f"{a.value}/{c.value}",
                implied=None, direct=None, gap_pct=None, ok=None,
                as_of=min(latest),
                stale=leg1.stale or leg2.stale or direct_read.stale,
                dates_differ=len(latest) > 1,
                leg_dates={f"{x.value}/{y.value}": r.as_of for (x, y), r in legs},
            )
            common = _common_rates(legs, history=history, valued_on=valued_on)
            if common is None:
                out.append(base.model_copy(update={"reason": (
                    f"最近 {_TRI_LOOKBACK_DAYS} 天內三組匯率沒有共同日期，無法比較")}))
                continue
            on, (r1, r2, r3) = common
            if r3 == _ZERO:
                continue
            implied = r1 * r2
            gap_pct = ((implied / r3) - _ONE) * Decimal("100")
            gap_q = gap_pct.quantize(_TRI_GAP_DP, rounding=ROUND_HALF_UP)
            out.append(base.model_copy(update={
                "implied": implied.quantize(_TRI_RATE_DP, rounding=ROUND_HALF_UP),
                "direct": r3.quantize(_TRI_RATE_DP, rounding=ROUND_HALF_UP),
                "gap_pct": gap_q,
                # Judged on the QUANTIZED gap: the badge and the number printed beside it
                # must never disagree, which they can if ok reads full precision.
                "ok": abs(gap_q) <= _TRI_TOLERANCE_PCT,
                "compared_on": on,
            }))
    return out


def _common_rates(
    legs: tuple[tuple[tuple[Currency, Currency], FxRead], ...],
    *,
    history: _FxHistory,
    valued_on: date,
) -> tuple[date, tuple[Decimal, Decimal, Decimal]] | None:
    """The latest date not after *valued_on* on which all three legs have a stored rate,
    with that day's three rates (leg1, leg2, direct) — or None (DEF-077).

    When every leg's latest read already falls on one such day, the reads are the answer
    (the common case: no extra query). Otherwise each leg's series is read back over
    ``_TRI_LOOKBACK_DAYS`` before the oldest latest date — a common date cannot be later
    than that — in the direction the rate was resolved.
    """
    latest = {r.as_of for _, r in legs}
    first = next(iter(latest))
    if len(latest) == 1 and first <= valued_on:
        return first, (legs[0][1].rate, legs[1][1].rate, legs[2][1].rate)
    end = min(min(latest), valued_on)
    start = end - timedelta(days=_TRI_LOOKBACK_DAYS)
    series = [{row.as_of: row.rate for row in history(x, y, start, end)} for (x, y), _ in legs]
    days = set(series[0]) & set(series[1]) & set(series[2])
    if not days:
        return None
    day = max(days)
    return day, (series[0][day], series[1][day], series[2][day])


# Fixed market order for the per-market subtotal rows (deterministic wire order).
_MARKET_ORDER = (Market.TW, Market.US, Market.MY)
_SubtotalKey = tuple[str | None, Market | None]


def _holdings_subtotals(
    holding_rows: list[HoldingRow],
    fx_rate: Callable[[Currency, Currency], Decimal],
    reporting: Currency,
    *,
    value_available: bool,
    unrealized_available: bool,
) -> list[HoldingSubtotal]:
    """Re-aggregate the per-holding reporting-currency market value + unrealized P&L into
    the (account, market) filter cells the dashboard 合計 footer and the filtered
    CSV/report select. This is a REGROUPING of the SAME per-holding values that feed
    ``kpis`` — not a new money formula (see ``HoldingSubtotal``).

    Every cell excludes 缺價 (``market_value is None``) holdings exactly as the KPI does
    (oversold positions carry a null market value, so they drop out here too). Because
    ``convert`` is pure ``amount * rate`` with no rounding, the grand ``(None, None)``
    cell reproduces ``KpiSummary.total_market_value`` / ``.unrealized_total`` exactly.

    Degradation mirrors the KPI's all-or-nothing FX rule: ``value_available`` /
    ``unrealized_available`` reflect whether the reporting-currency blend could be formed
    (``combined_view`` / ``total_return`` did not raise). When a blend is unavailable the
    corresponding figure is ``None`` on every cell — never a partial or fabricated number.
    When it IS available, every held currency has a resolvable rate, so no conversion here
    can raise.
    """
    mv_sum: dict[_SubtotalKey, Decimal] = defaultdict(lambda: Decimal("0"))
    ur_sum: dict[_SubtotalKey, Decimal] = defaultdict(lambda: Decimal("0"))
    seen_accounts: list[str] = []
    seen_markets: set[Market] = set()
    seen_cells: list[tuple[str, Market]] = []

    for h in holding_rows:
        acct, mkt = h.account_id, h.market
        if acct not in seen_accounts:
            seen_accounts.append(acct)
        seen_markets.add(mkt)
        if (acct, mkt) not in seen_cells:
            seen_cells.append((acct, mkt))
        # The four buckets this holding contributes to: grand, its account, its market,
        # and its (account, market) cell. The grand bucket is accumulated in holding-list
        # order — identical to combined_view / total_return — so it matches the KPI byte
        # for byte on total_market_value.
        keys: tuple[_SubtotalKey, ...] = (
            (None, None), (acct, None), (None, mkt), (acct, mkt),
        )
        if value_available and h.market_value is not None:
            rep_mv = convert(h.market_value, fx_rate(h.quote_ccy, reporting))
            for k in keys:
                mv_sum[k] += rep_mv
        if unrealized_available and h.unrealized_pnl is not None:
            rep_ur = convert(h.unrealized_pnl, fx_rate(h.quote_ccy, reporting))
            for k in keys:
                ur_sum[k] += rep_ur

    def cell(key: _SubtotalKey) -> HoldingSubtotal:
        acct, mkt = key
        return HoldingSubtotal(
            account_id=acct,
            market=mkt,
            total_market_value=(mv_sum[key] if value_available else None),
            unrealized_total=(ur_sum[key] if unrealized_available else None),
        )

    # Deterministic emission: grand, per-account (first-seen order), per-market (fixed
    # TW/US/MY among those present), per (account, market) cell (first-seen order).
    out: list[HoldingSubtotal] = [cell((None, None))]
    out.extend(cell((acct, None)) for acct in seen_accounts)
    out.extend(cell((None, mkt)) for mkt in _MARKET_ORDER if mkt in seen_markets)
    out.extend(cell(c) for c in seen_cells)
    return out


# How many rows an XIRR reason names individually before it summarizes.
_UNAPPLIED_ACTIONS_NAMED = 3


def _named_capped[T](items: Sequence[T], name: Callable[[T], str]) -> str:
    """「A（…）、B（…）、C（…），另有 N 筆」 — the ONE capped list every XIRR reason names its rows
    with (R11, 2026-10-04: three reasons carried three copies of it).

    Capped rather than unbounded: the string renders inside a KPI badge, and a 20-row ledger
    problem would push the dashboard sideways (the same overflow class as the 136-digit XIRR,
    2026-08-05). The remainder is counted, never dropped silently.
    """
    named = "、".join(name(i) for i in items[:_UNAPPLIED_ACTIONS_NAMED])
    rest = len(items) - _UNAPPLIED_ACTIONS_NAMED
    return named + (f"，另有 {rest} 筆" if rest > 0 else "")


def _unapplied_action_reason(unapplied: list[UnappliedAction]) -> str:
    """The XIRR badge's zh explanation when the book carries unapplied corporate actions.

    Names the ACCOUNT, SYMBOL and DATE of each one (owner requirement 2026-08-10). XIRR is
    the single figure a skipped action blanks portfolio-wide, so the message has to point at
    the one ledger row responsible instead of leaving the owner to hunt for it across a
    multi-account book. The 賣超 reason said only that *a* position was 待釐清 until CR-01
    (2026-10-04) gave it this same shape (:func:`_oversold_reason`). Capped by
    :func:`_named_capped`.
    """
    # The account is a TOKEN the fetch layer resolves to the zh display name (DEF-023):
    # this string used to carry the bare id, so the badge read 「2884（tw_broker・…）」.
    named = _named_capped(
        unapplied,
        lambda a: f"{a.from_symbol}（{account_ref(a.account_id)}・{a.date.isoformat()}）")
    return (
        f"帳本中有 {len(unapplied)} 筆公司行動無法套用（待釐清）：{named}"
        " — 這些部位的股數仍是行動前的基準，與行動後的現價不同步，無法計算 XIRR"
    )


def _oversold_reason(positions: list[OversoldPosition]) -> str:
    """The XIRR badge's zh explanation when the book carries 賣超 positions (CR-01, 2026-10-04).

    It said only 「帳本中有賣超部位待釐清」, which was enough while every 賣超 had a holdings row
    wearing the badge. One netted back to exactly zero shares has none — it is listed only on
    the BOOK — so the sentence now names each position the way the unapplied-action reason
    does: account (as a token the fetch layer resolves), symbol, the day the sell exceeded the
    holding, and 「部位已歸零」 when no holdings row will show it. Capped by
    :func:`_named_capped`.
    """
    def one(p: OversoldPosition) -> str:
        when = f"・{p.oversold_on.isoformat()}" if p.oversold_on is not None else ""
        flat = "，部位已歸零" if p.shares == _ZERO else ""
        return f"{p.symbol}（{account_ref(p.account_id)}{when}{flat}）"

    return (f"帳本中有賣超部位待釐清：{_named_capped(positions, one)}"
            " — 成本基礎已捨棄、該筆賣出沒有已實現損益，無法計算 XIRR")


def _orphan_dividend_reason(dividends: list[Dividend]) -> str:
    """The XIRR badge's zh explanation when the book refused dividends with no position (CR-02):
    account, symbol and the effective date of each row, capped by :func:`_named_capped`."""
    named = _named_capped(
        dividends,
        lambda d: f"{d.symbol}（{account_ref(d.account_id)}・{d.effective_date.isoformat()}）")
    return (f"帳本中有 {len(dividends)} 筆股利沒有對應持倉（待釐清）：{named}"
            " — 該帳戶在股利日之前沒有買進或期初庫存，股利未入帳，無法計算 XIRR")


def _refused_close_reason(closes: Sequence[RefusedClose]) -> str:
    """The XIRR badge's zh explanation when the book refused option close rows (options spec
    §4.4): the contract, account and date of each row, capped by :func:`_named_capped`.

    XIRR is withheld rather than computed without them for the orphan-dividend reason: a close
    the replay cannot book means the ledger is missing the trade it closes (or carries a close
    against the wrong side), so a figure over the rest is the return of a DIFFERENT ledger."""
    named = _named_capped(
        closes,
        lambda c: (f"{contract_name(c.symbol) or c.symbol}（{account_ref(c.account_id)}・"
                   f"{c.date.isoformat()}）"))
    return (f"帳本中有 {len(closes)} 筆選擇權到期作廢／被指派／履約列無法入帳（待釐清）：{named}"
            " — 該列沒有可結清的部位，無法計算 XIRR")


#: The cause word each ``UnbookableDividend.cause`` reads as inside a reason sentence.
_UNBOOKABLE_CAUSE_ZH = {"short": "放空中", "vacated": "已換股"}


def _unbookable_dividend_why(dividends: Sequence[UnbookableDividend]) -> str:
    """The FX-complete reason's rung for dividends refused on a position that existed (K3).

    Named per row — account, symbol, effective date and the cause — because nothing else on
    the dashboard names them: XIRR is not withheld for these (it excludes the payout and
    computes, review R2 ⑤), so when the carrier is flat this sentence is the dashboard's ONLY
    pointer to the row. Capped by :func:`_named_capped`.
    """
    named = _named_capped(
        dividends,
        lambda u: (f"{u.dividend.symbol}（{account_ref(u.dividend.account_id)}・"
                   f"{u.dividend.effective_date.isoformat()}・"
                   f"{_UNBOOKABLE_CAUSE_ZH[u.cause]}）"))
    return f"帳本中有 {len(dividends)} 筆股利無法入帳（待釐清）：{named}"


@dataclass(frozen=True)
class FxCompleteOutcome:
    """B of the A · B · B−A decomposition, plus WHY it is absent when it is.

    Same shape and same reason as ``XirrOutcome``: a figure that legitimately degrades needs
    to carry its own explanation, or the UI can only blank the row. Owner ruling 2026-08-25 —
    a line that silently vanishes teaches the reader nothing; 「—」 with a reason does.
    """

    value: Decimal | None
    reason: str | None


def fx_complete_return(
    trend: TrendSeries, *, has_oversold: bool, has_unapplied_action: bool,
    has_orphan_dividend: bool = False,
    unbookable_dividends: Sequence[UnbookableDividend] = (),
    refused_closes: Sequence[RefusedClose] = (),
) -> FxCompleteOutcome:
    """B of the A · B · B−A decomposition — the FX-COMPLETE lifetime result (AI-D41).

    ``total_return`` (A) translates each currency's NET P&L at today's spot, so the rate never
    reaches the PRINCIPAL. The trend converts every flow at its own trade-date rate, so
    ``total_value − net_invested`` on its last point is the figure that does — and it was
    being displayed under the label 浮動損益.

    Degrades with a stated reason whenever the trend cannot support it: unavailable/empty, or
    a last day the trend itself marks ``incomplete``. That last case does NOT fall back to an
    earlier complete day: a B measured on a different date than A makes B − A a
    plausible-looking wrong number, which is the whole failure mode this decomposition exists
    to prevent.
    """
    if not trend.available or not trend.points:
        return FxCompleteOutcome(None, "尚無趨勢資料，無法計算含匯兌損益")
    last = trend.points[-1]
    # Options spec §4.4: a refused close withholds B like XIRR — from the BOOK's record, so it
    # holds even if the trend's last day were somehow complete. Empty on a ledger without one.
    if last.incomplete or refused_closes:
        # WHICH kind of incomplete (F-13, 2026-08-27). `TrendPoint.incomplete` is set by five
        # different causes — a missing price, a 賣超 position, an unbookable dividend, an
        # unapplied corporate action, a whole-book action failure — and this reason named only
        # the first. Measured: an oversold TSLA that HAD a same-day close produced 「最新一日
        # 有標的缺價」, sending the reader off to check the price feed, while the XIRR row two
        # lines above, reading the same book, said 「帳本中有賣超部位待釐清」. Same ladder order
        # as the XIRR gate, so one book never yields two diagnoses.
        # CR-02 (2026-10-04) added the third rung, in the XIRR gate's own order. Defaulted to
        # False only because the cost of a missed argument is a wrong DIAGNOSIS on an already-
        # blank figure (B is withheld either way) — the one production caller passes it.
        # K3 (2026-10-05) added the fourth: a payout refused on an open short or a vacated
        # ticker. The trend marked those days (held carrier) yet this ladder had no rung for it
        # and fell through to 「缺價」 over a priced position — F-13's misdiagnosis, again. It
        # sits after the three XIRR rungs because XIRR itself is not withheld for it.
        why = ("帳本中有賣超部位待釐清" if has_oversold
               else "帳本中有公司行動無法套用（待釐清）" if has_unapplied_action
               else "帳本中有股利沒有對應持倉（待釐清）" if has_orphan_dividend
               else "帳本中有選擇權到期作廢／被指派／履約列無法入帳（待釐清）" if refused_closes
               else _unbookable_dividend_why(unbookable_dividends) if unbookable_dividends
               else "最新一日有標的缺價")
        return FxCompleteOutcome(
            None, f"{why}，含匯兌總損益暫不計算（不以較早日期替代，否則與"
                  "上方資產損益不同日，兩者的差額會失去意義）")
    return FxCompleteOutcome(last.total_value - last.net_invested, None)


_BENCH_EPOCH = date(1990, 1, 1)  # benchmark closes may predate the first ledger event


def bounded_closes(
    conn: sqlite3.Connection, symbol: str, boundary: date, end: date, *, floor: date,
) -> list[tuple[date, Decimal]]:
    """The closes a carry-forward reader of ``[boundary, end]`` needs, never one before *floor*.

    v0.1.31 read every close ``BETWEEN floor AND end`` (the trend and the performance route
    from 1900, the benchmark from 1990). The light read (PERF spec §8.5) starts at the last
    close on or before ``max(boundary, floor)`` instead — so a first day on a weekend, a
    holiday or in a suspension keeps its carried-forward value — and drops whatever predates
    *floor*. Every lookup on a day from *boundary* on therefore finds exactly the close the
    full v0.1.31 read gave it, including "none" for a day before the first close on or after
    *floor* (a 1988 flow is uncovered by a benchmark history stored from 1987, as before).
    """
    return [row for row in get_close_series(conn, symbol, max(boundary, floor), end)
            if row[0] >= floor]


def _benchmark_comparison(
    conn: sqlite3.Connection,
    bundle: LedgerBundle,
    fx_history: FxHistory,
    reporting: Currency,
    *,
    as_of: date,
    actions: ActionIndex,
    fx_complete: Decimal | None,
    cash_movements: Sequence[CashMovementFlow],
    refused_dividends: Iterable[DividendIdentity],
    stored: tuple[BenchState, date] | None = None,
) -> BenchmarkComparison:
    """R4 / AI-D43 — spend the portfolio's own flows on each market's index instead.

    Assembled HERE for the same reason ``fx_complete_return`` is: this is the only layer
    holding both the ledger flows and the reporting-currency figure they must be compared
    against. Reuses the request's ONE ``ActionIndex`` (trap #21) and the ALREADY-loaded
    ``fx_history`` — a benchmark's quote currency is the quote currency of the market whose
    flows it is standing in for, so the pair is loaded exactly when it is needed, and a
    genuinely missing pair drops those closes and degrades that market to uncovered rather
    than raising.

    ⚠ ``series_in`` is applied to the benchmark series for the same reason and with the same
    known limitation the performance router documents: nobody holds an index, so nobody
    records its splits, making the call a structural no-op today — wired anyway because it
    is correct the moment such an action IS recorded, and because one raw series read is how
    the next reader concludes the re-expression rule is optional.

    *stored* (PERF spec §4 "Decimal order", §10 step 5) is the settlement store's accumulated
    state and the day it was accumulated through (``settled_through``), handed in only while
    nothing is pending or claimed by a run and no corporate action on a benchmark storage key
    is dated after that day (:func:`_stored_bench`). It is used iff, in addition, no flow is
    dated after that day — then the flows spent are exactly the ones it accumulated, in the
    same order — and finished with TODAY's last close (:func:`_finish_stored`). Otherwise the
    counterfactual is computed live, in reference order.
    """
    # K2: the stream B is built from — a payout the replay refused is no flow on either leg.
    flows = build_reporting_flows(bundle, fx_history, reporting,
                                  cash_movements=cash_movements,
                                  refused_dividends=refused_dividends)
    if flows is None:
        return BenchmarkComparison(
            available=False,
            reason="部分投入流量缺少當日匯率，無法換算成報告幣，基準對照暫不可用",
        )
    finished = None
    if stored is not None and all(f.on <= stored[1] for f in flows):
        finished = _finish_stored(conn, stored[0], fx_history, reporting, as_of=as_of,
                                  actions=actions)
    if finished is not None:
        result, labels = finished
    else:
        result, labels = _live_counterfactual(conn, flows, fx_history, reporting,
                                              as_of=as_of, actions=actions)
    excess = (
        None if not result.available or fx_complete is None or result.benchmark_return is None
        else fx_complete - result.benchmark_return
    )
    return BenchmarkComparison(
        available=result.available,
        reason=result.reason,
        terminal_value=result.terminal_value,
        net_invested=result.net_invested,
        benchmark_return=result.benchmark_return,
        excess=excess,
        uncovered_markets=list(result.uncovered_markets),
        uncovered_ratio=result.uncovered_ratio,
        by_market=[
            BenchmarkMarketLeg(
                market=leg.market, label=labels[leg.market],
                terminal_value=leg.terminal_value, net_invested=leg.net_invested,
            )
            for leg in result.by_market
        ],
    )


def _live_counterfactual(
    conn: sqlite3.Connection, flows: Sequence[ReportingFlow], fx_history: FxHistory,
    reporting: Currency, *, as_of: date, actions: ActionIndex,
) -> tuple[Counterfactual, dict[Market, str]]:
    """The counterfactual over *flows*, every index level read live (v0.1.31's order)."""
    closes_by_market: dict[Market, list[tuple[date, Decimal]]] = {}
    labels: dict[Market, str] = {}
    # AI-D49: a cash-movement flow has no market and buys no index, so it contributes no
    # benchmark series to load. `counterfactual` excludes it from both legs; this loop must
    # not ask for 「the index of None」.
    # PERF spec §8.5: each market's closes are read from the last close on or before that
    # market's FIRST flow — the earliest day `counterfactual` looks a level up for — and
    # never from before 1990 (v0.1.31's floor, `bounded_closes`); its terminal value is the
    # last close, which the bounded read keeps too.
    first_flow: dict[Market, date] = {}
    for f in flows:
        if f.market is not None and (f.market not in first_flow or f.on < first_flow[f.market]):
            first_flow[f.market] = f.on
    for market in {f.market for f in flows if f.market is not None}:
        bench = benchmark_for_market(market)
        if bench is None:
            # No market is unmapped today (MY gained KLCI 2026-08-27), but a market
            # added later would land here — uncovered and NAMED, never dropped.
            continue
        raw = closes_in(
            actions, bench.storage_key,
            bounded_closes(conn, bench.storage_key, first_flow[market], as_of,
                           floor=_BENCH_EPOCH),
            valued_on=as_of,
        )
        converted = convert_closes(raw, fx_history, bench.quote_ccy, reporting)
        if converted:
            closes_by_market[market] = converted
            labels[market] = bench.label
    return counterfactual(flows, closes_by_market), labels


def _finish_stored(
    conn: sqlite3.Connection, state: BenchState, fx_history: FxHistory, reporting: Currency,
    *, as_of: date, actions: ActionIndex,
) -> tuple[Counterfactual, dict[Market, str]] | None:
    """Finish the store's accumulated *state* with TODAY's last close of each placed market
    (PERF spec §8.4: ``finish`` reads only each series' last element). Each close is read,
    re-expressed and converted exactly as :func:`_live_counterfactual` reads the last element
    of its series: the last close on or before *as_of* (never before 1990), in *as_of*'s
    share terms, at its own date's rate. ``None`` — compute live — when a placed market has no
    benchmark or no convertible close (a state the live path could not have produced)."""
    closes_by_market: dict[Market, list[tuple[date, Decimal]]] = {}
    labels: dict[Market, str] = {}
    for market in state.units:
        bench = benchmark_for_market(market)
        if bench is None:
            return None
        raw = closes_in(
            actions, bench.storage_key,
            bounded_closes(conn, bench.storage_key, as_of, as_of, floor=_BENCH_EPOCH),
            valued_on=as_of,
        )
        converted = convert_closes(raw, fx_history, bench.quote_ccy, reporting)
        if not converted:
            return None
        closes_by_market[market] = converted
        labels[market] = bench.label
    return finish(state, closes_by_market), labels


def _stored_bench(state: settlement_store.StoreState | None,
                  bundle: LedgerBundle) -> tuple[BenchState, date] | None:
    """The store's benchmark state and its day ``S``, under the conditions of PERF spec §10
    step 5 that do not need the flows (:func:`_benchmark_comparison` checks the last one — no
    flow dated after ``S``): a usable store, nothing pending and nothing claimed by a run
    (:attr:`~settlement_store.StoreState.claimed_day` is ∞: the active run's whole
    ``run_from`` counts here, where ``Dd`` counts only the dirty day it claimed), a state that
    was published (a partial publish stores none), and no
    corporate action on a benchmark storage key dated after ``S`` — ``series_in``
    re-expresses every level into the valuation day's terms, and ``0050`` is both a benchmark
    and a stock the owner can hold. *bundle* is the build's registered ledger cut at today
    (its actions are the ``ActionIndex`` the live read re-expresses through)."""
    if (state is None or state.claimed_day is not None or state.bench_state is None
            or state.settled_through is None):
        return None
    through = state.settled_through
    keys = set(settlement_store.default_benchmark_keys())
    if any(a.date > through and (a.from_symbol in keys or a.to_symbol in keys)
           for a in bundle.actions):
        return None
    try:
        return BenchState.from_json(json.loads(state.bench_state)), through
    except (ValueError, KeyError, TypeError):
        return None


def closes_in(
    index: ActionIndex,
    symbol: str,
    closes: list[tuple[date, Decimal]],
    *,
    valued_on: date,
) -> list[tuple[date, Decimal]]:
    """:func:`~portfolio_dash.portfolio.price_basis.series_in` for ``(date, close)`` pairs
    (``pricing.store.get_close_series``): every close in *valued_on*'s share terms through
    :func:`price_in`, and — the same structural short-circuit — the pairs untouched when no
    SPLIT is recorded on *symbol*."""
    if not index.splits_on(symbol):
        return closes
    return [(d, price_in(index, symbol, v, priced_on=d, valued_on=valued_on))
            for d, v in closes]


@dataclass(frozen=True)
class KpiBlock:
    """What :func:`kpis_as_of` answers (PERF spec §11.2) — its return set is fixed.

    * the KPI fields: KPI 總市值 through :meth:`market_value_with` (the stock grand subtotal
      ``stock_market_value`` plus the options block's own market value, all-or-nothing on FX),
      ``total_return`` (A) and its rate, the realized / unrealized split, ``xirr`` with its
      window and reason, ``by_currency_value``;
    * the rates used and the missing / stale facts: ``price_reads`` (each held stock's price
      read, ``None`` = missing; ``stale`` per row) and ``fx_reads`` (each pair the KPIs asked
      for, ``None`` = missing, with its date and ``stale``) — a snapshot taken when the KPIs
      were done, so the pairs a later step reads are not in it;
    * the intermediates ``_build_dashboard`` goes on with: the valued bundle, the book, the
      valuation, the returns, the currency view, the resolver (whose ``reads`` keep growing)
      and the cut cash movements.

    The 選擇權 block is assembled by :func:`options_block_of` from these intermediates — once —
    and KPI 總市值 is read from it, exactly as v0.1.31 read it.
    """

    as_of: date
    reporting: Currency
    total_return: Decimal | None
    total_return_rate: Decimal | None
    realized_total: Decimal | None
    unrealized_total: Decimal | None
    xirr: Decimal | None
    xirr_window_days: int | None
    xirr_reason: str | None
    by_currency_value: dict[Currency, Decimal] | None
    price_reads: dict[str, PriceRead | None]
    fx_reads: dict[tuple[Currency, Currency], FxRead | None]
    bundle: LedgerBundle
    has_options: bool
    option_symbols: frozenset[str]
    book: Book
    actions: ActionIndex
    price_map: dict[str, Decimal]
    last_trades: dict[str, LastTrade]
    valued: list[Holding]
    valued_stocks: list[Holding]
    returns: ReturnSummary | None
    view: CombinedView | None
    stock_market_value: Decimal | None
    resolver: RateResolver
    cash_movements: list[CashMovementFlow]

    def market_value_with(self, options: OptionsBlock | None) -> Decimal | None:
        """KPI 總市值 (OPT-D5): the stock grand subtotal plus *options*' own reporting market
        value — the block :func:`options_block_of` assembled, never a second computation. The
        addition is SKIPPED (not ``+ 0``) on a ledger without an option, and the figure
        degrades to ``None`` when either half cannot be formed (the all-or-nothing FX rule).

        Refuses the wrong block shape loudly: an options ledger without its block (the
        figure would silently drop every contract) or a block on a stock ledger.
        """
        if not self.has_options:
            if options is not None:
                raise ValueError("an options block for a ledger without an option")
            return self.stock_market_value
        if options is None:
            raise ValueError("KPI 總市值 of an options ledger needs its options block")
        option_mv = options.market_value.reporting
        if self.stock_market_value is None or option_mv is None:
            return None
        return self.stock_market_value + option_mv

    @property
    def held_symbols(self) -> list[str]:
        """The held stock symbols, sorted — the keys of ``price_reads``."""
        return list(self.price_reads)

    @property
    def missing_prices(self) -> list[str]:
        return [sym for sym, pr in self.price_reads.items() if pr is None]

    @property
    def missing_fx(self) -> list[str]:
        return [f"{b.value}/{q.value}" for (b, q), read in
                sorted(self.fx_reads.items(), key=lambda kv: (kv[0][0].value, kv[0][1].value))
                if read is None]


def options_block_of(
    kpi: KpiBlock, prices: PriceSource, *, trend_excludes_value: bool,
) -> OptionsBlock | None:
    """The 選擇權 block (options spec §7.1) from *kpi*'s ONE replay and ONE valuation, or
    ``None`` on a ledger without an option. The underlyings' prices come from *prices* — the
    same source the valuation read (:class:`LatestStored` live, :class:`OnOrBefore` for a
    period close); *trend_excludes_value* is the trend's own flag. KPI 總市值 reads this
    block's market value (:meth:`KpiBlock.market_value_with`)."""
    if not kpi.has_options:
        return None
    return option_summary(
        book=kpi.book, valued=kpi.valued, bundle=kpi.bundle, last_trades=kpi.last_trades,
        underlying_prices=_underlying_prices(prices, kpi.book, kpi.option_symbols,
                                             kpi.bundle.instruments, kpi.actions,
                                             as_of=kpi.as_of),
        fx_rate=kpi.resolver.rate, reporting=kpi.reporting, as_of=kpi.as_of,
        trend_excludes_value=trend_excludes_value)


def kpis_as_of(
    bundle: LedgerBundle,
    *,
    book: Book | None,
    cash_movements: Sequence[CashMovementFlow],
    prices: PriceSource,
    fx: FxSource,
    as_of: date,
    reporting: Currency,
) -> KpiBlock:
    """The KPI computation of the dashboard as at *as_of* (PERF spec §11.2): book, prices,
    valuation, returns, XIRR, the KPI blend and the currency view.

    The live dashboard calls it with :class:`LatestStored` (its payload is unchanged); a
    period close calls it with :class:`OnOrBefore` and a book its one replay already holds,
    so no replay runs per period end.

    * *bundle* — the ledger with every unregistered row dropped, NOT cut: whether the 選擇權
      block exists is decided on the whole ledger (options spec §7.1); the valuation cut
      ``bundle.valued_as_of(as_of)`` is taken here;
    * *book* — the dashboard-path replay of that cut (``iter_books`` yields it), or ``None``
      to replay here;
    * *cash_movements* — already cut at *as_of* (``valued_rows_as_of``, step 1);
    * *prices* / *fx* — where the held prices, the current rates and XIRR's per-flow rates
      come from.
    """
    # Options spec §7.1: the 選擇權 block exists iff the UNCUT ledger carries a trade or an
    # action on an option contract — decided BEFORE the valuation cut below, so a contract
    # traded after the valuation day still shows its (empty) section. On every other ledger
    # `has_options` is False and every option branch below is skipped outright (invariant 1:
    # no `+ Decimal(0)`, the stock path runs untouched).
    has_options = ledger_has_options(bundle)
    instruments = bundle.instruments
    # 1c. Every row counts from its OWN date (DEF-016 for dividends, widened to trades,
    # openings, FX conversions and corporate actions by DEF-056 — owner rulings 2026-09-24).
    # A confirmed payout is stored on its payment date and a trade may be entered ahead of
    # its date; replaying either now moved the holdings, 總報酬 and the adjusted cost, and
    # handed XIRR a flow dated after its own terminal value, while the cash pool and the
    # trend already ignored it. ONE cut, here, so the book, XIRR, the received-dividend
    # summary, the FX pool and the trend all read the same ledger — and every surface built
    # on this function (drawer, exports, alerts, insights, snapshots, rebalance) inherits it.
    bundle = bundle.valued_as_of(as_of)
    # Read-only local views of the (now filtered) bundle, for the steps below.
    txs, divs, opening = bundle.transactions, bundle.dividends, bundle.opening

    # 2. Book and valuation. allow_oversell: an acked oversell must not crash the
    # dashboard — it degrades to a flagged 賣超 holding (待釐清) instead (see build_book).
    if book is None:
        try:
            book = build_book(bundle, allow_oversell=True)
        except ArithmeticError as exc:
            # ⚠ NOT THE OWNER any more (2026-08-29). ``cost_basis.build_book`` re-types every
            # ``ArithmeticError`` out of the replay into an ``UnbookableLedgerError`` naming
            # the offending row, so this arm no longer fires for a ledger fault — go THERE to
            # change the behaviour or the wording. The re-typing moved because this was the
            # ONLY call site that had it: the same row that degraded here answered 500 on 重算,
            # the tax package and 試算, and fixing it at four sites is four chances to miss one.
            #
            # Kept, deliberately, as a floor rather than deleted: the guarantee this file owes
            # its caller is "``build_dashboard`` does not raise a raw decimal fault", and that
            # must hold even if the replay is swapped, patched or extended. It is pinned as a
            # CHANNEL by ``tests/portfolio/test_r8_decimal_fault_degrades.py``, which injects
            # the fault rather than provoking it, precisely so the guarantee survives its
            # current cause.
            #
            # The arm catches the CLASS, not one cause: ``decimal.DivisionByZero`` and
            # ``decimal.Overflow`` are SIBLINGS of ``InvalidOperation``, not subclasses (this
            # arm said ``InvalidOperation`` until wave 3 and both walked straight past it), and
            # ``ZeroDivisionError`` is in the class too.
            raise UnbookableLedgerError(
                "帳本中有一列的數值無法計算（例如股數為 0 或數值過大的交易），"
                "請於交易帳本修正該列後重試"
            ) from exc
    # Read off the BOOK, not off the holdings (CR-01 / CR-11, 2026-10-04): a 賣超 that a later
    # buy of the same size netted back to exactly zero shares has no holding to inspect —
    # `any(h.oversold ...)` was False there, and XIRR came out as a confident −67.8% over a
    # discarded basis (measured on the review's probe). `Book.oversold_positions` lists every
    # 賣超 position, so this is a strict superset of the old test.
    oversold_positions = book.oversold_positions
    # CR-02: dividends the replay refused because the account never held the symbol by their
    # date. No position carries a flag for them, so the book's list is the only record.
    orphan_dividends = book.orphan_dividends
    # Read off the BOOK, not off the holdings: two of the three ways an action goes
    # unapplied leave no holding to inspect — an EXCHANGE that already emptied the source
    # (the flag is dropped with its zero-share carrier), and a source that never existed
    # (nothing is flagged at all). `any(h.unbookable_action ...)` is False in both, and the
    # XIRR terminal value is wrong in all three. See Book.unapplied_actions (audit F-47/F-49).
    unapplied_actions = book.unapplied_actions
    # Options spec §5: a contract is never quoted, so it never enters `price_reads` — nor,
    # therefore, `freshness.prices`, `missing_prices` or `any_stale` — nor the ex-dividend
    # calendar below (OPT-D10). Empty without an option, so `held_symbols` is unchanged.
    opt_syms = option_symbols(instruments) if has_options else frozenset()
    held_symbols = sorted({h.symbol for h in book.holdings if h.symbol not in opt_syms})
    price_reads: dict[str, PriceRead | None] = {sym: prices.price(sym) for sym in held_symbols}
    # §5.1(d), applied HERE and not inside `value_holdings` (trap #3). A stored close is
    # as traded on its own date, but the book's share count is already in today's terms, so
    # a price carried forward across a split must be re-expressed — divided by the splits
    # in `(pr.as_of, as_of]` — before it meets those shares. `price_map` is the ONE dict fed
    # to BOTH `value_holdings` (below) and `xirr_reporting` (step 4), which multiplies
    # `price × h.shares` itself rather than reading `market_value`: correcting it inside
    # `value_holdings` would leave the XIRR terminal value on the raw price and make market
    # value and XIRR disagree by the whole ratio — two numbers on one screen, the worst
    # kind of discrepancy. One seam here gives valuation, XIRR, weights, KPIs and net worth
    # the same corrected price.
    #
    # ONE index for the request (trap #21). `price_in` short-circuits structurally on "no
    # SPLIT for this symbol", so an action-free ledger takes the untouched pre-existing
    # expression and this dict is byte-for-byte the old one (D38 invariant 1).
    actions = ActionIndex.build(bundle.actions)
    price_map = {
        sym: price_in(actions, sym, pr.value, priced_on=pr.as_of, valued_on=as_of)
        for sym, pr in price_reads.items() if pr is not None
    }
    # R2 (options spec §5): an open contract is valued at its last ordinary trade. It joins
    # `price_map` — the ONE dict both `value_holdings` and XIRR's terminal value read, so the
    # open liability is in both — and never `price_reads` (no freshness) nor `prices`.
    last_trades = (last_trade_prices(bundle, as_of, unapplied=book.unapplied_actions)
                   if has_options else {})
    for h in book.holdings:
        if h.symbol in opt_syms and h.symbol in last_trades:
            price_map[h.symbol] = last_trades[h.symbol].price
    valued = value_holdings(book.holdings, price_map)
    # OPT-D5: holdings, weights, allocation, subtotals, the ex-dividend projection and the
    # `realized` section are STOCK-ONLY, fed to the SAME functions as before; the KPI totals,
    # total return A, XIRR and the FX pool's exposure read every position. Without an option
    # the stock list IS `valued` — the same object, not an equal copy.
    valued_stocks = split_holdings(valued, opt_syms)[0] if has_options else valued

    resolver = RateResolver(fx=fx)

    # 3. Core summaries — each degrades to None on a missing current rate.
    returns: ReturnSummary | None
    try:
        returns = total_return(book, valued, resolver.rate, reporting)
    except KeyError:
        returns = None
    view: CombinedView | None
    try:
        view = combined_view(valued_stocks, resolver.rate, reporting)
    except KeyError:
        view = None

    # 4. XIRR — on-or-before trade-date FX; degrades to None with a reason.
    def fx_at(d: date, base: Currency, quote: Currency) -> Decimal:
        if base == quote:
            return _ONE
        direct = fx.on_or_before(base, quote, d)
        if direct is not None:
            return direct.rate
        inverse = fx.on_or_before(quote, base, d)
        if inverse is not None:
            return _ONE / inverse.rate
        # Same: rendered in the XIRR badge. Keeps the pair + the date, because those two
        # facts are what tell the owner which rate to backfill.
        raise KeyError(
            f"查無 {d.isoformat()}（含）之前的 {base.value}/{quote.value} 匯率"
        )

    xirr_value: Decimal | None = None
    xirr_window_days: int | None = None
    xirr_reason: str | None = None
    if oversold_positions:
        # An oversold (賣超) position has no honest terminal value -> XIRR is not computable.
        # Named per position since CR-01 (2026-10-04): a 賣超 netted back to zero shares has no
        # holdings row to find, so the reason is the one place that says which row broke.
        xirr_reason = _oversold_reason(oversold_positions)
    elif unapplied_actions:
        # A skipped corporate action is a DIFFERENT problem from 賣超 and says so: the shares
        # are positive and look ordinary, but they are in PRE-action terms while every price
        # is post-action, so `xirr_reporting` — which multiplies `price × h.shares` itself
        # rather than reading `market_value` — would build its terminal value out of the
        # wrong quantity and return a confident number (measured before this gate: 0.5301
        # with no reason given, off a terminal value 94% composed of one such row).
        #
        # BLAST RADIUS — deliberate, and the ONE place in this change that is not contained
        # to the affected symbol (owner requirement 2026-08-10). Everywhere else, one
        # unapplied action damages exactly one stock: `pnl.py` nulls that position's
        # market_value and every aggregate simply excludes it while still computing for the
        # rest. XIRR cannot work that way. It is a SINGLE number over a terminal value that
        # sums every holding, so one wrong share count makes the sum wrong — and dropping
        # the position instead would silently report the XIRR of a DIFFERENT portfolio,
        # which is the same wrong-number-that-looks-right failure by another route. Blanking
        # the whole figure is therefore correct (and matches the `has_oversold` precedent
        # directly above), but it is an accepted portfolio-wide cost, not a free one. The
        # reason string below carries the account, symbol and date precisely so the owner
        # can go straight to the one row that blanked it.
        xirr_reason = _unapplied_action_reason(unapplied_actions)
    elif orphan_dividends:
        # CR-02 (2026-10-04). A dividend with no position means the ledger is missing the buy
        # or opening that earned it. XIRR without both is the return of a DIFFERENT ledger — the
        # unapplied-action reasoning directly above — so the figure is withheld and the rows
        # are named, rather than computed with the payout silently left out.
        xirr_reason = _orphan_dividend_reason(orphan_dividends)
    elif book.refused_closes:
        # Options spec §4.4: a close row the replay refused — the record, like the orphan
        # dividends above (it may have no position to flag). Named per row.
        xirr_reason = _refused_close_reason(book.refused_closes)
    else:
        try:
            outcome = xirr_reporting(
                txs, divs, opening, valued, instruments, fx_at,
                price_map, resolver.rate, as_of, reporting,
                # The replay REFUSED these (a dividend on an open short); counting them
                # here is what gave one payment three answers on three screens.
                refused_dividends=book.refused_dividends,
                # AI-D42: the three trading/financing costs. `cash_movements` is already
                # loaded above for the pool/FX work — no extra read.
                cash_movements=cash_movements)
            xirr_value = outcome.rate
            xirr_window_days = outcome.window_days
            if (xirr_value is not None and xirr_window_days is not None
                    and xirr_window_days < _XIRR_MIN_WINDOW_DAYS):
                # Withhold the annualization, not the return: `total_return_rate` is
                # unaffected and still on the wire. `window_days` is reported either way.
                xirr_value = None
                xirr_reason = (f"觀察期 {xirr_window_days} 天・不足以年化"
                               f"（需 ≥{_XIRR_MIN_WINDOW_DAYS} 天）")
        except KeyError as exc:
            xirr_reason = str(exc).strip("'\"")
    if xirr_value is None and xirr_reason is None:
        xirr_reason = "無法計算（缺少現價、現金流無正負變化，或迭代未收斂）"

    # 10. KPIs — blended; None whenever the blend cannot be formed honestly.
    total_return_blended: Decimal | None = None
    total_return_rate: Decimal | None = None
    realized_total: Decimal | None = None
    unrealized_total: Decimal | None = None
    if returns is not None:
        total_return_blended = returns.reporting_total_return
        gross_rep = _ZERO
        realized_rep = _ZERO
        unrealized_rep = _ZERO
        for ccy, cr in returns.by_currency.items():
            rate = resolver.rate(ccy, reporting)  # cached: already resolved above
            gross_rep += convert(cr.gross_invested, rate)
            realized_rep += convert(cr.realized, rate)
            unrealized_rep += convert(cr.unrealized, rate)
        realized_total = realized_rep
        unrealized_total = unrealized_rep
        if gross_rep != _ZERO:
            total_return_rate = total_return_blended / gross_rep
    # KPI 總市值's stock half; the options' half is the 選擇權 block's own market value
    # (`KpiBlock.market_value_with`).
    total_value = view.reporting_total_value if view is not None else None

    return KpiBlock(
        as_of=as_of, reporting=reporting, total_return=total_return_blended,
        total_return_rate=total_return_rate, realized_total=realized_total,
        unrealized_total=unrealized_total, xirr=xirr_value,
        xirr_window_days=xirr_window_days, xirr_reason=xirr_reason,
        by_currency_value=view.by_currency_value if view is not None else None,
        price_reads=price_reads, fx_reads=dict(resolver.reads),
        bundle=bundle, has_options=has_options, option_symbols=opt_syms, book=book,
        actions=actions, price_map=price_map, last_trades=last_trades, valued=valued,
        valued_stocks=valued_stocks, returns=returns, view=view,
        stock_market_value=total_value, resolver=resolver,
        cash_movements=list(cash_movements),
    )


def build_dashboard(
    conn: sqlite3.Connection, *, now: datetime, reporting: Currency
) -> DashboardData:
    """Assemble the complete dashboard data model from SQLite (read-only).

    Served through the result cache (PERF spec 2026-10-18 §12.1, ``shared/result_cache.py``,
    which replaced the in-flight-only sharing of 2026-10-13): one entry per database file,
    ``data_version``, write generation, DAY and reporting currency, so any commit — this
    process, another one, the settlement child — makes the next read build afresh, and
    identical builds that run at the same time still run once — also while another request's
    write window is open (post-v0.1.32 item 1: the commit itself moves ``data_version``).
    Nothing is cached inside a transaction or for a database with no file. Every reader
    receives its own deep copy with the fields that depend on *now* beyond its date
    (:data:`SUB_DAY_FIELDS`) set to its own clock.

    **The settlement store** (PERF spec §10): the days before the store's ``live_from`` are
    read from the published generation, the rest valued live, all inside ONE read
    transaction opened after the cache key (PERF-S12). With the store clean, or dirty only
    inside the live window, the payload is byte-identical to a full live build; while history
    is stale the days ``[Dd, live_from)`` are the previous generation's and
    ``DashboardData.history_state`` says so. ``PD_SETTLE=off`` or an unusable store: every
    day is live.
    """
    return build_dashboard_keyed(conn, now=now, reporting=reporting)[0]


def build_dashboard_keyed(
    conn: sqlite3.Connection, *, now: datetime, reporting: Currency
) -> tuple[DashboardData, result_cache.CacheKey | None]:
    """:func:`build_dashboard` plus the cache key it was served under (``None`` when built
    uncached) — for a caller that derives a second cached result from this build, which may
    be filed under its own key only when both describe the same state
    (``result_cache.same_state``; ``api/alert_inputs.compute_alerts_full``'s ``data_key``)."""
    def build() -> DashboardData:
        # PERF-S12: ONE read transaction, opened after the key (the cache opens it on a miss;
        # an uncached build — another file, a caller inside a transaction — opens it here).
        with result_cache.read_snapshot(conn):
            return _build_dashboard(conn, now=now, reporting=reporting)

    def stamp(data: DashboardData) -> DashboardData:
        for name in SUB_DAY_FIELDS:
            setattr(data, name, now)
        return data

    return result_cache.cached_keyed(
        conn, name=_CACHE_NAME, now=now, reporting=reporting, compute=build,
        copy=_own_copy, restamp=stamp, extra=_cache_extra(),
    )


def file_snapshot_build(
    conn: sqlite3.Connection, data: DashboardData, *, now: datetime, reporting: Currency,
    mark: result_cache.SnapshotMark | None,
) -> result_cache.CacheKey | None:
    """File *data* — a build computed UNCACHED inside the read snapshot *mark* describes — as
    the dashboard's cache entry, under the key :func:`build_dashboard_keyed` will take next,
    once that snapshot has closed (``result_cache.file_marked``).

    ``api/snapshots.py``'s closes redo the build inside their own snapshot when the cached one
    may describe another state (review G #8); a build inside a transaction is never cached,
    so the next ``/api/dashboard`` built it all again (post-v0.1.32 backlog item 4). It is
    filed only while the snapshot's state is still the current one — no commit, write-window
    edge or own write since it opened — and never otherwise. The entry is a deep copy; each
    later reader gets its own, with :data:`SUB_DAY_FIELDS` set to its own clock. Returns the
    key it was filed under, or ``None``."""
    return result_cache.file_marked(
        conn, name=_CACHE_NAME, now=now, reporting=reporting, extra=_cache_extra(),
        value=data, copy=_own_copy, mark=mark,
    )


#: The result-cache name of the dashboard build.
_CACHE_NAME = "dashboard"


def _cache_extra() -> tuple[bool]:
    """The build's key beyond the state, the day and the reporting currency: the kill switch.
    With history stale, a build with the store and a full live build differ on the stale days
    (§10), and a flag flipped between two reads of one state must not be answered from the
    other's entry."""
    return (flags.settle_enabled(),)


#: Every ``DashboardData`` field that depends on ``now`` beyond its date — re-stamped on each
#: reader's copy of a cached build. ``tests/shared/test_result_cache_dashboard.py`` proves the
#: list complete (two clocks on one day, every corpus).
SUB_DAY_FIELDS: tuple[str, ...] = ("as_of",)


def _own_copy(data: DashboardData) -> DashboardData:
    return data.model_copy(deep=True)


#: The most trend books :func:`_replay_once` holds to replay the ledger only once. A build
#: over a usable settlement store values only the days from ``live_from``: today on a clean
#: store (one or two books), the dirty day on a store dirty inside the window — never before
#: the 1st of the previous month, so at most 62 calendar days and one book per event day in
#: them, plus the one before — which this covers. A longer trend (no usable store, or a store
#: far behind: every event day of the history) streams its books and keeps its two replays:
#: holding them would cost far more memory than the replay costs time (measured 2026-10-22, a
#: synthetic 1,092-event ledger, ``PD_SETTLE=off``: its 213 books held raised the build's
#: ``tracemalloc`` peak from 5.3 MB to 14.5 MB, to save a 6 ms replay of a 130 ms build).
_ONE_REPLAY_MAX_BOOKS = 64

#: ...and the most book ENTRIES it holds — each held book's holdings plus its opened
#: positions (a held position is in both) — projected from the books read so far, so a
#: ledger with very many positions streams too. About 2.6 KB per entry (measured as above:
#: 9.4 MB for 3,557 entries), so this bounds what is held at about 21 MB.
_ONE_REPLAY_HELD_ENTRIES = 8_000


def _replay_once(cut: LedgerBundle, book_days: list[date], as_of: date
                 ) -> tuple[Book | None, Iterator[tuple[date, Book]]]:
    """Today's book and the trend's ``(day, book)`` pairs from ONE replay of *cut* (the
    valuation cut at *as_of*) — post-v0.1.32 item 2.

    The build replayed the whole ledger twice: ``build_book(cut)`` for today's book
    (``kpis_as_of``), then ``iter_books(cut, book_days)`` for the trend, which replays every
    event again from the first. Here ``iter_books`` is asked for *book_days* and then
    *as_of*: its last book IS ``build_book(cut, allow_oversell=True)``, field for field
    (``iter_books``' contract — ``cut.through(as_of)`` holds the same rows as *cut*; pinned
    by ``tests/portfolio/test_engine_iter_books.py``), and the pairs before it are the very
    pairs the trend read. Today's book is needed BEFORE the trend reads any (its flows leave
    out today's book's refused payouts), so the trend's books are HELD until it does — and
    released one by one as it reads them (:func:`_drained`): a book the trend has passed is
    referenced by nothing here.

    **Bounded** (:data:`_ONE_REPLAY_MAX_BOOKS`, :data:`_ONE_REPLAY_HELD_ENTRIES`). Past either
    bound this answers ``None`` for today's book (``kpis_as_of`` replays it itself, as
    before) and the trend reads the books held so far, then the REST of the same replay
    lazily — never past its own last day: the two replays it always cost, and no more.

    **A failure falls back to the two replays, from scratch.** When the replay raises while
    its books are held — a row that cannot be booked, a day whose holdings cannot be
    assembled, or a ``MemoryError`` — what it holds is dropped, today's book is ``None``
    (``kpis_as_of`` replays and raises whatever it raised before) and the trend gets its own
    replay, started only when it first asks for a book (:func:`_replay_later`), where the
    build started it before item 2: a ledger error is raised again on the same day, in the
    same sentence. ``MemoryError`` is not re-raised at once because the hold may be what
    exhausted the memory: the old path holds at most two books, so shedding the hold gives
    the build the chance it had before item 2 instead of failing one that used to succeed.
    """
    if len(book_days) > _ONE_REPLAY_MAX_BOOKS:
        return None, _replay_later(cut, book_days)
    days = [*book_days, as_of]
    held: list[tuple[date, Book]] = []
    entries = 0
    try:
        pairs = zip(days, iter_books(cut, days), strict=True)
        for day, book in pairs:
            held.append((day, book))
            remaining = len(book_days) - len(held)
            if remaining < 0:
                break  # today's book, after every trend book
            size = len(book.holdings) + len(book.opened_positions)
            entries += size
            if remaining and entries + size * remaining > _ONE_REPLAY_HELD_ENTRIES:
                return None, chain(_drained(held), islice(pairs, remaining))
    except Exception:  # noqa: BLE001 - every failure takes the two-replay path (docstring)
        held.clear()
        return None, _replay_later(cut, book_days)
    today = held.pop()[1]
    return today, _drained(held)


def _drained(held: list[tuple[date, Book]]) -> Iterator[tuple[date, Book]]:
    """*held*'s pairs in order, each removed from *held* as it is handed out, so a book the
    consumer has moved past is no longer referenced from here."""
    held.reverse()
    while held:
        yield held.pop()


def _replay_later(cut: LedgerBundle, book_days: list[date]) -> Iterator[tuple[date, Book]]:
    """The trend's own replay of *cut* over *book_days*, started only when the trend first
    asks for a book — where the build started it before item 2 (the two-replay path)."""
    yield from zip(book_days, iter_books(cut, book_days), strict=True)


def _build_dashboard(
    conn: sqlite3.Connection, *, now: datetime, reporting: Currency
) -> DashboardData:
    """The build itself — :func:`build_dashboard` decides whether it is shared."""
    as_of = now.date()

    # 1. Ledgers and reference data. One bundle, one loader (Stored* -> ledger models).
    bundle = load_ledger_bundle(conn)
    instruments = bundle.instruments
    # DEF-056 (owner ruling 2026-09-24): FX conversions and cash movements count from their
    # own date like every other ledger — the SAME predicate as step 1c's bundle cut. The FX
    # pool and the cash series already bounded themselves by `as_of`; XIRR's three cost
    # kinds (AI-D42) and 交易與融資成本 (the B − A split) did not, so a future-dated fee or
    # rebate was a flow dated after XIRR's own terminal value. Cut ONCE, here, at the load.
    convs = [
        FXConversion(account_id=s.account_id, date=s.date, from_ccy=s.from_ccy,
                     from_amount=s.from_amount, to_ccy=s.to_ccy, to_amount=s.to_amount)
        for s in valued_rows_as_of(list_fx_conversions(conn), as_of)
    ]
    accounts = {a.account_id: a for a in list_accounts(conn)}
    # Cash movements feed BOTH the FX pool (step 5 — a foreign deposit/opening now funds the
    # pool and carries its cost basis, spec 2026-07-30) and the net-worth cash series (9b).
    # Loaded once here so the two views can never read different rows.
    cash_movements = valued_rows_as_of(list_cash_movements(conn), as_of)

    # 1b. Unregistered-symbol guard (2026-07-02): a ledger row whose symbol has no
    # Instrument row has no quote currency — it cannot be booked, valued, or priced.
    # NEVER crash the dashboard over it: exclude those events from ALL computation
    # (book, XIRR, trend, dividends — consistently) and surface the symbols in
    # freshness.unregistered_symbols so the UI can tell the user exactly how to fix it
    # (register the symbol, then the next build includes the rows). Same degradation
    # philosophy as the oversold (賣超) path.
    unregistered = bundle.unregistered_symbols
    if unregistered:
        bundle = bundle.without_unregistered()

    # PERF spec §8.5: every FX read of this build — the current rates, XIRR's rate per flow,
    # the trend's, the cash series' and the triangulation's histories — goes through ONE cache
    # (one query per directed pair; measured before: 1,059 `fx_rates` queries per build).
    source = LatestStored(conn, now=now, fx_cache=FxSeriesCache(conn))

    # PERF spec §10 steps 2–4, decided BEFORE the book so that one replay can serve both
    # today's book and the trend's (post-v0.1.32 item 2, :func:`_replay_once`): the settlement
    # store, when it may be read (``None`` = every day live — ``PD_SETTLE=off``, or a store
    # not usable for this reporting currency and code); the days before `live` come from it
    # (contiguous from the series' own first day, never a placeholder — `settled_prefix`);
    # `live` is the first day valued here. Without a usable store it is the first day: all
    # live. Reads only, inside the build's one read snapshot, so reading them first changes
    # nothing they read. `cut` is the valuation cut `kpis_as_of` takes (step 1c) — the same
    # rows, so `trend_start` and the book days are the ones the trend branch below reads.
    cut = bundle.valued_as_of(as_of)
    store = _usable_store(conn, reporting)
    prefix: settlement_store.SettledPrefix | None = None
    first_day: date | None = None
    live: date | None = None
    book_days: list[date] = []
    if cut.transactions or cut.dividends or cut.opening:
        first_day = trend_start(cut)
        assert first_day is not None  # the ledger has a trade, a dividend or an opening
        if store is not None:
            prefix = settlement_store.settled_prefix(conn, store, first_day=first_day,
                                                     today=as_of)
        live = first_day if prefix is None else prefix.live_from
        book_days = trend_book_days(cut, start=live, end=as_of)
    today_book, trend_books = _replay_once(cut, book_days, as_of)

    # 1c, 2, 3 and 4: the valuation cut, the book, the valuation, the returns, XIRR and the
    # KPI blend — the ONE computation a period close repeats as at its own day (§11.2).
    kpi = kpis_as_of(bundle, book=today_book, cash_movements=cash_movements, prices=source,
                     fx=source, as_of=as_of, reporting=reporting)
    has_options = kpi.has_options
    bundle = kpi.bundle
    # Read-only local views of the (now filtered) bundle, for the steps below.
    txs, divs, opening = bundle.transactions, bundle.dividends, bundle.opening
    book = kpi.book
    oversold_positions = book.oversold_positions
    has_oversold = bool(oversold_positions)
    orphan_dividends = book.orphan_dividends
    unapplied_actions = book.unapplied_actions
    opt_syms = kpi.option_symbols
    held_symbols = kpi.held_symbols
    price_reads = kpi.price_reads
    actions = kpi.actions
    valued = kpi.valued
    valued_stocks = kpi.valued_stocks
    resolver = kpi.resolver
    returns = kpi.returns
    view = kpi.view
    allocation: SectorAllocation | None
    # FU-D31 (P1①) / R6 (2026-07-19): canonicalize sectors at the donut GROUPING seam. Stored
    # instrument rows ARE now migrated once at the schema/boot seam (R6,
    # store.migrate_instrument_sectors), so most stored values are already canonical here; this
    # read-time canonicalization is KEPT as defense-in-depth — a provider- or CSV-supplied
    # synonym (or a value stored between boots) still groups correctly before the next boot
    # migrates it. Both the sector-allocation donut AND the sector_weight alert group on THIS
    # SectorAllocation (strategy/alerts.py reads data.allocation.weights), so this ONE
    # canonicalization fixes both — verified single seam. Holding ROWS keep their (now usually
    # already-canonical) stored sector, section 8 below; only the grouping is normalized.
    # Canonical keys are English; zh display is deferred (see shared/sectors.py).
    alloc_instruments = {
        sym: inst.model_copy(update={"sector": canonical_sector(inst.sector)})
        for sym, inst in instruments.items()
    }
    try:
        allocation = sector_allocation(valued_stocks, alloc_instruments, resolver.rate,
                                       reporting)
    except KeyError:
        allocation = None
    xirr_value = kpi.xirr
    xirr_window_days = kpi.xirr_window_days
    xirr_reason = kpi.xirr_reason

    # 5. FX P&L — settlement != funding accounts; cold-start KeyError -> None.
    # The exposure figure is the market value of equity holdings quoted in the account's
    # FOREIGN (settlement) currency — it is labelled and marked-to-spot in that one
    # currency (compute_account_fx: foreign_stock_value * (spot - avg_rate)). Sum ONLY
    # holdings whose quote currency IS that settlement currency: an account may hold
    # instruments in more than one currency (a dual-market account with settlement USD /
    # funding MYR would also hold MYR-quoted MY stocks), and folding a MYR-quoted value
    # into the USD exposure would mis-sum two currencies into one number. h.quote_ccy is
    # the instrument's quote currency (Holding carries it, sourced in build_book), so no
    # instruments-map lookup is needed here. On all current data each such account holds
    # only settlement-ccy instruments, so this filter is a no-op today (bug-proofing).
    exposure: dict[str, tuple[Currency, Decimal]] = {}
    for acct in accounts.values():
        if acct.settlement_ccy == acct.funding_ccy:
            continue
        stock_value = _ZERO
        for h in valued:
            if (h.account_id == acct.account_id
                    and h.quote_ccy == acct.settlement_ccy
                    and h.market_value is not None):
                stock_value += h.market_value
        exposure[acct.account_id] = (acct.settlement_ccy, stock_value)
    fx_summary: FXSummary | None
    fx_reason: str | None = None
    try:
        # `as_of` is the SAME day `GET /api/cash` bounds its balances to (M5-06), so the FX
        # pool and the funds view are asked for the same day — the manual's
        # `fx.pool_equals_funds` (§8.3) is an identity only under that condition (X8b).
        # ⑪: the FX pool's foreign cash counts the payouts the BOOK booked — the same rows
        # `GET /api/cash` counts, or `fx.pool_equals_funds` (§8.3) breaks by a refused one.
        fx_summary = compute_fx_summary(accounts, instruments, txs,
                                        booked_dividends(divs, book.refused_dividends), convs,
                                        exposure, resolver.rate, reporting,
                                        cash_movements, as_of=as_of)
    except KeyError as exc:
        # LAST RESORT, and no longer a SILENT one. `compute_fx_summary` now degrades per
        # account (QA-01: an EMPTY account with no home->reporting rate used to raise here
        # and take the whole `fx` section — and both FX KPIs — down with it), so a missing
        # rate does not reach this branch any more. What made that defect invisible was
        # that `fx = None` explained nothing anywhere on the payload; the resolver's
        # message is therefore surfaced VERBATIM as `freshness.fx_unavailable_reason`,
        # exactly as `xirr_unavailable_reason` already does with the same message.
        fx_summary = None
        fx_reason = str(exc).strip("'\"")
    if fx_summary is not None and not any(
            r.realized_fx is not None or r.unrealized_fx_total is not None
            for r in fx_summary.by_account.values()):
        # NOTHING TO REPORT is not a report of zero. Every FX-exposed account came back
        # None (no account has an FX cost basis at all), so the two rollup totals are the
        # ZERO an empty accumulator starts at — a number no ledger row supports, and
        # exactly the "initialised value rendered as a fact" defect. The section is absent
        # instead, and now says why.
        #
        # ⚠ This is NOT the QA-01 bail-out coming back. That one dropped the section
        # because ONE unrelated (and empty) account lacked a rate, discarding another
        # account's correct 33,000 TWD; this fires only when EVERY account is None, so a
        # single real figure anywhere keeps the section. It is also decided from the
        # LEDGER rather than from whether some unrelated pair happened to be stored, which
        # is what made the old behaviour differ for the same portfolio on two days.
        fx_summary = None
        fx_reason = "尚無換匯或外幣入金的取得成本，無匯兌損益可計算"

    # 6. Dividend summary — cash actually received (incl. DRIP net), native ccy.
    # ttm_cutoff bounds the trailing-12-month window (display-only attribution).
    ttm_cutoff = as_of - timedelta(days=365)
    year_ccy: dict[int, dict[Currency, Decimal]] = {}
    total_ccy: dict[Currency, Decimal] = {}
    ttm_ccy: dict[Currency, Decimal] = {}
    # R6 (2026-10-04): only what the BOOK booked. The replay refuses a dividend with no
    # position (CR-02), one landing on an open declared short (the short PAYS it), and one on
    # a ticker an EXCHANGE vacated — and this walk of the raw ledger summed all three, so one
    # payout got three answers: counted here, excluded from 總報酬 and XIRR, 無法入帳 in the
    # drawer. Skipped through the SAME value key XIRR skips them by. Each stays named where it
    # is explained: the XIRR reason / the drawer (no position), the 待釐清 flag (the other two).
    refused = {dividend_key(dv) for dv in book.refused_dividends}
    for dv in divs:
        if dv.type is DividendType.STOCK:
            continue  # 配股 adds shares, not cash
        if dividend_key(dv) in refused:
            continue
        ccy = instruments[dv.symbol].quote_ccy
        per_year = year_ccy.setdefault(dv.date.year, {})
        per_year[ccy] = per_year.get(ccy, _ZERO) + dv.net
        total_ccy[ccy] = total_ccy.get(ccy, _ZERO) + dv.net
        if dv.date >= ttm_cutoff:
            ttm_ccy[ccy] = ttm_ccy.get(ccy, _ZERO) + dv.net
    dividend_summary = DividendSummary(
        by_year=[DividendYearRow(year=y, by_currency=year_ccy[y])
                 for y in sorted(year_ccy)],
        total_by_currency=total_ccy,
        ttm_net=ttm_ccy,
    )

    # 7. Ex-dividend calendar — held symbols, upcoming only.
    calendar: list[ExDividendItem] = []
    for sym in held_symbols:
        inst = instruments[sym]
        for ev in get_dividend_events(conn, sym):
            if ev.ex_date >= as_of:
                calendar.append(ExDividendItem(
                    symbol=sym, name=inst.name, ex_date=ev.ex_date,
                    pay_date=ev.pay_date, cash_amount=ev.cash_amount,
                    stock_amount=ev.stock_amount, currency=ev.currency,
                    source=ev.source))
    calendar.sort(key=lambda e: e.ex_date)

    # 7b. Dividend projection (spec 05) — current-year declared cash flow, per-account
    # net (withholding only), per currency, never summed across currencies.
    dividend_projection: DividendProjection = project_dividends(
        valued_stocks, calendar, accounts, instruments, year=as_of.year
    )

    # 8. Holding rows — enrichment + weight; age-based staleness overrides
    # value_holdings' presence-based flag.
    total_value = view.reporting_total_value if view is not None else None
    holding_rows: list[HoldingRow] = []
    for h in valued_stocks:
        inst = instruments[h.symbol]
        acct = accounts[h.account_id]
        pr = price_reads.get(h.symbol)
        weight: Decimal | None = None
        if total_value is not None and total_value != _ZERO and h.market_value is not None:
            try:
                weight = (convert(h.market_value, resolver.rate(h.quote_ccy, reporting))
                          / total_value)
            except KeyError:
                weight = None
        # Per-holding unrealized return rate, computed HERE so the frontend never derives it
        # (audit H1). Basis = original invested cost — see HoldingRow.unrealized_pct for why
        # adjusted cost is both inconsistent with the KPI definition and unsafe (it may be
        # <= 0, which flips the ratio's sign on a fully-recovered position).
        # A SHORT carries a NEGATIVE basis (the proceeds received), which would flip this
        # ratio's sign and render a profitable short as a loss — the very negative-denominator
        # trap the note above warns about, arriving from the other direction. Divide by the
        # MAGNITUDE of the capital at risk: the numerator already carries the correct sign.
        unrealized_pct: Decimal | None = None
        if h.unrealized_pnl is not None and h.original_cost_total != _ZERO:
            unrealized_pct = h.unrealized_pnl / abs(h.original_cost_total)
        data = h.model_dump()
        data.update(
            # I-10: the account as a TOKEN, like every backend sentence — ``accounts.name`` is
            # the English label (「TW Broker」) the owner never chose. pdApi resolves it for
            # every page, pdApi.download for the print reports that render this field.
            account_name=account_ref(acct.account_id), name=inst.name, market=inst.market,
            sector=inst.sector, board=inst.board,
            price_as_of=pr.as_of if pr is not None else None,
            price_stale=pr.stale if pr is not None else True,
            weight=weight,
            unrealized_pct=unrealized_pct,
        )
        holding_rows.append(HoldingRow(**data))

    # 9. Trend — bulk-load histories, then the ONE-pass daily replay (PERF spec §8).
    trend_reason: str | None = None
    # the flow `trend_reason` names (kept off the wire; 月度成績's live closes read it)
    trend_flow: UnconvertibleFlow | None = None
    # Declared OUT here (empty by default) because step 10's benchmark counterfactual reads
    # it as well, and on a ledger with no events the trend branch below never runs. Empty is
    # the honest value there: no flows to convert, so the counterfactual refuses.
    trend_fx_history: FxHistory = {}
    # The store, the stored prefix, `first_day`, `live` and the trend's books were decided
    # before the book (above), on the same cut and the same condition as this branch.
    if txs or divs or opening:
        assert first_day is not None and live is not None  # set above for this same cut
        # BOTH ends of every corporate action (audit F-50, `trend_symbols`): a SPINOFF child
        # appears in no other ledger. `without_unregistered()` above guarantees every symbol
        # has an Instrument row, so the quote-currency lookups stay total.
        ledger_symbols = trend_symbols(bundle)
        # RAW on purpose (W6c): this is the ONE series read that must NOT be re-expressed
        # here. Its consumer is `daily_value_series`, which values a DIFFERENT day on every
        # iteration and therefore applies §5.1(d) itself, per point, at its `_entry_at_or_
        # before` lookup — with `valued_on=day`, which is exactly the factor a precomputed
        # re-expression could not know. Correcting it twice would divide by the ratio twice.
        # PERF spec §8.5: read from the last close on or before the FIRST VALUED day — the
        # trend's first day, or the store's `live_from` — the earliest day a price is looked
        # up for, and never from before 1900, v0.1.31's floor (`bounded_closes`; the light
        # read). A first live day on a weekend, a holiday or in a suspension keeps the close
        # it carries forward.
        price_history: PriceHistory = {
            sym: bounded_closes(conn, sym, live, as_of, floor=_EPOCH)
            for sym in ledger_symbols
        }
        # (``trend_fx_history`` is declared before this branch so step 10's benchmark
        # counterfactual can read it on an EMPTY ledger too — where it is simply empty and
        # the counterfactual refuses honestly instead of raising UnboundLocalError.)
        # The pair set has ONE owner, shared with the settlement worker (PERF-F2): the quote
        # currencies AND the currency of every cash movement the trend converts.
        fx_history = trend_fx_history
        for ccy in trend_fx_currencies(bundle, cash_movements):
            if ccy == reporting:
                continue
            for base, quote in ((ccy, reporting), (reporting, ccy)):
                rows = source.fx_cache.history(base, quote, _EPOCH, as_of)
                if rows:
                    fx_history[(base, quote)] = [(r.as_of, r.rate) for r in rows]
        # ONE replay feeds every day (PERF spec §8.1): `iter_books` yields the book of each
        # day a row counts from — the replay that also gave today's book, when it held them
        # (`_replay_once`) — and the payouts the flows leave out are this request's book's
        # refusals — the same rows a second whole-bundle replay would refuse.
        # `available` is decided over EVERY flow whatever the start (§4: a series-level
        # aggregate, never settled). The options flag of the days before `live` is the
        # store's per day (`options_open`, §10 step 4 — never a series-level stored flag);
        # with no day before the start there is none.
        options_before = (prefix is not None and live > first_day
                          and any(d.options_open for d in prefix.days))
        trend = value_series(
            bundle, price_history, fx_history, reporting, start=live, end=as_of,
            cash_movements=cash_movements, refused_dividends=book.refused_dividends,
            books=trend_books, options_open_before=options_before)
        # The trend has read its books (or returned without them): drop the iterator, which
        # still holds any it did not read, before the rest of the build (review of item 2).
        del trend_books
        if not trend.available:
            # the series' ONE sentence: the first flow with no rate on or before its date,
            # by its date and its pair (accountant MINOR 4, owner ruling 2026-10-22) — the
            # same flows `value_series` converts, the same refusals
            trend_flow = first_unconvertible_flow(
                bundle, fx_history, reporting, cash_movements=cash_movements,
                refused_dividends=book.refused_dividends)
            trend_reason = trend_unavailable_reason(trend_flow)
        else:
            # 9b. Total net worth (FU-D29 / deferred C8): compose the UNCHANGED trend
            # with a daily cash series. Cash is read from the SAME Stored rows the 資金管理
            # /api/cash view uses (unregistered symbols skipped inside cash_balances,
            # exactly as there), converted at carry-forward FX per day. Display-only
            # attribution — no money-of-record path is touched.
            cash_txs = list_transactions(conn)
            # ⑪ (owner ruling 2026-10-05): only the payouts the replay booked, exactly as
            # `GET /api/cash` — a refused dividend raised the net-worth line by a payout
            # 總報酬, XIRR and 股利總覽 all left out. `book` is this request's replay; it cuts at
            # `as_of`, which is where this series ends.
            cash_divs = booked_dividends(list_dividends(conn), book.refused_dividends)
            cash_convs = list_fx_conversions(conn)
            cash_ccys = {
                ccy for _acct, ccy in cash_balances(
                    cash_movements, cash_convs, cash_txs, cash_divs, instruments)
            }
            cash_fx_history: FxHistory = {}
            for ccy in cash_ccys:
                if ccy == reporting:
                    continue
                for base, quote in ((ccy, reporting), (reporting, ccy)):
                    rows = source.fx_cache.history(base, quote, _EPOCH, as_of)
                    if rows:
                        cash_fx_history[(base, quote)] = [(r.as_of, r.rate) for r in rows]
            # PERF spec §10 step 4: only the LIVE points are composed with cash — a cash
            # map that starts at `live` would read every stored day's cash as 0
            # (`networth.py`); a stored day carries its own stored `net_worth`.
            cash_by_date = daily_cash_series(
                cash_movements, cash_convs, cash_txs, cash_divs, instruments,
                cash_fx_history, reporting, end=as_of,
                start=None if prefix is None else live)
            # The series as SERVED carries each point's 累計總損益（含匯兌）, the tooltip's figure
            # (owner ruling 2026-10-22, `dashboard_models.trend_pnl`): set on the live days
            # here and on the stored days as they are built (`_stored_point`), so neither part
            # is copied a second time.
            trend = with_trend_pnl(compose_net_worth(trend, cash_by_date))
            if prefix is not None and prefix.days:
                trend = trend.model_copy(update={
                    "points": [_stored_point(d, reporting) for d in prefix.days]
                    + trend.points})
    else:
        trend = TrendSeries(points=[], reporting_currency=reporting, available=False)
        trend_reason = "尚無任何帳本紀錄"
    history = _history_state(store, prefix if trend.available else None, today=as_of)

    # 10. KPIs — blended by kpis_as_of (None whenever the blend cannot be formed honestly);
    # what depends on the trend or the connection is assembled here.
    total_return_blended = kpi.total_return
    # AI-D41: B and B − A ride beside A. Computed HERE because this is the only layer
    # holding both the ReturnSummary and the TrendSeries; neither module can see the
    # other's figure, which is precisely why the discrepancy went unnoticed.
    fx_outcome = fx_complete_return(
        trend, has_oversold=has_oversold,
        has_unapplied_action=bool(unapplied_actions),
        has_orphan_dividend=bool(orphan_dividends),
        unbookable_dividends=book.unbookable_dividends,
        refused_closes=book.refused_closes)
    fx_complete = fx_outcome.value
    # AI-D48 made B − A a TWO-term difference, so the third term is subtracted out rather
    # than left inside 「本金匯率效果」 — a residual that quietly absorbs whatever joins B next
    # is the same mislabelling AI-D48 removed, one field over. B = A + 本金匯率 + 交易與融資成本.
    trading_cost = trading_financing_cost(cash_movements, trend_fx_history, reporting)
    # OPT-D20: while an option is open, B (trend-derived) excludes its value and A includes its
    # unrealized, so B − A would absorb the option's value as "FX effect" — withheld, with the
    # reason in `options.principal_fx_note`. Never reached without an option.
    option_open = has_options and has_open_option(book.holdings, opt_syms)
    principal_fx = (
        None if (fx_complete is None or total_return_blended is None or trading_cost is None
                 or option_open)
        else fx_complete - total_return_blended - trading_cost
    )
    # Options spec §7.1: the block, from this request's ONE replay and ONE valuation.
    options_block = options_block_of(kpi, source,
                                     trend_excludes_value=trend.options_value_excluded)
    # R4 / AI-D43: the index counterfactual rides beside B, measured on the same ruler.
    benchmark = _benchmark_comparison(
        conn, bundle, trend_fx_history, reporting,
        as_of=as_of, actions=actions, fx_complete=fx_complete,
        # AI-D48/D49: the SAME stream B is built from. The counterfactual then declines to
        # place the cash flows (they carry no market), so the costs land on the portfolio leg
        # only — one stream, one place that decides what can be spent on an index.
        cash_movements=cash_movements,
        refused_dividends=book.refused_dividends,
        # PERF spec §10 step 5: the store's accumulated state, only while it describes these
        # exact flows; its terminal value is always today's close.
        stored=_stored_bench(store, bundle),
    )
    kpis = KpiSummary(
        reporting_currency=reporting,
        # KPI 總市值 (OPT-D5): the stock grand subtotal plus the block's own market value.
        total_market_value=kpi.market_value_with(options_block),
        total_return=total_return_blended,
        total_return_fx_complete=fx_complete,
        principal_fx_effect=principal_fx,
        trading_financing_cost=None if fx_complete is None else trading_cost,
        fx_complete_reason=fx_outcome.reason,
        total_return_rate=kpi.total_return_rate,
        realized_total=kpi.realized_total,
        unrealized_total=kpi.unrealized_total,
        xirr=xirr_value,
        xirr_window_days=xirr_window_days,
        fx_realized=fx_summary.reporting_realized_fx if fx_summary is not None else None,
        fx_unrealized=(fx_summary.reporting_unrealized_fx
                       if fx_summary is not None else None),
    )

    # 10b. Holdings subtotals — a per-(account, market) FILTER re-aggregation of the SAME
    # per-holding reporting-currency values that feed `kpis` (NOT a new money formula). The
    # grand cell equals kpis.total_market_value / unrealized_total by construction; the
    # frontend's 合計 footer + filtered CSV/report pick the matching cell WITHOUT any client
    # math. Value availability mirrors the KPI's all-or-nothing FX rule: total_market_value
    # is derived from `view` (None -> the combined blend was unformable) and unrealized from
    # `returns` (None likewise).
    holdings_subtotals = _holdings_subtotals(
        holding_rows, resolver.rate, reporting,
        value_available=view is not None,
        unrealized_available=returns is not None,
    )

    # 11. Freshness.
    price_fresh = [
        PriceFreshness(symbol=sym,
                       as_of=pr.as_of if pr is not None else None,
                       stale=pr.stale if pr is not None else True)
        for sym, pr in price_reads.items()
    ]
    # Stable order: resolver.reads insertion order derives from set iteration over
    # quote currencies (hash-seed-dependent across processes), so sort by (base, quote)
    # to make the fx freshness + missing_fx lists deterministic in the API payload.
    fx_reads_sorted = sorted(
        resolver.reads.items(), key=lambda kv: (kv[0][0].value, kv[0][1].value)
    )
    fx_fresh = [
        FxFreshness(base=base, quote=quote,
                    as_of=read.as_of if read is not None else None,
                    stale=read.stale if read is not None else True,
                    source=read.source if read is not None else None)
        for (base, quote), read in fx_reads_sorted
    ]
    freshness = FreshnessReport(
        prices=price_fresh,
        fx=fx_fresh,
        any_stale=any(p.stale for p in price_fresh) or any(f.stale for f in fx_fresh),
        missing_prices=[sym for sym, pr in price_reads.items() if pr is None],
        missing_fx=[f"{base.value}/{quote.value}"
                    for (base, quote), read in fx_reads_sorted if read is None],
        xirr_unavailable_reason=xirr_reason,
        trend_unavailable_reason=trend_reason,
        fx_unavailable_reason=fx_reason,
        unregistered_symbols=unregistered,
        # M1: a CHECK over the same reads the list above reports on — no extra query, no
        # rate mutated. Empty unless three read pairs close a triangle.
        fx_triangulation=_fx_triangulation(resolver.reads, history=resolver.history,
                                           valued_on=as_of),
    )
    freshness.set_trend_unconvertible(trend_flow)

    return DashboardData(
        as_of=now,
        reporting_currency=reporting,
        kpis=kpis,
        holdings=holding_rows,
        holdings_subtotals=holdings_subtotals,
        # OPT-D5: the option rows live in `options.realized`, never summed in here. Without an
        # option this is the book's own object, unchanged.
        realized=(stock_realized(book.realized, opt_syms) if has_options
                  else book.realized),
        returns=returns,
        allocation=allocation,
        currency_view=view,
        fx=fx_summary,
        dividends=dividend_summary,
        ex_dividend_calendar=calendar,
        trend=trend,
        benchmark=benchmark,
        freshness=freshness,
        insights=[],
        dividend_projection=dividend_projection,
        # Read off the BOOK (see the `unapplied_actions` assignment above), not off the
        # holdings: this is the ONLY channel that survives an action whose source position
        # was emptied or never existed, and §6.3's red footer needs a cause to name.
        unapplied_actions=unapplied_actions,
        oversold_positions=oversold_positions,
        orphan_dividends=orphan_dividends,
        opened_positions=book.opened_positions,
        unbookable_dividends=book.unbookable_dividends,
        options=options_block,
        history_state=history,
    )


def _usable_store(conn: sqlite3.Connection,
                  reporting: Currency) -> settlement_store.StoreState | None:
    """The settlement store's state when a build may read it (PERF spec §10 step 2): the
    switch on (``PD_SETTLE``) and the store usable — a published generation, this code's
    calc version, this reporting currency, a verified trigger set. ``None``: every day live
    (§11.4 — slower, never stale, never missing). Reads only."""
    if not flags.settle_enabled():
        return None
    state = settlement_store.store_state(conn, reporting=reporting)
    return state if state.usable else None


def _stored_point(day: settlement_store.SettledDay, reporting: Currency) -> TrendPoint:
    """A settled day as the trend point it was settled from, with its stored ``net_worth``,
    as SERVED: with its ``pnl`` (``trend_pnl``, derived here — the store keeps no such
    column)."""
    return TrendPoint(date=day.day, total_value=day.total_value,
                      net_invested=day.net_invested, incomplete=day.incomplete,
                      net_worth=day.net_worth,
                      pnl=trend_pnl(day.total_value, day.net_invested, reporting,
                                    incomplete=day.incomplete))


def _history_state(store: settlement_store.StoreState | None,
                   prefix: settlement_store.SettledPrefix | None, *,
                   today: date) -> HistoryState:
    """``DashboardData.history_state`` (PERF spec §10 step 6).

    With days served from the store (*prefix*), the first live day and the stale days are
    the ones this build served. Without them — no usable store, or a trend that is
    unavailable (nothing of it served) — a usable store's own ``live_from`` and stale range
    stand, which is what a period close is read by (``api/snapshots.py``)."""
    if store is None:
        return HistoryState()
    if prefix is not None:
        first_live, stale = prefix.live_from, prefix.stale
    else:
        first_live = min(settlement_store.live_from(store, today), today + timedelta(days=1))
        stale = settlement_store.stale_range(store, today)
    return HistoryState(
        live_from=first_live, stale_from=None if stale is None else stale[0],
        stale_to=None if stale is None else stale[1], generation=store.generation,
        settled_through=store.settled_through, held_through=store.held_through,
        dirty_day=store.dirty_day, seq=store.seq)


def _underlying_prices(
    prices: PriceSource, book: Book, opt_syms: frozenset[str],
    instruments: dict[str, Instrument], actions: ActionIndex, *, as_of: date,
) -> dict[str, tuple[Decimal, date]]:
    """OPT-D17: the latest stored price of every REGISTERED underlying of an open contract —
    ONE read per distinct underlying (through *prices*, the valuation's own source),
    re-expressed across splits like every held price (``price_in``), and never added to
    ``price_reads`` (so never in freshness). An unregistered or unpriced underlying is simply
    absent (the block omits the two keys)."""
    out: dict[str, tuple[Decimal, date]] = {}
    seen: set[str] = set()
    for h in book.holdings:
        if h.symbol not in opt_syms or h.shares == _ZERO:
            continue
        terms = instruments[h.symbol].option
        if terms is None or terms.underlying in seen:
            continue
        seen.add(terms.underlying)
        und = instruments.get(terms.underlying)
        if und is None or und.is_option:
            continue
        pr = prices.price(terms.underlying)
        if pr is not None:
            out[terms.underlying] = (
                price_in(actions, terms.underlying, pr.value, priced_on=pr.as_of,
                         valued_on=as_of), pr.as_of)
    return out
