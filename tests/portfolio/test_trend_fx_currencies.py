"""PERF-F2's fix: the trend's FX pair set (``timeseries.trend_fx_currencies``) holds the quote
currencies AND the currency of every cash movement the trend converts — by the ONE predicate
XIRR reads (``returns.is_return_movement``) — and a rate genuinely missing on or before a flow
still makes the whole trend unavailable. Its reason names that flow's date and pair, from ONE
owner (``timeseries.first_unconvertible_flow`` / ``trend_unavailable_reason``; accountant
MINOR 4, owner ruling 2026-10-22).

Run on ``unquoted_ccy_cash`` (``golden`` + a MYR/TWD rate on 2026-03-02 + a 50 MYR REBATE on
2026-03-16; no instrument is quoted in MYR) and on its missing-rate variant
(``tests/reference/perf_f2.drop_rebate_rate``).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

import pytest

from portfolio_dash.data_ingestion.store import (
    StoredCashMovement,
    list_cash_movements,
    load_ledger_bundle,
)
from portfolio_dash.portfolio.dashboard import _build_dashboard
from portfolio_dash.portfolio.dashboard_models import UnconvertibleFlow
from portfolio_dash.portfolio.returns import (
    XIRR_CASH_KINDS,
    is_return_movement,
    replay_refusals,
)
from portfolio_dash.portfolio.timeseries import (
    FxHistory,
    build_reporting_flows,
    first_unconvertible_flow,
    trend_fx_currencies,
    trend_start,
    trend_unavailable_reason,
)
from portfolio_dash.pricing.store import FxSeriesCache
from portfolio_dash.shared.cash_kinds import CashKind
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.ledger import Dividend, LedgerBundle, valued_rows_as_of
from tests.reference import perf_f2
from tests.reference.corpora import CORPORA, TEMPLATE_DIR_NAME, copy_corpus, open_db

_FX_REFUSAL = "部分投入流量缺少當日匯率，無法換算成報告幣，基準對照暫不可用"
#: The trend's ONE unavailable sentence on the missing-rate variant (``timeseries``).
_MISSING_MYR = "2026-03-16 起缺少 MYR/TWD 匯率（當日或之前無匯率），走勢無法計算"


@dataclass(frozen=True)
class _Move:
    date: date
    kind: str
    ccy: Currency
    amount: Decimal


def _corpus(tmp_path_factory: pytest.TempPathFactory, *, missing_rate: bool
            ) -> Iterator[sqlite3.Connection]:
    base = tmp_path_factory.getbasetemp() / TEMPLATE_DIR_NAME
    db = copy_corpus("unquoted_ccy_cash", base, tmp_path_factory.mktemp("f2fx") / "u.db")
    if missing_rate:
        perf_f2.drop_rebate_rate(db.path)
    conn = open_db(db.path)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def unquoted(tmp_path_factory: pytest.TempPathFactory) -> Iterator[sqlite3.Connection]:
    yield from _corpus(tmp_path_factory, missing_rate=False)


@pytest.fixture
def missing_rate(tmp_path_factory: pytest.TempPathFactory) -> Iterator[sqlite3.Connection]:
    yield from _corpus(tmp_path_factory, missing_rate=True)


@pytest.mark.parametrize("kind", [k.value for k in CashKind])
def test_the_predicate_is_the_xirr_kinds(kind: str) -> None:
    """One predicate, the AI-D42 table's: the three costs of trading and financing, never a
    capital movement or idle-cash interest — in any spelling the ledger stores."""
    for spelling in (kind, kind.lower()):
        mv = _Move(date(2026, 1, 1), spelling, Currency.MYR, Decimal("1"))
        assert is_return_movement(mv) is (kind in XIRR_CASH_KINDS), spelling


def test_the_pair_set_holds_every_converted_movements_currency(
        unquoted: sqlite3.Connection) -> None:
    bundle = load_ledger_bundle(unquoted)
    moves = list_cash_movements(unquoted)
    assert trend_fx_currencies(bundle, []) == {Currency.TWD, Currency.USD}
    assert trend_fx_currencies(bundle, moves) == {Currency.TWD, Currency.USD, Currency.MYR}
    # a capital movement (and idle-cash interest) is no flow of the trend: no pair for it
    for kind in ("DEPOSIT", "WITHDRAW", "OPENING", "INTEREST"):
        other = [_Move(m.date, kind, m.ccy, m.amount) for m in moves]
        assert trend_fx_currencies(bundle, other) == {Currency.TWD, Currency.USD}, kind


def test_a_missing_rate_still_withholds_the_whole_trend(
        missing_rate: sqlite3.Connection) -> None:
    """The rebate's currency IS in the pair set, but no MYR rate is stored on or before
    2026-03-16: the trend is unavailable, and so are B, its split and the benchmark. The reason
    names the first flow with no rate and its pair (accountant MINOR 4, owner ruling
    2026-10-22) — it read 「帳本中有交易日期缺少匯率資料」, which named neither."""
    c = CORPORA["unquoted_ccy_cash"]
    data = _build_dashboard(missing_rate, now=c.now, reporting=c.reporting)
    assert data.trend.available is False and data.trend.points == []
    assert data.freshness.trend_unavailable_reason == _MISSING_MYR
    # the flow itself rides with the build, off the wire (月度成績's live closes read it)
    assert data.freshness.trend_unconvertible == UnconvertibleFlow(
        date(2026, 3, 16), Currency.MYR, Currency.TWD)
    assert "trend_unconvertible" not in data.freshness.model_dump()
    assert data.kpis.total_return_fx_complete is None
    assert data.kpis.trading_financing_cost is None
    assert data.kpis.principal_fx_effect is None
    assert data.benchmark is not None and data.benchmark.reason == _FX_REFUSAL


def test_the_rebate_is_converted_at_its_own_dates_rate(unquoted: sqlite3.Connection) -> None:
    """With the rate stored, every trend point from the rebate on carries it at the MYR/TWD
    rate on or before 2026-03-16 (7.05): ``net_invested`` drops by 352.50 on that day, and
    交易與融資成本 is the same 352.50."""
    c = CORPORA["unquoted_ccy_cash"]
    data = _build_dashboard(unquoted, now=c.now, reporting=c.reporting)
    assert data.trend.available is True
    points = {p.date: p for p in data.trend.points}
    rebate = date(2026, 3, 16)
    before = points[date(2026, 3, 15)].net_invested
    assert before - points[rebate].net_invested == Decimal("352.50")
    assert data.kpis.trading_financing_cost == Decimal("352.50")


def _inputs(conn: sqlite3.Connection) -> tuple[LedgerBundle, list[StoredCashMovement],
                                               FxHistory, list[Dividend]]:
    """The trend's inputs as the live build loads them: the ledger and the movements cut at
    the corpus's day, the FX history over the trend's pair set, the replay's refusals."""
    c = CORPORA["unquoted_ccy_cash"]
    day = c.now.date()
    bundle = load_ledger_bundle(conn).valued_as_of(day)
    moves = valued_rows_as_of(list_cash_movements(conn), day)
    cache = FxSeriesCache(conn)
    fx: FxHistory = {}
    for ccy in trend_fx_currencies(bundle, moves):
        if ccy == c.reporting:
            continue
        for base, quote in ((ccy, c.reporting), (c.reporting, ccy)):
            rows = cache.history(base, quote, date(1900, 1, 1), day)
            if rows:
                fx[(base, quote)] = [(r.as_of, r.rate) for r in rows]
    return bundle, moves, fx, replay_refusals(bundle)


