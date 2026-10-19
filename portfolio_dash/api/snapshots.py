"""月度成績 / 年度成績 — month and year closes, served (PERF spec 2026-10-18 §11.4, §14.6; plan
§7.1, package G).

Three readers share ONE rule for which close is served from where:

- ``GET /api/snapshots`` — the month rows of 月度成績 (:func:`snapshot_rows`): the row fields
  of the retired 23:50 snapshot (``month, as_of, reporting_ccy, total_value, total_return,
  total_return_rate, xirr, by_currency``) plus the additive ``kind``, ``provisional``,
  ``stale``, ``notes``, ``xirr_reason``, ``period_twr``, ``period_twr_reason`` and
  ``restated``. MONTHS ONLY — a year row must never crowd a month out of ``limit=12``;
- ``GET /api/settlement/periods?kind=M|Y`` — the closes themselves (:func:`period_rows`);
- ``GET /api/settlement/periods/{kind}/{period}`` — one close with its holdings and cash at
  its day (:func:`period_detail`).

**Which close comes from where** (§5, §10, §11.4). Every figure is since inception AS AT the
period's end day D — a year row is never that year's own return (當期報酬（TWR） is):

- the CURRENT month / year is live, as at today (``as_of`` = now; ``end_day`` = today);
- a past period whose D lies before the dashboard build's ``history_state.live_from`` is the
  published close (``settled_period``) — the same day the trend switches from stored to live,
  so a row and the trend never show two versions of one day;
- every other past period is computed live with the SAME ``period_close`` functions the
  settlement worker runs (``kpis_as_of`` with "on or before D" sources, the cash at D, the
  activity, the trend point and the period's TWR from the composed series) — so a correction
  dated last month changes last month's row at once. ``PD_SETTLE=off`` or an unusable store:
  every past period is live — slower, never stale, never missing.

**Computed when the close is READ** (§11.2): ``notes.dividends_receivable`` (hidden on a stale
row, which would mix today's ledger with the previous generation's KPIs); 待結算 — a
``period_twr`` of ``None`` while history is stale for any period whose ``(previous end, D]``
contains the first live day or overlaps the stale range; ``stale`` (更新前版本); ``restated``
(已重述 / 上線重述, every ``settle_restatement`` row of the close); ``provisional`` (暫定: D after
``settlement_service.final_through``, the same rule that caps the nightly run — it depends on
the price data, never on the store).

The 23:50 snapshot (``portfolio_snapshots``) is neither read nor served here any more: it stays
in the database as the record of the old frozen values (and the old side of the go-live
restatement, ``settlement_store.cutover_snapshots``). :func:`ensure_table`,
:func:`write_snapshot` and :func:`snapshot_job` remain only for the boot DDL and the nightly
runner ``api/app.py`` still registers; the main session retires that runner (plan W4).

Orchestration only: every number comes from ``portfolio/`` (``period_close``,
``settlement_store``, ``dashboard``); this module selects, labels and serialises.
"""

from __future__ import annotations

import copy
import json
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal

from portfolio_dash.api.routers.cash import CashLedgers, load_cash_ledgers
from portfolio_dash.api.settlement_service import SINCE_GO_LIVE, FinalThrough, final_through
from portfolio_dash.data_ingestion.store import list_accounts, load_ledger_bundle
from portfolio_dash.portfolio import period_close as pc
from portfolio_dash.portfolio import settlement_store as st
from portfolio_dash.portfolio.cost_basis import iter_books
from portfolio_dash.portfolio.dashboard import OnOrBefore, build_dashboard, build_dashboard_keyed
from portfolio_dash.portfolio.dashboard_models import DashboardData, HistoryState, TrendPoint
from portfolio_dash.portfolio.results import Book
from portfolio_dash.portfolio.settlement_models import (
    PeriodCash,
    PeriodClose,
    PeriodDetail,
    PeriodHolding,
    PeriodKpi,
    PriceDate,
    Restatement,
)
from portfolio_dash.portfolio.timeseries import holds_open_option, trend_book_days, trend_start
from portfolio_dash.portfolio.twr import twr_index
from portfolio_dash.pricing.store import FxSeriesCache
from portfolio_dash.shared import flags, result_cache
from portfolio_dash.shared.config import get_settings
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.ledger import Dividend, LedgerBundle
from portfolio_dash.shared.wire import decimal_str

