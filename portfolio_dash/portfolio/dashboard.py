"""The orchestration combiner: assemble one complete DashboardData from SQLite.

Read-only — never fetches. Reads ledgers (data_ingestion), prices/FX (pricing),
calls the calculation cores (portfolio, forex), and assembles the dashboard
contract. Degrades honestly: blended figures become None with freshness reasons;
it never fabricates and never raises on missing market data.

This module introduces the one-way edge ``portfolio -> forex`` (recorded in the
2026-06-10 dashboard-combiner spec); forex imports only ``shared``, so no cycle.
"""

import sqlite3
from collections import defaultdict
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal

from portfolio_dash.data_ingestion.store import (
    list_accounts,
    list_cash_movements,
    list_dividends,
    list_fx_conversions,
    list_instruments,
    list_opening,
    list_transactions,
)
from portfolio_dash.forex.fx_pnl import compute_fx_summary
from portfolio_dash.forex.results import FXSummary
from portfolio_dash.portfolio.allocation import combined_view, sector_allocation
from portfolio_dash.portfolio.cash import cash_balances
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.portfolio.dashboard_models import (
    DashboardData,
    DividendProjection,
    DividendSummary,
    DividendYearRow,
    ExDividendItem,
    FreshnessReport,
    FxFreshness,
    HoldingRow,
    HoldingSubtotal,
    KpiSummary,
    PriceFreshness,
    TrendSeries,
)
from portfolio_dash.portfolio.dividends import project_dividends
from portfolio_dash.portfolio.networth import compose_net_worth, daily_cash_series
from portfolio_dash.portfolio.pnl import value_holdings
from portfolio_dash.portfolio.results import CombinedView, ReturnSummary, SectorAllocation
from portfolio_dash.portfolio.returns import total_return, xirr_reporting
from portfolio_dash.portfolio.timeseries import FxHistory, PriceHistory, daily_value_series
from portfolio_dash.pricing.results import FxRead, PriceRead
from portfolio_dash.pricing.store import (
    get_dividend_events,
    get_fx,
    get_fx_history,
    get_fx_on,
    get_latest_price,
    get_price_history,
)
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.fx import convert
from portfolio_dash.shared.models.enums import DividendType
from portfolio_dash.shared.models.ledger import (
    Dividend,
    FXConversion,
    OpeningInventory,
    Transaction,
)
from portfolio_dash.shared.sectors import canonical_sector

_ZERO = Decimal("0")
_ONE = Decimal("1")
# History reads start here: stored prices may predate the first ledger event.
_EPOCH = date(1900, 1, 1)


class RateResolver:
    """Current-FX lookup: identity -> direct pair -> inverted pair -> KeyError.

    Records every requested pair (found or not) for the freshness report.
    """

    def __init__(self, conn: sqlite3.Connection, *, now: datetime) -> None:
        self._conn = conn
        self._now = now
        self.reads: dict[tuple[Currency, Currency], FxRead | None] = {}

    def _read(self, base: Currency, quote: Currency) -> FxRead | None:
        direct = get_fx(self._conn, base, quote, now=self._now)
        if direct is not None:
            return direct
        inverse = get_fx(self._conn, quote, base, now=self._now)
        if inverse is not None:
            return FxRead(rate=_ONE / inverse.rate, as_of=inverse.as_of,
                          source=inverse.source, stale=inverse.stale)
        return None

    def rate(self, base: Currency, quote: Currency) -> Decimal:
        if base == quote:
            return _ONE
        key = (base, quote)
        if key not in self.reads:
            self.reads[key] = self._read(base, quote)
        read = self.reads[key]
        if read is None:
            raise KeyError(f"no FX rate stored for {base.value}/{quote.value}")
        return read.rate


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


