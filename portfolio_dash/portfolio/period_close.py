"""Month and year closes (PERF spec 2026-10-18 §11.2, §11.3; plan §6.1 "period_close").

Pure functions over in-memory inputs, plus one read source (:class:`OnOrBefore` over the run's
connection). The settlement worker (``portfolio/settle_worker.py``) calls them for every period
end of its range, from the books of its ONE replay — never a replay per period end.

**The tax sheets' row selection lives here** (plan §6.1 "Tax-sheet extraction"): the annual tax
package (``export/tax.py``) and a period close's activity both take their rows from
:func:`realized_rows`, :func:`post_close_dividend_rows`, :func:`dividend_rows` and
:func:`fx_realized_rows`, so a close's realized, dividend and realized-FX lines tie out with the
package sheet by sheet (§11.3) by construction — one selection, two readers. The functions keep
the replay's / the ledger's own row order, which is what makes a year's sum equal the sum of its
months' sums when both are added left to right.

**A close at D** (§11.2) is:

- :func:`kpi_at` — the dashboard's own KPI computation (``dashboard.kpis_as_of``) with the
  "on or before D" sources (:class:`~portfolio_dash.portfolio.dashboard.OnOrBefore`), KPI 總市值
  read through the 選擇權 block exactly as the dashboard reads it, the rates used and their
  dates, and the ``notes`` that say why a figure is absent or unconfirmed;
- :func:`with_trend` — the settled day D's trend figures (``trend_total_value`` /
  ``net_invested`` / ``net_worth``, stocks only, named apart from KPI 總市值) and the period's
  time-weighted return (:func:`period_twr`) — both come from the run's series;
- :func:`activity` — the period's lines (§11.3), each in native currency and in the reporting
  currency at its own date's FX, never totalled across lines;
- :func:`holdings_at` and :func:`cash_at` — the positions and the cash pools at D.

FX P&L at D is deliberately NOT part of a close in this release (§11.2): its absence is not a
zero. ``dividends_receivable`` is computed when a close is READ (package G), never stored.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from portfolio_dash.data_ingestion.store import (
    StoredCashMovement,
    StoredDividend,
    StoredFxConversion,
    StoredTransaction,
)
from portfolio_dash.forex.fx_pnl import realized_fx_rows_as_of
from portfolio_dash.forex.pools import MovementRow
from portfolio_dash.forex.results import FxRealizedRow
from portfolio_dash.portfolio.cash import cash_balances
from portfolio_dash.portfolio.dashboard import KpiBlock, OnOrBefore, kpis_as_of, options_block_of
from portfolio_dash.portfolio.dashboard_models import TrendPoint
from portfolio_dash.portfolio.options import past_expiry_holdings
from portfolio_dash.portfolio.results import Book, RealizedRow
from portfolio_dash.portfolio.returns import booked_dividends
from portfolio_dash.portfolio.settlement_models import (
    CashMovementLine,
    DividendLine,
    FeeLine,
    FxConversionLine,
    FxRealizedLine,
    FxUsed,
    Money2,
    NotBookedLine,
    OpeningLine,
    PairDate,
    PendingPosition,
    PeriodActivity,
    PeriodCash,
    PeriodHolding,
    PeriodKpi,
    PeriodNotes,
    PriceDate,
    RealizedLine,
)
from portfolio_dash.portfolio.timeseries import trend_unavailable_reason
from portfolio_dash.pricing.store import FxSeriesCache
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.fx import convert
from portfolio_dash.shared.models.assets import Account
from portfolio_dash.shared.models.enums import DividendType
from portfolio_dash.shared.models.ledger import (
    Dividend,
    FXConversion,
    LedgerBundle,
    valued_rows_as_of,
)
from portfolio_dash.shared.options import ASSET_OPTION, ASSET_STOCK

_ZERO = Decimal("0")
_ONE = Decimal("1")

#: The realized-row kinds that are a CAPITAL gain or loss — a sale, and a declared short's
#: cover (2026-07-31). A ``dividend`` row (audit H2) is income, reported from the dividend
#: ledger instead.
CAPITAL_KINDS: tuple[str, ...] = ("sale", "short_cover")

# ---------------------------------------------------------------------------------------------
# The tax sheets' row selection (shared with export/tax.py)
# ---------------------------------------------------------------------------------------------


def _inside(day: date, start: date, end: date) -> bool:
    return start <= day <= end


def realized_rows(rows: Iterable[RealizedRow], *, start: date, end: date) -> list[RealizedRow]:
    """The capital-gains sheet's rows: every ``sale`` / ``short_cover`` realized row whose
    ``sell_date`` lies in ``[start, end]``, in the replay's own row order
    (``realized_gains_<year>.csv``)."""
    return [r for r in rows if _inside(r.sell_date, start, end) and r.kind in CAPITAL_KINDS]


def post_close_dividend_rows(rows: Iterable[RealizedRow], *, start: date,
                             end: date) -> list[RealizedRow]:
    """The realized rows of kind ``dividend`` — a cash payout booked after its position closed
    (audit H2) — dated in ``[start, end]``, in replay order. The same payout is also on the
    dividends sheet, which is why the capital-gains sheet leaves it out."""
    return [r for r in rows if _inside(r.sell_date, start, end) and r.kind == "dividend"]


def dividend_rows(dividends: Iterable[Dividend], *, start: date, end: date) -> list[Dividend]:
    """The dividends sheet's rows: every dividend but a 配股 (STOCK adds shares, not cash) whose
    payment ``date`` lies in ``[start, end]``, in ledger order (``dividends_<year>.csv``).

    *dividends* are the rows the caller counts: the tax package replays strictly (a refused
    payout raises there), a period close passes the BOOKED rows only."""
    return [d for d in dividends
            if _inside(d.date, start, end) and d.type is not DividendType.STOCK]


@dataclass(frozen=True)
class AccountFxRealized:
    """One realized-FX row of the 換匯損益 owner, with the account it belongs to."""

    account_id: str
    row: FxRealizedRow


def fx_realized_rows(accounts: Iterable[Account], conversions: Sequence[FXConversion],
                     movements: Sequence[MovementRow], *, start: date,
                     end: date) -> list[AccountFxRealized]:
    """The realized-FX sheet's rows (``fx_realized_<year>.csv``): for every account whose
    settlement currency is not its funding currency, in *accounts* order, each reconversion
    dated in ``[start, end]`` priced at the pool average ON ITS OWN DATE
    (:func:`~portfolio_dash.forex.fx_pnl.realized_fx_rows_as_of` — the one entry point the
    dashboard's 換匯損益 uses, so the two can never price one reconversion differently)."""
    out: list[AccountFxRealized] = []
    for acct in accounts:
        if acct.settlement_ccy == acct.funding_ccy:
            continue
        home, foreign = acct.funding_ccy, acct.settlement_ccy
        acct_convs = [c for c in conversions if c.account_id == acct.account_id]
        acct_moves = [m for m in movements if m.account_id == acct.account_id]
        for fr in realized_fx_rows_as_of(acct_convs, acct_moves, home, foreign):
            if _inside(fr.date, start, end):
                out.append(AccountFxRealized(account_id=acct.account_id, row=fr))
    return out


# ---------------------------------------------------------------------------------------------
# Rates
# ---------------------------------------------------------------------------------------------

#: A rate *ccy* → the reporting currency on or before a day, or ``None`` (no row).
RateAt = Callable[[date, Currency], Decimal | None]


@dataclass(frozen=True)
class RateRead:
    rate: Decimal
    as_of: date | None


def rate_read(fx_cache: FxSeriesCache, reporting: Currency, day: date,
              ccy: Currency) -> RateRead | None:
    """*ccy* → *reporting* on or before *day*: identity, else the direct pair, else the inverse
    pair inverted (``1 / rate``) — the rule every carry-forward reader of the app uses (the
    trend's ``_fx_at``, XIRR's ``fx_at``). ``as_of`` is ``None`` for the identity."""
    if ccy == reporting:
        return RateRead(_ONE, None)
    direct = fx_cache.on_or_before(ccy, reporting, day)
    if direct is not None:
        return RateRead(direct.rate, direct.as_of)
    inverse = fx_cache.on_or_before(reporting, ccy, day)
    if inverse is not None:
        return RateRead(_ONE / inverse.rate, inverse.as_of)
    return None


def own_date_rates(fx_cache: FxSeriesCache, reporting: Currency) -> RateAt:
    """:func:`rate_read`'s rate, for converting a row at its OWN date (§11.3, the trend's flow
    conversion) — over every stored pair, so a cash movement in a currency no instrument is
    quoted in still converts."""
    def rate(day: date, ccy: Currency) -> Decimal | None:
        read = rate_read(fx_cache, reporting, day, ccy)
        return None if read is None else read.rate
    return rate


class _Money:
    """A native sum and its reporting-currency twin (each row at its own rate, ``None`` as
    soon as one row has no rate), added left to right in row order."""

    __slots__ = ("ccy", "native", "ok", "reporting")

    def __init__(self, ccy: Currency) -> None:
        self.ccy = ccy
        self.native = _ZERO
        self.reporting = _ZERO
        self.ok = True

    def add(self, amount: Decimal, rate: Decimal | None) -> None:
        self.native += amount
        if rate is None:
            self.ok = False
        else:
            self.reporting += convert(amount, rate)

    def money(self) -> Money2:
        return Money2(ccy=self.ccy, native=self.native,
                      reporting=self.reporting if self.ok else None)


# ---------------------------------------------------------------------------------------------
# The KPI block (§11.2)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CloseLedger:
    """The ledgers every close of one run reads, loaded once and never cut here (each close
    cuts at its own D). *bundle* is the registered ledger, NOT cut — whether the 選擇權 block
    exists is decided on the whole ledger, as the dashboard decides it (options spec §7.1)."""

    bundle: LedgerBundle
    movements: Sequence[StoredCashMovement]
    conversions: Sequence[StoredFxConversion]
    transactions: Sequence[StoredTransaction]
    dividends: Sequence[StoredDividend]
    accounts: Sequence[Account]


@dataclass(frozen=True)
class CloseKpi:
    """:func:`kpi_at`'s answer: the stored KPI block (trend fields not yet set) and the
    dashboard's intermediates the holdings of the close are read from."""

    kpi: PeriodKpi
    block: KpiBlock


def _pair(base: Currency, quote: Currency) -> str:
    return f"{base.value}/{quote.value}"


def pending_positions(book: Book) -> list[PendingPosition]:
    """The 待釐清 positions on *book* (§11.2 ``notes.pending_positions``): 賣超, an unapplied
    corporate action, a refused dividend, a refused close — each (account, symbol, kind) once,
    in that order."""
    out: list[PendingPosition] = []
    seen: set[tuple[str, str, str]] = set()

    def add(account_id: str, symbol: str, kind: str) -> None:
        if (account_id, symbol, kind) not in seen:
            seen.add((account_id, symbol, kind))
            out.append(PendingPosition.model_validate(
                {"account_id": account_id, "symbol": symbol, "kind": kind}))

    for p in book.oversold_positions:
        add(p.account_id, p.symbol, "oversold")
    for a in book.unapplied_actions:
        add(a.account_id, a.from_symbol, "unapplied_action")
    for d in book.refused_dividends:
        add(d.account_id, d.symbol, "refused_dividend")
    for c in book.refused_closes:
        add(c.account_id, c.symbol, "refused_close")
    return out


def unconfirmed_at(day: date, price_dates: Mapping[str, date | None],
                   keys: Mapping[date, Sequence[str]],
                   carried: Sequence[PriceDate] = ()) -> list[PriceDate]:
    """The closes at *day* no ``ok`` quote run confirmed (§9.1, §11.2
    ``notes.unconfirmed_closes``), in symbol order.

    *keys* is ``RunRequest.unconfirmed_closes``: each day ``k`` a quote fire was settled by the
    4-day rule, with the held symbols its run failed (``held_failed``). ``k`` is the fire's
    local date in its job's timezone — a ``quotes_us`` fire judges New York's session ``k``, a
    ``quotes_tw`` fire Taipei's — so it is a TRADING day and need not be a period end: a
    Saturday month end D is settled on Friday's close. **Rule:** a held symbol ``s`` is
    unconfirmed at D when some key ``k`` with ``k ≤ D`` lists ``s`` and the price D is valued at
    is dated on or before ``k`` — that price is not newer than the close the failed run should
    have brought. A later confirmed close (dated after ``k``) clears it; a missing price is
    already ``missing_prices``.

    *carried* are the marks the previous generation stored for this close: a request covers
    only the fires its window examines (``job_runs`` may have been pruned since), so a mark
    stays while the close is still valued at the SAME price date — a recompute (a full run, the
    weekly self-check) never drops one the window no longer sees.
    """
    found: dict[str, date] = {}
    for k, symbols in keys.items():
        if k > day:
            continue
        for s in symbols:
            on = price_dates.get(s)
            if on is not None and on <= k and s not in found:
                found[s] = on
    for mark in carried:
        if mark.symbol in found or mark.price_date is None:
            continue
        if price_dates.get(mark.symbol) == mark.price_date:
            found[mark.symbol] = mark.price_date
    return [PriceDate(symbol=s, price_date=found[s]) for s in sorted(found)]


def kpi_at(day: date, *, book: Book, ledger: CloseLedger, conn_source: OnOrBefore,
           reporting: Currency, options_value_excluded: bool,
           unconfirmed: Mapping[date, Sequence[str]] | None = None,
           carried: Sequence[PriceDate] = ()) -> CloseKpi:
    """The KPI block of the close ending on *day* (§11.2) — since inception, as at *day*.

    *book* is the dashboard-path replay of ``ledger.bundle.valued_as_of(day)`` (the run's
    ``iter_books`` yields it); *conn_source* the :class:`OnOrBefore` source of *day*, built on
    the run's ONE :class:`~portfolio_dash.pricing.store.FxSeriesCache`;
    *options_value_excluded* the OR of the settled days' ``options_open`` over
    ``(previous end, day]``; *unconfirmed* ``RunRequest.unconfirmed_closes`` and *carried* the
    previous generation's marks for this close (:func:`unconfirmed_at`).

    KPI 總市值 is read exactly as the dashboard reads it: the stock grand subtotal plus the
    選擇權 block's own market value (``KpiBlock.market_value_with``). Every FX list is sorted
    by (base, quote): the resolver records pairs in an order that follows set iteration.
    """
    if conn_source.day != day:
        raise ValueError(f"the source is as at {conn_source.day}, the close at {day}")
    movements = valued_rows_as_of(ledger.movements, day)
    block = kpis_as_of(ledger.bundle, book=book, cash_movements=movements,
                       prices=conn_source, fx=conn_source, as_of=day, reporting=reporting)
    options = options_block_of(block, conn_source,
                               trend_excludes_value=options_value_excluded)
    reads = sorted(block.fx_reads.items(), key=lambda kv: (kv[0][0].value, kv[0][1].value))
    unconfirmed_dates = unconfirmed_at(
        day, {s: None if pr is None else pr.as_of for s, pr in block.price_reads.items()},
        unconfirmed or {}, carried)
    past_expiry: list[str] = []
    for h in past_expiry_holdings(block.book.holdings, block.bundle.instruments, day):
        if h.symbol not in past_expiry:
            past_expiry.append(h.symbol)
    notes = PeriodNotes(
        missing_prices=block.missing_prices,
        stale_prices=[PriceDate(symbol=s, price_date=pr.as_of)
                      for s, pr in block.price_reads.items() if pr is not None and pr.stale],
        unconfirmed_closes=unconfirmed_dates,
        missing_fx=block.missing_fx,
        stale_fx=[PairDate(pair=_pair(b, q), rate_date=r.as_of)
                  for (b, q), r in reads if r is not None and r.stale],
        pending_positions=pending_positions(block.book),
        options_value_excluded=options_value_excluded,
        options_past_expiry=past_expiry,
    )
    kpi = PeriodKpi(
        reporting_ccy=reporting,
        total_market_value=block.market_value_with(options),
        total_return=block.total_return,
        total_return_rate=block.total_return_rate,
        xirr=block.xirr,
        xirr_window_days=block.xirr_window_days,
        xirr_reason=block.xirr_reason,
        by_currency_value=dict(block.by_currency_value or {}),
        fx_used=[FxUsed(pair=_pair(b, q), rate=r.rate, as_of=r.as_of)
                 for (b, q), r in reads if r is not None],
        notes=notes,
    )
    return CloseKpi(kpi=kpi, block=block)


#: Why a close carries no trend figure or no 當期報酬 (zh, server-owned). A close with no trend
#: point for want of a rate reads the series' own sentence, whose ONE owner is
#: ``timeseries.trend_unavailable_reason`` (「2026-03-16 起缺少 MYR/TWD 匯率（當日或之前無匯率），
#: 走勢無法計算」); the callers hand it in (``period_twr(unavailable=)``,
#: ``with_trend(trend_reason=)``).
TWR_NO_ANCHOR_ZH = "尚無可計算的時間加權報酬（尚未有估值完整且市值為正的一日）"
TWR_END_INCOMPLETE_ZH = "期末估值不完整（缺價格或待釐清），當期報酬暫不計算"
TWR_START_INCOMPLETE_ZH = "期初估值不完整（缺價格或待釐清），當期報酬暫不計算"
TWR_START_UNAVAILABLE_ZH = "期初沒有走勢資料，當期報酬暫不計算"
#: A close before the series (the ledger's first rows are cash or conversions, its first trade
#: comes later): it has its cash and activity, and no trend point yet.
TREND_NOT_STARTED_ZH = "走勢自第一筆交易、股利或期初庫存起算，此日尚無走勢資料"
TWR_NOT_STARTED_ZH = "走勢尚未開始，當期報酬暫不計算"
#: The XIRR reason of a close before the first trade: no position yet (the engine's generic
#: reason names a missing price, which a month with no position does not have).
XIRR_NO_POSITION_ZH = "尚無持倉，XIRR 不計"


@dataclass(frozen=True)
class TwrIndex:
    """The full-series TWR index (``twr.twr_index`` over every day the run's series holds)."""

    values: Mapping[date, Decimal]
    #: The index's first day (100); ``None`` when no day is complete with a positive value.
    anchor: date | None


def period_twr(index: TwrIndex, points: Mapping[date, TrendPoint], *, start_end: date | None,
               end: date, unavailable: str | None) -> tuple[Decimal | None, str | None]:
    """``I(end) / I(start_end) − 1`` on the full-series index (§11.2 ``period_twr``), with the
    reason when it is ``None``.

    *start_end* is the previous period's end day (``None`` for the first period); a period that
    begins before the index's anchor starts from the anchor (100). ``None`` when either end day
    is incomplete — the index carries over an incomplete day, but a period return ending on one
    is not a measured return — or has no trend point at all: then the reason is *unavailable*,
    the series' own sentence (``timeseries.trend_unavailable_reason`` of its first
    unconvertible flow; ``None`` from a caller that holds every point). The stale-join rule
    (「待結算」, §11.2) is the read path's: it depends on the store at read time."""
    point = points.get(end)
    if point is None:
        return None, unavailable or trend_unavailable_reason(None)
    if index.anchor is None or end < index.anchor:
        return None, TWR_NO_ANCHOR_ZH
    if point.incomplete:
        return None, TWR_END_INCOMPLETE_ZH
    if start_end is None or start_end < index.anchor:
        base = index.values[index.anchor]
    else:
        start_point = points.get(start_end)
        if start_point is None:
            return None, TWR_START_UNAVAILABLE_ZH
        if start_point.incomplete:
            return None, TWR_START_INCOMPLETE_ZH
        base = index.values[start_end]
    return index.values[end] / base - _ONE, None


def kpi_incomplete(kpi: PeriodKpi) -> bool:
    """The KPI block itself is incomplete: a figure is blank, a held price or a rate is
    missing, or a position is 待釐清."""
    n = kpi.notes
    return (kpi.total_market_value is None or kpi.total_return is None
            or bool(n.missing_prices or n.missing_fx or n.pending_positions))


def with_trend(kpi: PeriodKpi, *, point: TrendPoint | None, twr: Decimal | None,
               twr_reason: str | None, trend_reason: str | None) -> PeriodKpi:
    """The KPI block with the settled day D's trend figures and the period's TWR.

    ``notes.incomplete`` is the settled day's own flag. A day with NO trend point carries no
    trend figure and says why in ``notes.trend_unavailable``: *trend_reason* — the series' own
    sentence when it is unavailable there (``timeseries.trend_unavailable_reason``, which names
    the first flow with no rate and its pair), :data:`TREND_NOT_STARTED_ZH` before the series
    starts, ``None`` from a caller that holds every point. Its ``incomplete`` then states the
    KPIs' own completeness (:func:`kpi_incomplete`) — a missing trend is not a missing price."""
    notes = kpi.notes.model_copy(update={
        "incomplete": kpi_incomplete(kpi) if point is None else point.incomplete,
        "trend_unavailable": (trend_reason or trend_unavailable_reason(None)) if point is None
        else None})
    return kpi.model_copy(update={
        "trend_total_value": None if point is None else point.total_value,
        "net_invested": None if point is None else point.net_invested,
        "net_worth": None if point is None else point.net_worth,
        "period_twr": twr,
        "period_twr_reason": twr_reason,
        "notes": notes,
    })


# ---------------------------------------------------------------------------------------------
# Holdings and cash at D
# ---------------------------------------------------------------------------------------------

_FLAG_FIELDS: tuple[str, ...] = ("oversold", "short_open", "unbookable_dividend",
                                 "unbookable_action", "unbookable_close")


def holdings_at(block: KpiBlock) -> list[PeriodHolding]:
    """Every position of the close's book at D, stocks and options both, tagged by
    ``asset_type`` (OPT-D5 keeps them apart wherever they are listed): native currency, the
    price the valuation used and its date (a stock's stored close; a contract's last trade),
    market value and unrealized as the valuation left them (``None`` on a 待釐清 position), and
    the 待釐清 flags that are set."""
    out: list[PeriodHolding] = []
    for h in block.valued:
        is_option = h.symbol in block.option_symbols
        price: Decimal | None = h.market_price
        price_as_of: date | None = None
        if is_option:
            # the 選擇權 block's rule (``options._open_row``): a 待釐清 contract has no price
            last = block.last_trades.get(h.symbol)
            priced = last if not (h.oversold or h.unbookable_close) else None
            price = None if priced is None else priced.price
            price_as_of = None if priced is None else priced.on
        else:
            # the holdings row's rule: the date of the price read, whatever the valuation did
            read = block.price_reads.get(h.symbol)
            price_as_of = None if read is None else read.as_of
        out.append(PeriodHolding(
            account_id=h.account_id, symbol=h.symbol,
            asset_type=ASSET_OPTION if is_option else ASSET_STOCK, quote_ccy=h.quote_ccy,
            shares=h.shares, original_cost_total=h.original_cost_total,
            adjusted_cost_total=h.adjusted_cost_total, price=price,
            price_as_of=price_as_of, market_value=h.market_value,
            unrealized=h.unrealized_pnl,
            flags={f: True for f in _FLAG_FIELDS if getattr(h, f)}))
    return out


def cash_at(day: date, *, book: Book, ledger: CloseLedger, fx_cache: FxSeriesCache,
            reporting: Currency) -> list[PeriodCash]:
    """Every cash pool's end-of-day balance at *day* (§11.3 "cash at D") and its reporting
    value at *day*'s rate — ``cash_balances(…, as_of=day)`` fed the BOOKED dividends only
    (*book*'s refusals at *day*, as the dashboard feeds it), so a refused payout never lands in
    month-end cash. The reporting pool needs no rate."""
    divs = booked_dividends(ledger.dividends, book.refused_dividends)
    balances = cash_balances(ledger.movements, ledger.conversions, ledger.transactions, divs,
                             ledger.bundle.instruments, as_of=day)
    out: list[PeriodCash] = []
    for (account_id, ccy), balance in sorted(balances.items(),
                                             key=lambda kv: (kv[0][0], kv[0][1].value)):
        if ccy == reporting:
            out.append(PeriodCash(account_id=account_id, ccy=ccy, balance=balance,
                                  reporting_value=balance))
            continue
        read = rate_read(fx_cache, reporting, day, ccy)
        if read is None:
            # an empty pool is worth nothing in any currency and needs no rate (the cash
            # series skips it the same way); a non-empty one has no reporting value
            out.append(PeriodCash(account_id=account_id, ccy=ccy, balance=balance,
                                  reporting_value=balance if balance == _ZERO else None))
            continue
        out.append(PeriodCash(
            account_id=account_id, ccy=ccy, balance=balance,
            reporting_value=convert(balance, read.rate), fx_rate=read.rate,
            fx_as_of=read.as_of))
    return out


def missing_cash_fx(cash: Sequence[PeriodCash], reporting: Currency) -> list[str]:
    """The pairs of the cash pools at D with no rate to *reporting* (``reporting_value`` None),
    sorted — ``notes.missing_cash_fx``."""
    return sorted({f"{c.ccy.value}/{reporting.value}" for c in cash
                   if c.reporting_value is None})


# ---------------------------------------------------------------------------------------------
# Period activity (§11.3)
# ---------------------------------------------------------------------------------------------


class _DividendAcc:
    """One (type, account, currency) dividend line being summed."""

    __slots__ = ("gross", "net", "rows", "shares", "withholding")

    def __init__(self, ccy: Currency) -> None:
        self.rows = 0
        self.gross, self.withholding, self.net = _Money(ccy), _Money(ccy), _Money(ccy)
        self.shares = _ZERO


def activity(*, start: date, end: date, book: Book, ledger: CloseLedger,
             reporting: Currency, rate_at: RateAt) -> PeriodActivity:
    """The period's activity (§11.3): every line in native currency and in the reporting
    currency at each row's own date's FX (:func:`own_date_rates`), rows counted inside
    ``[start, end]`` by the dates the ledger cut uses. The lines are NOT additive — under the
    adjusted-cost model a sale's realized already holds the dividends folded into its cost —
    and no total of them is formed.

    *book* is the close's book at *end* (its realized rows and refusals are the replay's own;
    a row dated after *end* is not in it). Groups keep the order of their first row.
    """
    del reporting  # each line's reporting twin is decided by *rate_at*
    bundle = ledger.bundle.valued_as_of(end)
    instruments = bundle.instruments

    def asset_type(symbol: str) -> str:
        inst = instruments.get(symbol)
        return ASSET_OPTION if inst is not None and inst.is_option else ASSET_STOCK

    # --- realized: sales and short covers (vs adjusted and vs original), post-close dividends
    realized: dict[tuple[str, str, Currency], tuple[_Money, _Money | None, list[int]]] = {}
    for kind, rows in (("trade", realized_rows(book.realized.rows, start=start, end=end)),
                       ("dividend_after_close",
                        post_close_dividend_rows(book.realized.rows, start=start, end=end))):
        for r in rows:
            key = (kind, asset_type(r.symbol), r.quote_ccy)
            if key not in realized:
                realized[key] = (_Money(r.quote_ccy),
                                 _Money(r.quote_ccy) if kind == "trade" else None, [0])
            adjusted, original, count = realized[key]
            rate = rate_at(r.sell_date, r.quote_ccy)
            adjusted.add(r.realized, rate)
            if original is not None:
                original.add(r.proceeds_net - r.original_cost_removed, rate)
            count[0] += 1
    realized_lines = [
        RealizedLine.model_validate({
            "kind": kind, "asset_type": atype, "rows": count[0],
            "realized": adjusted.money(),
            "capital_gain": None if original is None else original.money()})
        for (kind, atype, _ccy), (adjusted, original, count) in realized.items()
    ]

    # --- dividends: booked rows only, by type, account and currency
    booked = booked_dividends(bundle.dividends, book.refused_dividends)
    dividends: dict[tuple[DividendType, str, Currency], _DividendAcc] = {}
    for d in booked:
        if not _inside(d.effective_date, start, end):
            continue
        ccy = instruments[d.symbol].quote_ccy
        key = (d.type, d.account_id, ccy)
        if key not in dividends:
            dividends[key] = _DividendAcc(ccy)
        acc = dividends[key]
        acc.rows += 1
        if d.type is not DividendType.STOCK:  # a 配股 is shares only, no amount
            rate = rate_at(d.date, ccy)
            acc.gross.add(d.gross, rate)
            acc.withholding.add(d.withholding, rate)
            acc.net.add(d.net, rate)
        if d.type in (DividendType.DRIP, DividendType.STOCK) and d.reinvest_shares is not None:
            acc.shares += d.reinvest_shares
    dividend_lines: list[DividendLine] = []
    for (dtype, account_id, _ccy), acc in dividends.items():
        is_stock = dtype is DividendType.STOCK
        dividend_lines.append(DividendLine.model_validate({
            "type": dtype.value, "account_id": account_id, "rows": acc.rows,
            "gross": None if is_stock else acc.gross.money(),
            "withholding": None if is_stock else acc.withholding.money(),
            "net": None if is_stock else acc.net.money(),
            "reinvested_shares": acc.shares if dtype is DividendType.DRIP else None,
            "stock_shares": acc.shares if is_stock else None,
        }))

    # --- dividends not booked: the refused payouts of the period (未入帳（待釐清）)
    refused: dict[Currency, tuple[_Money, list[int]]] = {}
    for d in book.refused_dividends:
        if not _inside(d.effective_date, start, end):
            continue
        ccy = instruments[d.symbol].quote_ccy
        if ccy not in refused:
            refused[ccy] = (_Money(ccy), [0])
        money, count = refused[ccy]
        money.add(d.net, rate_at(d.date, ccy))
        count[0] += 1
    not_booked = [NotBookedLine(rows=count[0], net=money.money())
                  for money, count in refused.values()]

    # --- trade fees and tax, by account and currency
    fees: dict[tuple[str, Currency], tuple[_Money, _Money]] = {}
    for t in bundle.transactions:
        if not _inside(t.trade_date, start, end):
            continue
        ccy = instruments[t.symbol].quote_ccy
        fee_key = (t.account_id, ccy)
        if fee_key not in fees:
            fees[fee_key] = (_Money(ccy), _Money(ccy))
        rate = rate_at(t.trade_date, ccy)
        fees[fee_key][0].add(t.fees, rate)
        fees[fee_key][1].add(t.tax, rate)
    fee_lines = [FeeLine(account_id=account_id, fees=f.money(), tax=x.money())
                 for (account_id, _ccy), (f, x) in fees.items()]

    # --- every cash-movement kind, by account and currency
    moves: dict[tuple[str, str, Currency], tuple[_Money, list[int]]] = {}
    for m in ledger.movements:
        if not _inside(m.date, start, end):
            continue
        move_key = (m.account_id, m.kind.upper(), m.ccy)
        if move_key not in moves:
            moves[move_key] = (_Money(m.ccy), [0])
        moves[move_key][0].add(m.amount, rate_at(m.date, m.ccy))
        moves[move_key][1][0] += 1
    move_lines = [CashMovementLine(account_id=account_id, kind=kind, rows=count[0],
                                   amount=money.money())
                  for (account_id, kind, _ccy), (money, count) in moves.items()]

    # --- FX conversions (from / to amounts) and realized FX (the 換匯損益 owner)
    convs = [FXConversion(account_id=c.account_id, date=c.date, from_ccy=c.from_ccy,
                          from_amount=c.from_amount, to_ccy=c.to_ccy, to_amount=c.to_amount)
             for c in valued_rows_as_of(ledger.conversions, end)]
    conv_groups: dict[tuple[str, Currency, Currency], tuple[_Money, _Money, list[int]]] = {}
    for c in convs:
        if not _inside(c.date, start, end):
            continue
        conv_key = (c.account_id, c.from_ccy, c.to_ccy)
        if conv_key not in conv_groups:
            conv_groups[conv_key] = (_Money(c.from_ccy), _Money(c.to_ccy), [0])
        src, dst, count = conv_groups[conv_key]
        src.add(c.from_amount, rate_at(c.date, c.from_ccy))
        dst.add(c.to_amount, rate_at(c.date, c.to_ccy))
        count[0] += 1
    conv_lines = [FxConversionLine(account_id=ck[0], rows=count[0], from_amount=src.money(),
                                   to_amount=dst.money())
                  for ck, (src, dst, count) in conv_groups.items()]
    fx_groups: dict[tuple[str, Currency], _Money] = {}
    for item in fx_realized_rows(ledger.accounts, convs,
                                 valued_rows_as_of(ledger.movements, end),
                                 start=start, end=end):
        fr = item.row
        key2 = (item.account_id, fr.home_ccy)
        if key2 not in fx_groups:
            fx_groups[key2] = _Money(fr.home_ccy)
        fx_groups[key2].add(fr.realized, rate_at(fr.date, fr.home_ccy))
    fx_lines = [FxRealizedLine(account_id=account_id, realized=money.money())
                for (account_id, _ccy), money in fx_groups.items()]

    # --- opening inventory built in the period
    opening_lines: list[OpeningLine] = []
    for o in bundle.opening:
        if not _inside(o.build_date, start, end):
            continue
        ccy = instruments[o.symbol].quote_ccy
        money = _Money(ccy)
        money.add(o.original_cost_total, rate_at(o.build_date, ccy))
        opening_lines.append(OpeningLine(account_id=o.account_id, symbol=o.symbol,
                                         shares=o.shares, original_cost_total=money.money()))

    return PeriodActivity(realized=realized_lines, dividends=dividend_lines,
                          dividends_not_booked=not_booked, fees=fee_lines,
                          cash_movements=move_lines, fx_conversions=conv_lines,
                          fx_realized=fx_lines, openings=opening_lines)


def period_start(kind: str, end: date) -> date:
    """The first calendar day of the month (``M``) or year (``Y``) ending on *end*."""
    return date(end.year, end.month, 1) if kind == "M" else date(end.year, 1, 1)



__all__ = [
    "CAPITAL_KINDS",
    "TREND_NOT_STARTED_ZH",
    "TWR_END_INCOMPLETE_ZH",
    "TWR_NOT_STARTED_ZH",
    "XIRR_NO_POSITION_ZH",
    "TWR_NO_ANCHOR_ZH",
    "TWR_START_INCOMPLETE_ZH",
    "TWR_START_UNAVAILABLE_ZH",
    "AccountFxRealized",
    "CloseKpi",
    "CloseLedger",
    "RateAt",
    "RateRead",
    "TwrIndex",
    "activity",
    "cash_at",
    "dividend_rows",
    "fx_realized_rows",
    "holdings_at",
    "kpi_at",
    "kpi_incomplete",
    "missing_cash_fx",
    "own_date_rates",
    "pending_positions",
    "period_start",
    "period_twr",
    "post_close_dividend_rows",
    "rate_read",
    "realized_rows",
    "unconfirmed_at",
    "with_trend",
]
