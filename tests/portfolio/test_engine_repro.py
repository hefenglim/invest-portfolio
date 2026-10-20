"""Two suspected pre-existing defects, reproduced and NOT fixed (PERF plan §5.1 "Repro, don't
fix"; spec §8 item 5 and §20). A fix changes output, so each waits for the owner's ruling and
is then its own change. Each ``xfail(strict=True)`` states what the CORRECT behaviour would be:
it fails today (the defect is there), and the day a fix lands it passes and ``strict`` turns
that into a failure, so the marker has to be removed with the fix.

PERF-F2 — the trend's FX pair set omits the currency of a cash-kind flow
    The trend loads FX only for the ledger's quote currencies. A cash movement of an XIRR kind
    (REBATE / INTEREST_EXPENSE / BROKER_FEE) in a currency no instrument is quoted in finds no
    rate in that map, so the WHOLE trend is unavailable — and with it net worth, B, B − A and
    the benchmark — although the rate is stored and XIRR counts the same movement.

The price-basis split edge (spec §20 row 9)
    ``upsert_prices`` folds in the splits dated in ``(as_of, fetched_at]``, with *fetched_at*
    the TAIPEI date of the fetch. A US fetch made in Taipei on a split's ex-date but before the
    New York open — when the provider still delivers the previous session's close as traded —
    multiplies that close by the split ratio. The next day's fetch, after the provider has
    restated its history, stores the right value again.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.portfolio.dashboard import _build_dashboard
from portfolio_dash.portfolio.dashboard_models import DashboardData
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.schema import create_tables
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.corporate_actions import (
    ActionIndex,
    CorporateAction,
    CorporateActionKind,
    split_factor,
)
from portfolio_dash.shared.enums import Market
from tests.reference.corpora import (
    CORPORA,
    TEMPLATE_DIR_NAME,
    UNQUOTED_FX_DATE,
    UNQUOTED_MOVE_DATE,
    copy_corpus,
    open_db,
)

TAIPEI = ZoneInfo("Asia/Taipei")

# --- PERF-F2 -------------------------------------------------------------------------------


@pytest.fixture
def unquoted(tmp_path_factory: pytest.TempPathFactory) -> Iterator[sqlite3.Connection]:
    base = tmp_path_factory.getbasetemp() / TEMPLATE_DIR_NAME
    db = copy_corpus("unquoted_ccy_cash", base, tmp_path_factory.mktemp("f2") / "u.db")
    conn = open_db(db.path)
    try:
        yield conn
    finally:
        conn.close()


def _build(conn: sqlite3.Connection) -> DashboardData:
    c = CORPORA["unquoted_ccy_cash"]
    return _build_dashboard(conn, now=c.now, reporting=c.reporting)


def test_perf_f2_preconditions_hold(unquoted: sqlite3.Connection) -> None:
    """The defect's shape: the MYR/TWD rate IS stored on or before the MYR rebate, no ledger
    instrument is quoted in MYR (so the quote currencies alone never load the pair), and XIRR
    counts the rebate."""
    row = unquoted.execute(
        "SELECT MAX(as_of_date) FROM fx_rates WHERE base='MYR' AND quote='TWD' "
        "AND as_of_date<=?", (UNQUOTED_MOVE_DATE.isoformat(),)).fetchone()
    assert row[0] == UNQUOTED_FX_DATE.isoformat()
    quoted = {r[0] for r in unquoted.execute(
        "SELECT DISTINCT i.quote_ccy FROM instruments i WHERE i.symbol IN ("
        "SELECT symbol FROM transactions UNION SELECT symbol FROM dividends "
        "UNION SELECT symbol FROM opening_inventory)")}
    assert "MYR" not in quoted and quoted
    data = _build(unquoted)
    assert data.kpis.xirr is not None


def test_perf_f2_an_xirr_kind_movement_in_an_unquoted_currency_keeps_the_trend(
    unquoted: sqlite3.Connection,
) -> None:
    """Fixed: the trend's pair set holds the rebate's currency (``trend_fx_currencies``), so
    the trend, B and its split are formed. The benchmark is no longer refused for want of a
    rate: it gives golden's own answer — golden stores no index closes, and the rebate carries
    no market, so it is no flow of the counterfactual (AI-D49)."""
    rebate = unquoted.execute(
        "SELECT m.amount, r.rate FROM cash_movements m JOIN fx_rates r ON r.base = m.ccy "
        "AND r.quote = 'TWD' AND r.as_of_date <= m.date WHERE m.kind = 'REBATE' "
        "ORDER BY r.as_of_date DESC LIMIT 1").fetchone()
    data = _build(unquoted)
    assert data.trend.available is True
    assert data.freshness.trend_unavailable_reason is None
    assert data.kpis.total_return_fx_complete is not None
    assert data.kpis.trading_financing_cost == Decimal(rebate[0]) * Decimal(rebate[1])
    # the counterfactual RAN (every flow uncovered: no index closes) — not refused for FX
    assert data.benchmark is not None and data.benchmark.uncovered_markets == ["TW", "US"]
    assert "匯率" not in (data.benchmark.reason or "")


# --- the price-basis split edge (spec §20 row 9) ------------------------------------------------

_EX_DATE = date(2026, 6, 16)           # a Tuesday: the split takes effect at the NY open
_LAST_SESSION = date(2026, 6, 15)      # Monday, the last pre-split session
_SPLIT = CorporateAction(account_id="schwab", date=_EX_DATE, kind=CorporateActionKind.SPLIT,
                         from_symbol="AAA", to_symbol="AAA", ratio_to=Decimal("20"),
                         ratio_from=Decimal("1"))
_AS_TRADED = Decimal("400")            # Monday's close, as traded (pre-split terms)


@pytest.fixture
def prices() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    create_tables(c)
    yield c
    c.close()


def _fetch(conn: sqlite3.Connection, close: Decimal, fetched_at: datetime) -> Decimal:
    """One provider row for Monday through the real write seam, with the split ledger bound
    in as ``scheduler.jobs.split_factor_fn`` binds it; returns the stored close."""
    index = ActionIndex.build([_SPLIT])

    def factor_of(symbol: str, *, after: date, through: date) -> Decimal:
        return split_factor(index, symbol, after=after, through=through)

    upsert_prices(conn, [PriceRow(instrument="AAA", market=Market.US, as_of=_LAST_SESSION,
                                  close=close, source="test")],
                  fetched_at=fetched_at, factor_of=factor_of)
    row = conn.execute("SELECT close FROM prices WHERE instrument='AAA' AND as_of_date=?",
                       (_LAST_SESSION.isoformat(),)).fetchone()
    return Decimal(row["close"])


def test_a_fetch_dated_before_the_ex_date_stores_the_close_as_traded(
    prices: sqlite3.Connection,
) -> None:
    """Control: Monday 23:00 Taipei — the window ``(Mon, Mon]`` holds no split."""
    assert _fetch(prices, _AS_TRADED, datetime(2026, 6, 15, 23, 0, tzinfo=TAIPEI)) == _AS_TRADED


def test_the_next_days_restated_fetch_heals_the_row(prices: sqlite3.Connection) -> None:
    """Control: Wednesday 05:30 Taipei — the provider has restated Monday to post-split terms
    (400 / 20), the seam multiplies the split back out, and the row is as traded again."""
    restated = _AS_TRADED / _SPLIT.ratio_to
    assert _fetch(prices, restated, datetime(2026, 6, 17, 5, 30, tzinfo=TAIPEI)) == _AS_TRADED


def test_a_fetch_on_the_ex_dates_taipei_date_before_new_york_midnight_stores_it_as_traded(
    prices: sqlite3.Connection,
) -> None:
    # Tuesday 05:30 Taipei = Monday 17:30 New York: Monday's session is closed, the ex-date has
    # not begun in New York (the window's bound turns at New York's midnight, owner ruling
    # 2026-10-20), and the provider delivers Monday's close as traded.
    assert _fetch(prices, _AS_TRADED, datetime(2026, 6, 16, 5, 30, tzinfo=TAIPEI)) == _AS_TRADED