def _bisected_u(bundle: LedgerBundle, moves: list[StoredCashMovement], fx: FxHistory,
                reporting: Currency, refused: list[Dividend], first: date,
                through: date) -> date | None:
    """The settlement worker's U before the ruling, transcribed: the first day whose prefix
    ledger ``build_reporting_flows`` refuses, bisected over ``[first, through]``."""
    def bad(day: date) -> bool:
        return build_reporting_flows(
            bundle.valued_as_of(day), fx, reporting,
            cash_movements=valued_rows_as_of(moves, day), refused_dividends=refused) is None

    if not bad(through):
        return None
    lo, hi = first, through
    if bad(lo):
        return lo
    while (hi - lo).days > 1:
        mid = lo + timedelta(days=(hi - lo).days // 2)
        if bad(mid):
            hi = mid
        else:
            lo = mid
    return hi


def test_the_first_unconvertible_flow_is_named_by_its_date_and_pair(
        unquoted: sqlite3.Connection, missing_rate: sqlite3.Connection) -> None:
    """``timeseries.first_unconvertible_flow`` — the ONE owner of "which flow and pair cannot
    convert" (MINOR 4): ``None`` exactly when ``build_reporting_flows`` converts every flow,
    otherwise the earliest flow with no rate on or before its date; its sentence is the
    trend's one reason."""
    c = CORPORA["unquoted_ccy_cash"]
    bundle, moves, fx, refused = _inputs(unquoted)
    assert first_unconvertible_flow(bundle, fx, c.reporting, cash_movements=moves,
                                    refused_dividends=refused) is None
    assert build_reporting_flows(bundle, fx, c.reporting, cash_movements=moves,
                                 refused_dividends=refused) is not None
    bundle, moves, fx, refused = _inputs(missing_rate)
    flow = first_unconvertible_flow(bundle, fx, c.reporting, cash_movements=moves,
                                    refused_dividends=refused)
    assert flow == UnconvertibleFlow(date(2026, 3, 16), Currency.MYR, Currency.TWD)
    assert flow.pair == "MYR/TWD" and trend_unavailable_reason(flow) == _MISSING_MYR
    # a day before the flow reads the live series' variant, from the same date and pair; the
    # flow's own day and after read the sentence itself
    assert trend_unavailable_reason(flow, as_at=date(2026, 2, 28)) == (
        "即時走勢因 2026-03-16 起缺少 MYR/TWD 匯率而無法計算")
    assert trend_unavailable_reason(flow, as_at=date(2026, 3, 16)) == _MISSING_MYR
    assert build_reporting_flows(bundle, fx, c.reporting, cash_movements=moves,
                                 refused_dividends=refused) is None
    # the settlement's U is that flow's day — what the worker's bisection found
    first = trend_start(bundle)
    assert first is not None
    assert _bisected_u(bundle, moves, fx, c.reporting, refused, first,
                       c.now.date()) == date(2026, 3, 16) == max(flow.on, first)


def test_a_cost_dated_before_the_first_trade_is_named_on_its_own_day(
        missing_rate: sqlite3.Connection) -> None:
    """The rebate moved before golden's first trade (2026-01-05): the sentence names the
    rebate's own day, and the settlement's U — the trend's first day, where the series starts —
    is what the bisection found (``max(flow day, first day)``)."""
    missing_rate.execute("UPDATE cash_movements SET date = '2025-12-20' WHERE kind = 'REBATE'")
    missing_rate.commit()
    c = CORPORA["unquoted_ccy_cash"]
    bundle, moves, fx, refused = _inputs(missing_rate)
    flow = first_unconvertible_flow(bundle, fx, c.reporting, cash_movements=moves,
                                    refused_dividends=refused)
    assert flow is not None and flow.on == date(2025, 12, 20)
    assert trend_unavailable_reason(flow) == (
        "2025-12-20 起缺少 MYR/TWD 匯率（當日或之前無匯率），走勢無法計算")
    first = trend_start(bundle)
    assert first == date(2026, 1, 5)
    assert _bisected_u(bundle, moves, fx, c.reporting, refused, first,
                       c.now.date()) == first == max(flow.on, first)