Kind = Literal["M", "Y"]

_ONE_DAY = timedelta(days=1)

#: The market a quote job prices (``scheduler/jobs.py::QUOTE_JOB_IDS``), in the words of a 暫定
#: note: 「月底收盤價尚未確定：美股報價工作未完成」.
_QUOTE_JOB_MARKET_ZH: dict[str, str] = {
    "quotes_tw": "台股", "quotes_us": "美股", "quotes_my": "馬股",
}


# =============================================================================================
# The 23:50 snapshot (retired as a source; kept for the boot DDL and the W4 runner removal)
# =============================================================================================

_DDL = """
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    month TEXT PRIMARY KEY,
    as_of TEXT NOT NULL,
    reporting_ccy TEXT NOT NULL,
    total_value TEXT,
    total_return TEXT,
    total_return_rate TEXT,
    xirr TEXT,
    by_currency TEXT NOT NULL
);
"""


def ensure_table(conn: sqlite3.Connection) -> None:
    """The retired snapshot table — created at boot only (``api/app.py``), never on a read."""
    conn.executescript(_DDL)
    conn.commit()


def write_snapshot(conn: sqlite3.Connection, *, now: datetime) -> str:
    """The retired 23:50 freeze: upsert the current month's dashboard KPIs.

    Nothing reads these rows any more (``GET /api/snapshots`` serves the closes); the nightly
    runner that calls this is retired by the main session at plan W4. Optional KPIs store
    NULL — honest degradation, never fabricated. Returns a short run summary.
    """
    ensure_table(conn)
    reporting = get_settings().reporting_currency
    data = build_dashboard(conn, now=now, reporting=reporting)
    k = data.kpis
    month = now.strftime("%Y-%m")
    by_ccy = {
        ccy.value: decimal_str(v)
        for ccy, v in data.currency_view.by_currency_value.items()
    } if data.currency_view is not None else {}
    conn.execute(
        "INSERT INTO portfolio_snapshots (month, as_of, reporting_ccy, total_value, "
        "total_return, total_return_rate, xirr, by_currency) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(month) DO UPDATE SET as_of=excluded.as_of, "
        "reporting_ccy=excluded.reporting_ccy, total_value=excluded.total_value, "
        "total_return=excluded.total_return, "
        "total_return_rate=excluded.total_return_rate, xirr=excluded.xirr, "
        "by_currency=excluded.by_currency",
        (
            month,
            now.isoformat(),
            reporting.value,
            decimal_str(k.total_market_value) if k.total_market_value is not None else None,
            decimal_str(k.total_return) if k.total_return is not None else None,
            decimal_str(k.total_return_rate) if k.total_return_rate is not None else None,
            decimal_str(k.xirr) if k.xirr is not None else None,
            json.dumps(by_ccy),
        ),
    )
    conn.commit()
    return f"快照已寫入 {month}"


def snapshot_job(conn: sqlite3.Connection, *, now: datetime) -> str:
    """The retired nightly runner (``snapshot_monthly``) — removed from the registry at W4."""
    return write_snapshot(conn, now=now)


# =============================================================================================
# Periods
# =============================================================================================


@dataclass(frozen=True)
class _Period:
    kind: Kind
    period: str
    #: Its first calendar day.
    start: date
    #: Its last calendar day.
    end: date
    #: The period holding today: live as at today, never a close.
    current: bool

    def as_at(self, today: date) -> date:
        """The day its figures are as at: its end, or today for the current period."""
        return today if self.current else self.end


def _periods(first: date, today: date, kind: Kind) -> list[_Period]:
    """Every period from the one holding *first* to the one holding *today*, newest first."""
    out: list[_Period] = []
    if kind == "M":
        y, m = first.year, first.month
        while (y, m) <= (today.year, today.month):
            label = f"{y:04d}-{m:02d}"
            out.append(_Period("M", label, date(y, m, 1), st.period_end("M", label),
                               (y, m) == (today.year, today.month)))
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    else:
        for y in range(first.year, today.year + 1):
            out.append(_Period("Y", f"{y:04d}", date(y, 1, 1), date(y, 12, 31),
                               y == today.year))
    out.reverse()
    return out


