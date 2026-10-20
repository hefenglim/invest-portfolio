"""PERF-F2's fix: the trend's FX pair set (``timeseries.trend_fx_currencies``) holds the quote
currencies AND the currency of every cash movement the trend converts — by the ONE predicate
XIRR reads (``returns.is_return_movement``) — and a rate genuinely missing on or before a flow
still makes the whole trend unavailable, with the same reason.

Run on ``unquoted_ccy_cash`` (``golden`` + a MYR/TWD rate on 2026-03-02 + a 50 MYR REBATE on
2026-03-16; no instrument is quoted in MYR) and on its missing-rate variant
(``tests/reference/perf_f2.drop_rebate_rate``).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.data_ingestion.store import list_cash_movements, load_ledger_bundle
from portfolio_dash.portfolio.dashboard import _build_dashboard
from portfolio_dash.portfolio.returns import XIRR_CASH_KINDS, is_return_movement
from portfolio_dash.portfolio.timeseries import trend_fx_currencies
from portfolio_dash.shared.cash_kinds import CashKind
from portfolio_dash.shared.enums import Currency
from tests.reference import perf_f2
from tests.reference.corpora import CORPORA, TEMPLATE_DIR_NAME, copy_corpus, open_db

_FX_REFUSAL = "部分投入流量缺少當日匯率，無法換算成報告幣，基準對照暫不可用"


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
    2026-03-16: the trend is unavailable in the same words as before the fix, and so are B,
    its split and the benchmark."""
    c = CORPORA["unquoted_ccy_cash"]
    data = _build_dashboard(missing_rate, now=c.now, reporting=c.reporting)
    assert data.trend.available is False and data.trend.points == []
    assert data.freshness.trend_unavailable_reason == "帳本中有交易日期缺少匯率資料"
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