def build_dashboard(
    conn: sqlite3.Connection, *, now: datetime, reporting: Currency
) -> DashboardData:
    """Assemble the complete dashboard data model from SQLite (read-only)."""
    as_of = now.date()

    # 1. Ledgers and reference data (Stored* rows -> ledger models).
    txs = [
        Transaction(account_id=s.account_id, symbol=s.symbol, side=s.side,
                    quantity=s.quantity, price=s.price, fees=s.fees, tax=s.tax,
                    trade_date=s.trade_date)
        for s in list_transactions(conn)
    ]
    divs = [
        Dividend(account_id=s.account_id, symbol=s.symbol, date=s.date,
                 type=DividendType(s.type), gross=s.gross, withholding=s.withholding,
                 net=s.net, reinvest_shares=s.reinvest_shares,
                 reinvest_price=s.reinvest_price)
        for s in list_dividends(conn)
    ]
    convs = [
        FXConversion(account_id=s.account_id, date=s.date, from_ccy=s.from_ccy,
                     from_amount=s.from_amount, to_ccy=s.to_ccy, to_amount=s.to_amount)
        for s in list_fx_conversions(conn)
    ]
    opening = [
        OpeningInventory(account_id=s.account_id, symbol=s.symbol, shares=s.shares,
                         original_cost_total=s.original_cost_total,
                         build_date=s.build_date)
        for s in list_opening(conn)
    ]
    instruments = {i.symbol: i for i in list_instruments(conn)}
    accounts = {a.account_id: a for a in list_accounts(conn)}

    # 1b. Unregistered-symbol guard (2026-07-02): a ledger row whose symbol has no
    # Instrument row has no quote currency — it cannot be booked, valued, or priced.
    # NEVER crash the dashboard over it: exclude those events from ALL computation
    # (book, XIRR, trend, dividends — consistently) and surface the symbols in
    # freshness.unregistered_symbols so the UI can tell the user exactly how to fix it
    # (register the symbol, then the next build includes the rows). Same degradation
    # philosophy as the oversold (賣超) path.
    ledger_syms = ({t.symbol for t in txs} | {d.symbol for d in divs}
                   | {o.symbol for o in opening})
    unregistered = sorted(ledger_syms - instruments.keys())
    if unregistered:
        skip = set(unregistered)
        txs = [t for t in txs if t.symbol not in skip]
        divs = [d for d in divs if d.symbol not in skip]
        opening = [o for o in opening if o.symbol not in skip]

    # 2. Book and valuation. allow_oversell: an acked oversell must not crash the
    # dashboard — it degrades to a flagged 賣超 holding (待釐清) instead (see build_book).
    book = build_book(txs, divs, opening, instruments, allow_oversell=True)
    has_oversold = any(h.oversold for h in book.holdings)
    held_symbols = sorted({h.symbol for h in book.holdings})
    price_reads: dict[str, PriceRead | None] = {
        sym: get_latest_price(conn, sym, now=now) for sym in held_symbols
    }
    price_map = {sym: pr.value for sym, pr in price_reads.items() if pr is not None}
    valued = value_holdings(book.holdings, price_map)

    resolver = RateResolver(conn, now=now)

    # 3. Core summaries — each degrades to None on a missing current rate.
    returns: ReturnSummary | None
    try:
        returns = total_return(book, valued, resolver.rate, reporting)
    except KeyError:
        returns = None
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
        allocation = sector_allocation(valued, alloc_instruments, resolver.rate, reporting)
    except KeyError:
        allocation = None
    view: CombinedView | None
    try:
        view = combined_view(valued, resolver.rate, reporting)
    except KeyError:
        view = None

    # 4. XIRR — on-or-before trade-date FX; degrades to None with a reason.
    def fx_at(d: date, base: Currency, quote: Currency) -> Decimal:
        if base == quote:
            return _ONE
        direct = get_fx_on(conn, base, quote, on=d)
        if direct is not None:
            return direct.rate
        inverse = get_fx_on(conn, quote, base, on=d)
        if inverse is not None:
            return _ONE / inverse.rate
        raise KeyError(
            f"no FX rate stored on or before {d.isoformat()} "
            f"for {base.value}/{quote.value}"
        )

    xirr_value: Decimal | None = None
    xirr_window_days: int | None = None
    xirr_reason: str | None = None
    if has_oversold:
        # An oversold (賣超) position has no honest terminal value -> XIRR is not computable.
        xirr_reason = "ledger has an oversold (賣超) position — 待釐清"
    else:
        try:
            outcome = xirr_reporting(txs, divs, opening, valued, instruments, fx_at,
                                     price_map, resolver.rate, as_of, reporting)
            xirr_value = outcome.rate
            xirr_window_days = outcome.window_days
        except KeyError as exc:
            xirr_reason = str(exc).strip("'\"")
    if xirr_value is None and xirr_reason is None:
        xirr_reason = ("not computable (missing current price, no sign change, "
                       "or non-convergence)")

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
    try:
        fx_summary = compute_fx_summary(accounts, instruments, txs, divs, convs,
                                        exposure, resolver.rate, reporting)
    except KeyError:
        fx_summary = None

    # 6. Dividend summary — cash actually received (incl. DRIP net), native ccy.
    # ttm_cutoff bounds the trailing-12-month window (display-only attribution).
    ttm_cutoff = as_of - timedelta(days=365)
    year_ccy: dict[int, dict[Currency, Decimal]] = {}
    total_ccy: dict[Currency, Decimal] = {}
    ttm_ccy: dict[Currency, Decimal] = {}
    for dv in divs:
        if dv.type is DividendType.STOCK:
            continue  # 配股 adds shares, not cash
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
        valued, calendar, accounts, instruments, year=as_of.year
    )

    # 8. Holding rows — enrichment + weight; age-based staleness overrides
    # value_holdings' presence-based flag.
    total_value = view.reporting_total_value if view is not None else None
    holding_rows: list[HoldingRow] = []
    for h in valued:
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
        data = h.model_dump()
        data.update(
            account_name=acct.name, name=inst.name, market=inst.market,
            sector=inst.sector, board=inst.board,
            price_as_of=pr.as_of if pr is not None else None,
            price_stale=pr.stale if pr is not None else True,
            weight=weight,
        )
        holding_rows.append(HoldingRow(**data))

    # 9. Trend — bulk-load histories, then the pure daily replay.
    trend_reason: str | None = None
    if txs or divs or opening:
        ledger_symbols = sorted({t.symbol for t in txs} | {d.symbol for d in divs}
                                | {o.symbol for o in opening})
        price_history: PriceHistory = {
            sym: [(p.as_of, p.value) for p in get_price_history(conn, sym, _EPOCH, as_of)]
            for sym in ledger_symbols
        }
        fx_history: FxHistory = {}
        for ccy in {instruments[sym].quote_ccy for sym in ledger_symbols}:
            if ccy == reporting:
                continue
            for base, quote in ((ccy, reporting), (reporting, ccy)):
                rows = get_fx_history(conn, base, quote, _EPOCH, as_of)
                if rows:
                    fx_history[(base, quote)] = [(r.as_of, r.rate) for r in rows]
        trend = daily_value_series(txs, divs, opening, instruments, price_history,
                                   fx_history, reporting, end=as_of)
        if not trend.available:
            trend_reason = "missing FX history for a ledger flow date"
        else:
            # 9b. Total net worth (FU-D29 / deferred C8): compose the UNCHANGED trend
            # with a daily cash series. Cash is read from the SAME Stored rows the 資金管理
            # /api/cash view uses (unregistered symbols skipped inside cash_balances,
            # exactly as there), converted at carry-forward FX per day. Display-only
            # attribution — no money-of-record path is touched.
            cash_movements = list_cash_movements(conn)
            cash_txs = list_transactions(conn)
            cash_divs = list_dividends(conn)
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
                    rows = get_fx_history(conn, base, quote, _EPOCH, as_of)
                    if rows:
                        cash_fx_history[(base, quote)] = [(r.as_of, r.rate) for r in rows]
            cash_by_date = daily_cash_series(
                cash_movements, cash_convs, cash_txs, cash_divs, instruments,
                cash_fx_history, reporting, end=as_of)
            trend = compose_net_worth(trend, cash_by_date)
    else:
        trend = TrendSeries(points=[], reporting_currency=reporting, available=False)
        trend_reason = "no ledger events"

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
    kpis = KpiSummary(
        reporting_currency=reporting,
        total_market_value=total_value,
        total_return=total_return_blended,
        total_return_rate=total_return_rate,
        realized_total=realized_total,
        unrealized_total=unrealized_total,
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
                    stale=read.stale if read is not None else True)
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
        unregistered_symbols=unregistered,
    )

    return DashboardData(
        as_of=now,
        reporting_currency=reporting,
        kpis=kpis,
        holdings=holding_rows,
        holdings_subtotals=holdings_subtotals,
        realized=book.realized,
        returns=returns,
        allocation=allocation,
        currency_view=view,
        fx=fx_summary,
        dividends=dividend_summary,
        ex_dividend_calendar=calendar,
        trend=trend,
        freshness=freshness,
        insights=[],
        dividend_projection=dividend_projection,
    )