def parse_period(kind: str, period: str) -> tuple[Kind, str] | None:
    """``M`` + ``YYYY-MM`` or ``Y`` + ``YYYY``, normalised; ``None`` when malformed."""
    try:
        if kind == "M" and len(period) == 7 and period[4] == "-":
            start = date(int(period[:4]), int(period[5:]), 1)
            return "M", f"{start:%Y-%m}"
        if kind == "Y" and len(period) == 4 and period.isdigit():
            return "Y", f"{int(period):04d}"
    except ValueError:
        return None
    return None


# =============================================================================================
# The served closes
# =============================================================================================


@dataclass
class _Served:
    """One close as served, before its per-request label (暫定)."""

    period: _Period
    close: PeriodClose
    holdings: list[PeriodHolding] | None = None
    cash: list[PeriodCash] | None = None
    restatements: list[Restatement] = field(default_factory=list)


@dataclass(frozen=True)
class _Inputs:
    """What this request's closes read, from ONE read snapshot: the build, the ledger."""

    data: DashboardData
    history: HistoryState
    registered: LedgerBundle
    cut: LedgerBundle
    ledgers: CashLedgers
    #: The ledger's first row counting by today — where the first close's period starts.
    first_row: date | None


def _store_view(history: HistoryState) -> tuple[Any, ...] | None:
    if history.generation == 0:
        return None
    return (history.generation, history.settled_through, history.held_through,
            history.dirty_day, history.seq)


def _state_view(conn: sqlite3.Connection, reporting: Currency) -> tuple[Any, ...] | None:
    if not flags.settle_enabled():
        return None
    state = st.store_state(conn, reporting=reporting)
    if not state.usable:
        return None
    return (state.generation, state.settled_through, state.held_through, state.dirty_day,
            state.seq)


def _inputs(conn: sqlite3.Connection, data: DashboardData, *, now: datetime,
            reporting: Currency) -> _Inputs:
    """Read inside this request's ONE read snapshot. *data* is the (cached) build taken before
    it; when the store this snapshot sees is not the one the build saw (a run published, a
    change landed in between) the build is redone inside the snapshot — uncached, consistent.
    The pool ledgers come through the cash API's one reviewed read (``load_cash_ledgers``,
    ⑪); a live close's cash at D re-applies its own book's refusals (``cash_at``)."""
    if _store_view(data.history_state) != _state_view(conn, reporting):
        data = build_dashboard(conn, now=now, reporting=reporting)
    today = now.date()
    raw = load_ledger_bundle(conn)
    registered = raw.without_unregistered() if raw.unregistered_symbols else raw
    cut = registered.valued_as_of(today)
    ledgers = load_cash_ledgers(conn)
    return _Inputs(data=data, history=data.history_state, registered=registered, cut=cut,
                   ledgers=ledgers,
                   first_row=st.ledger_first_row(cut, ledgers.movements, ledgers.fx, today))


def _stale_range(history: HistoryState) -> tuple[date, date] | None:
    if history.stale_from is None or history.stale_to is None:
        return None
    return history.stale_from, history.stale_to


def _from_store(history: HistoryState, p: _Period) -> bool:
    return (not p.current and history.live_from is not None and p.end < history.live_from
            and history.generation > 0)


#: Picks the periods to serve from the ledger's first row (``None``: an empty ledger).
_Select = Callable[[date | None], list[_Period]]


def _closes(conn: sqlite3.Connection, select: _Select, *, now: datetime,
            reporting: Currency, detail: bool) -> tuple[list[_Served], list[_Period]]:
    """The closes of the periods *select* picks (in its order) and the periods it picked,
    inside ONE read snapshot opened after the dashboard's cached build (PERF-S12): stored
    where :func:`_from_store` says, live otherwise, each with the read-time fields of
    §11.2 / §14.6."""
    today = now.date()
    data, _key = build_dashboard_keyed(conn, now=now, reporting=reporting)
    with result_cache.read_snapshot(conn):
        inputs = _inputs(conn, data, now=now, reporting=reporting)
        history = inputs.history
        periods = select(inputs.first_row)
        served: dict[tuple[str, str], _Served] = {}
        live: list[_Period] = []
        for p in periods:
            stored = (st.read_period(conn, history.generation, p.kind, p.period)
                      if _from_store(history, p) else None)
            if stored is None:
                live.append(p)
                continue
            close = PeriodClose(kind=stored.kind, period=stored.period, end_day=stored.end_day,
                                kpi=stored.kpi, activity=stored.activity, source="store")
            s = _Served(period=p, close=close)
            if detail:
                s.holdings = st.read_period_holdings(conn, history.generation, p.kind,
                                                     p.period)
                s.cash = st.read_period_cash(conn, history.generation, p.kind, p.period)
            served[(p.kind, p.period)] = s
        if live:
            for s in _live_closes(conn, live, inputs=inputs, now=now, reporting=reporting,
                                  detail=detail):
                served[(s.period.kind, s.period.period)] = s
        stale = _stale_range(history)
        refused: list[Dividend] = (list(inputs.data.orphan_dividends)
                                   + [u.dividend for u in inputs.data.unbookable_dividends])
        out: list[_Served] = []
        for p in periods:
            s = served[(p.kind, p.period)]
            s.restatements = st.restatements_for(conn, p.kind, p.period)
            _read_time_fields(s, history=history, stale=stale, inputs=inputs,
                              refused=refused, today=today)
            out.append(s)
    return out, periods


def _read_time_fields(s: _Served, *, history: HistoryState, stale: tuple[date, date] | None,
                      inputs: _Inputs, refused: Sequence[Dividend], today: date) -> None:
    """§11.2 / §11.4 / §14.6, decided when the close is read."""
    p = s.period
    as_at = p.as_at(today)
    is_stale = (s.close.source == "store" and stale is not None
                and stale[0] <= p.end <= stale[1])
    kpi = s.close.kpi
    receivable = None if is_stale else st.dividends_receivable(
        inputs.registered.dividends, inputs.registered.instruments, as_at, refused=refused)
    update: dict[str, Any] = {"notes": kpi.notes.model_copy(
        update={"dividends_receivable": receivable})}
    if st.twr_pending(p.start, as_at, live_from=history.live_from, stale=stale):
        update.update(period_twr=None, period_twr_reason=st.TWR_PENDING_ZH)
    s.close = s.close.model_copy(update={
        "kpi": kpi.model_copy(update=update), "stale": is_stale,
        "restated": bool(s.restatements), "restatements": list(s.restatements)})


def _live_closes(conn: sqlite3.Connection, periods: Sequence[_Period], *, inputs: _Inputs,
                 now: datetime, reporting: Currency, detail: bool) -> list[_Served]:
    """The closes of *periods* computed live (§11.4): what the settlement worker would store
    for each (``settle_worker._Run._close``), from ONE replay (``iter_books``) and the
    build's composed trend."""
    today = now.date()
    cut = inputs.cut
    instruments = cut.instruments
    first = trend_start(cut)
    # every book a close reads: its own day's, and those that decide its options flag (the
    # latest book on or before the period's first day, then one per event day in it)
    wanted: set[date] = set()
    flag_days: dict[tuple[str, str], list[date]] = {}
    for p in periods:
        as_at = p.as_at(today)
        days = trend_book_days(cut, start=p.start, end=as_at)
        flag_days[(p.kind, p.period)] = days
        wanted.update(days)
        wanted.add(as_at)
    order = sorted(wanted)
    books: dict[date, Book] = dict(zip(order, iter_books(cut, order), strict=True))
    ledgers = inputs.ledgers
    ledger = pc.CloseLedger(
        bundle=inputs.registered, movements=ledgers.movements, conversions=ledgers.fx,
        transactions=ledgers.transactions,
        dividends=tuple(ledgers.dividends) + tuple(ledgers.unbooked),
        accounts=list_accounts(conn))
    cache = FxSeriesCache(conn)
    rates = pc.own_date_rates(cache, reporting)
    unconfirmed = final_through(conn, now, since=SINCE_GO_LIVE).unconfirmed_closes
    data = inputs.data
    points = {pt.date: pt for pt in data.trend.points} if data.trend.available else {}
    index_points = twr_index(list(data.trend.points)) if data.trend.available else []
    index = pc.TwrIndex(values={ip.date: ip.value for ip in index_points},
                        anchor=index_points[0].date if index_points else None)
    history = inputs.history
    out: list[_Served] = []
    for p in periods:
        as_at = p.as_at(today)
        book = books[as_at]
        excluded = any(holds_open_option(books[d], instruments)
                       for d in flag_days[(p.kind, p.period)])
        ck = pc.kpi_at(as_at, book=book, ledger=ledger,
                       conn_source=OnOrBefore(conn, as_at, fx_cache=cache),
                       reporting=reporting, options_value_excluded=excluded,
                       unconfirmed=unconfirmed, carried=_carried_marks(conn, history, p))
        cash = pc.cash_at(as_at, book=book, ledger=ledger, fx_cache=cache, reporting=reporting)
        kpi = ck.kpi.model_copy(update={"notes": ck.kpi.notes.model_copy(update={
            "options_value_excluded": excluded,
            "missing_cash_fx": pc.missing_cash_fx(cash, reporting)})})
        kpi = _with_live_trend(kpi, p, as_at=as_at, first=first, data=data, points=points,
                               index=index)
        act = pc.activity(start=p.start, end=as_at, book=book, ledger=ledger,
                          reporting=reporting, rate_at=rates)
        close = PeriodClose(kind=p.kind, period=p.period, end_day=as_at, kpi=kpi,
                            activity=act, source="live")
        # positions in the store's order (``read_period_holdings``: account, then symbol), so
        # a close reads the same whether it is served live or settled
        holdings = (sorted(pc.holdings_at(ck.block), key=lambda h: (h.account_id, h.symbol))
                    if detail else None)
        out.append(_Served(period=p, close=close, holdings=holdings,
                           cash=cash if detail else None))
    return out


def _carried_marks(conn: sqlite3.Connection, history: HistoryState,
                   p: _Period) -> list[PriceDate]:
    """The unconfirmed-close marks the published generation stored for this close (the
    worker's *carried*, ``period_close.unconfirmed_at``)."""
    if history.generation == 0 or p.current:
        return []
    stored = st.read_period(conn, history.generation, p.kind, p.period)
    return [] if stored is None else list(stored.kpi.notes.unconfirmed_closes)


def _with_live_trend(kpi: PeriodKpi, p: _Period, *, as_at: date, first: date | None,
                     data: DashboardData, points: dict[date, TrendPoint],
                     index: pc.TwrIndex) -> PeriodKpi:
    """The trend figures at D and the period's TWR, as the worker sets them, from the build's
    composed trend (the stored days before ``live_from``, live after)."""
    if first is None or as_at < first:
        kpi = pc.with_trend(kpi, point=None, twr=None, twr_reason=pc.TWR_NOT_STARTED_ZH,
                            trend_reason=pc.TREND_NOT_STARTED_ZH)
        if kpi.xirr is None:
            kpi = kpi.model_copy(update={"xirr_reason": pc.XIRR_NO_POSITION_ZH})
        return kpi
    if not data.trend.available:
        # the live series is unavailable (a flow without a rate): no trend figure — in the
        # build's own words — exactly as the trend card shows none
        reason = data.freshness.trend_unavailable_reason or pc.TREND_UNAVAILABLE_ZH
        return pc.with_trend(kpi, point=None, twr=None, twr_reason=reason,
                             trend_reason=reason)
    prev = p.start - _ONE_DAY
    start_end = prev if prev >= first else None
    twr, why = pc.period_twr(index, points, start_end=start_end, end=as_at)
    return pc.with_trend(kpi, point=points.get(as_at), twr=twr, twr_reason=why)


# =============================================================================================
# 暫定 (§11.4) — per request: it depends on the quote runs and the clock, never on the store
# =============================================================================================


def _provisional_note(p: _Period, ft: FinalThrough) -> str | None:
    """「月底收盤價尚未確定：美股報價工作未完成」 for a past period whose end day lies after
    ``final_through``; ``None`` otherwise (and for the current period, which is live)."""
    if p.current or p.end <= ft.through:
        return None
    markets = [_QUOTE_JOB_MARKET_ZH.get(j, j) for j in ft.blocked_jobs]
    what = "年底" if p.kind == "Y" else "月底"
    if not markets:
        return f"{what}收盤價尚未確定：報價工作尚未完成"
    return f"{what}收盤價尚未確定：{'、'.join(markets)}報價工作未完成"


def _labelled(served: Sequence[_Served], conn: sqlite3.Connection,
              now: datetime) -> list[_Served]:
    ft = final_through(conn, now)
    for s in served:
        s.close = s.close.model_copy(update={"provisional": _provisional_note(s.period, ft)})
    return list(served)


# =============================================================================================
# The three readers
# =============================================================================================


def _listing(conn: sqlite3.Connection, *, kind: Kind, limit: int, now: datetime,
             reporting: Currency) -> tuple[list[_Served], int]:
    """The newest *limit* closes of *kind* and how many there are — cached per state and day
    (``shared/result_cache``; the build it reads is the cached one), then labelled 暫定 for
    this request's clock."""
    today = now.date()

    def compute() -> tuple[list[_Served], int]:
        total = 0

        def select(first: date | None) -> list[_Period]:
            nonlocal total
            periods = [] if first is None else _periods(first, today, kind)
            total = len(periods)
            return periods[:limit]

        served, _picked = _closes(conn, select, now=now, reporting=reporting, detail=False)
        return served, total

    served, total = result_cache.cached(
        conn, name=f"periods:{kind}", now=now, reporting=reporting,
        extra=(limit, flags.settle_enabled()), compute=compute, copy=copy.deepcopy,
        snapshot=False)
    return _labelled(served, conn, now), total


def snapshot_rows(conn: sqlite3.Connection, *, limit: int, now: datetime,
                  reporting: Currency) -> dict[str, Any]:
    """``GET /api/snapshots``: ``{rows, total_count}`` — the month rows, newest first
    (``total_count``: every month the ledger has, before *limit*)."""
    served, total = _listing(conn, kind="M", limit=limit, now=now, reporting=reporting)
    return {"rows": [_month_row(s, now) for s in served], "total_count": total}


def _month_row(s: _Served, now: datetime) -> dict[str, Any]:
    kpi = s.close.kpi
    wire = kpi.model_dump(mode="json")
    restated = None
    if s.restatements:
        restated = {"cutover": all(r.cutover for r in s.restatements),
                    "rows": [r.model_dump(mode="json") for r in s.restatements]}
    return {
        "month": s.period.period,
        "as_of": now.isoformat() if s.period.current else s.period.end.isoformat(),
        "reporting_ccy": kpi.reporting_ccy.value,
        "total_value": wire["total_market_value"],
        "total_return": wire["total_return"],
        "total_return_rate": wire["total_return_rate"],
        "xirr": wire["xirr"],
        "by_currency": wire["by_currency_value"],
        "kind": "M",
        "provisional": s.close.provisional,
        "stale": s.close.stale,
        "notes": wire["notes"],
        "xirr_reason": kpi.xirr_reason,
        "period_twr": wire["period_twr"],
        "period_twr_reason": kpi.period_twr_reason,
        "restated": restated,
    }


def period_rows(conn: sqlite3.Connection, *, kind: Kind, limit: int, now: datetime,
                reporting: Currency) -> dict[str, Any]:
    """``GET /api/settlement/periods``: ``{rows: PeriodClose[], total_count}``, newest
    first, the current period (live) included."""
    served, total = _listing(conn, kind=kind, limit=limit, now=now, reporting=reporting)
    return {"rows": [s.close.model_dump(mode="json", by_alias=True) for s in served],
            "total_count": total}


def period_detail(conn: sqlite3.Connection, *, kind: Kind, period: str, now: datetime,
                  reporting: Currency) -> dict[str, Any] | None:
    """``GET /api/settlement/periods/{kind}/{period}``: one close with its holdings and its
    cash at D, or ``None`` when *period* is not one of the ledger's (before its first row,
    after today). *period* is already normalised (:func:`parse_period`)."""
    today = now.date()

    def select(first: date | None) -> list[_Period]:
        if first is None:
            return []
        return [p for p in _periods(first, today, kind) if p.period == period]

    closes, picked = _closes(conn, select, now=now, reporting=reporting, detail=True)
    if not picked:
        return None
    (served,) = _labelled(closes, conn, now)
    detail = PeriodDetail(**served.close.model_dump(), holdings=served.holdings or [],
                          cash=served.cash or [])
    return detail.model_dump(mode="json", by_alias=True)


__all__ = [
    "ensure_table",
    "parse_period",
    "period_detail",
    "period_rows",
    "snapshot_job",
    "snapshot_rows",
    "write_snapshot",
]
