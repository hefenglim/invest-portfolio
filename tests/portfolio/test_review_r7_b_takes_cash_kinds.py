"""AI-D48/D49: B (含匯兌總損益) takes the same three cash kinds XIRR does — and R4 does not.

AI-D42 moved `xirr_reporting` onto REBATE / INTEREST_EXPENSE / BROKER_FEE and left B behind.
That is not a cosmetic gap: **A and B are printed side by side on one KPI band** (AI-D41), and
their difference is labelled 「本金匯率效果」. With one of them counting a 77% rebate and the
other not, that label stopped being true — the difference became 「本金匯率效果 + 三類現金帳」.

R4 made it worse: the benchmark comparison's `excess` is measured against B, so a broker fee B
could not see was being reported as beating the index.

The counter-evidence is deliberately about DIRECTION and MAGNITUDE, not just "the number
changed": a fee must LOWER B by exactly the fee, because a fee is money consumed exactly like a
buy-side fee (which `net_invested` has always capitalised), and a rebate must RAISE it.
"""

import sqlite3
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_cash_movement,
    insert_transaction,
    upsert_instrument,
)
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side

D = Decimal
NOW = datetime(2026, 6, 10, 12, 0)
BUY_DAY = date(2026, 1, 5)
FEE_DAY = date(2026, 2, 2)
TWD = Currency.TWD


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    create_pricing_tables(c)
    seed_accounts(c)
    yield c
    c.close()


def _seed(conn: sqlite3.Connection) -> None:
    """One TW buy in the reporting currency, so FX cannot blur the arithmetic.

    Single-currency on purpose: this file is about WHICH FLOWS COUNT, and a second currency
    would let a rate change mask a missing flow.
    """
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW, quote_ccy=TWD,
                                       sector="Semiconductors", name="TSMC", board="TWSE"))
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=D("1000"), price=D("500"), fees=D("0"), tax=D("0"),
                       trade_date=BUY_DAY)
    upsert_prices(conn, [
        PriceRow(instrument="2330", market=Market.TW, as_of=BUY_DAY + timedelta(days=i),
                 close=D("500"), source="test")
        for i in range(160)
    ], fetched_at=NOW)


def _b(conn: sqlite3.Connection) -> Decimal:
    data = build_dashboard(conn, now=NOW, reporting=TWD)
    value = data.kpis.total_return_fx_complete
    assert value is not None, "B must be available on a priced single-currency ledger"
    return value


def _move(conn: sqlite3.Connection, kind: str, amount: str) -> None:
    insert_cash_movement(conn, account_id="tw_broker", move_date=FEE_DAY, kind=kind,
                         ccy=TWD, amount=D(amount))


def test_a_broker_fee_lowers_b_by_exactly_the_fee(conn: sqlite3.Connection) -> None:
    _seed(conn)
    before = _b(conn)
    _move(conn, "BROKER_FEE", "900")
    assert _b(conn) == before - D("900")


def test_margin_interest_lowers_b_by_exactly_the_interest(conn: sqlite3.Connection) -> None:
    _seed(conn)
    before = _b(conn)
    _move(conn, "INTEREST_EXPENSE", "250")
    assert _b(conn) == before - D("250")


def test_a_rebate_raises_b_by_exactly_the_rebate(conn: sqlite3.Connection) -> None:
    """FE-D1's 77% 群益 rebate refunds commission that was capitalised into cost basis, so
    leaving it out overstates the cost of every round trip (0.229% of capital per trip)."""
    _seed(conn)
    before = _b(conn)
    _move(conn, "REBATE", "669")
    assert _b(conn) == before + D("669")


@pytest.mark.parametrize("kind", ["DEPOSIT", "WITHDRAW", "OPENING", "INTEREST"])
def test_capital_movements_and_idle_interest_leave_b_untouched(
    conn: sqlite3.Connection, kind: str
) -> None:
    """The exclusions are as load-bearing as the inclusions, and for AI-D42's exact reasons.

    DEPOSIT / WITHDRAW / OPENING are capital movements: admitting them would redefine B from
    「這些證券賺了多少」 into an account balance. INTEREST is pool income whose principal never
    entered the figure, so crediting its yield is asymmetric — the same argument that kept it
    out of XIRR, applied to the figure printed beside XIRR.
    """
    _seed(conn)
    before = _b(conn)
    _move(conn, kind, "5000")
    assert _b(conn) == before


def test_the_benchmark_leg_does_not_buy_the_index_with_a_broker_fee(
    conn: sqlite3.Connection,
) -> None:
    """AI-D49: the costs are charged to the portfolio, never placed on the index.

    A passive investor holding the index does not pay this account's margin interest or
    monthly broker fee, so the counterfactual's own `net_invested` stays SECURITIES-only —
    while `excess` (measured against B) falls by the fee. Charging both legs would invent a
    cost no index investor paid; leaving B alone would report the fee as market-beating.

    The index is seeded flat, so the counterfactual's return is zero and `excess` is B — which
    makes the assertion about the fee and nothing else.
    """
    _seed(conn)
    upsert_prices(conn, [
        PriceRow(instrument="0050", market=Market.TW, as_of=BUY_DAY + timedelta(days=i),
                 close=D("100"), source="test")
        for i in range(160)
    ], fetched_at=NOW)
    before = build_dashboard(conn, now=NOW, reporting=TWD).benchmark
    _move(conn, "BROKER_FEE", "900")
    after = build_dashboard(conn, now=NOW, reporting=TWD).benchmark
    assert before is not None and after is not None
    assert before.available, before.reason
    assert after.available, after.reason
    assert before.excess is not None
    # The index leg is untouched: same money, same days, same units.
    assert after.net_invested == before.net_invested
    assert after.benchmark_return == before.benchmark_return
    # The portfolio leg carries the cost.
    assert after.excess == before.excess - D("900")
    # And the fee is NOT counted as 「money with no benchmark」 — that ratio means a market
    # this app cannot compare (MY today), not a flow that was never going to buy an index.
    assert after.uncovered_ratio == before.uncovered_ratio


def test_the_decomposition_stays_three_honest_terms(conn: sqlite3.Connection) -> None:
    """B = A + 本金匯率效果 + 交易與融資成本, and the middle term keeps its own meaning.

    Without the third field, `principal_fx_effect` (computed as B − A) would have absorbed
    the broker fee while still being labelled 「本金匯率效果」 on the KPI band — the same
    mislabelling AI-D48 was raised to remove, reproduced one field over. Single-currency
    ledger, so the FX term is exactly zero and the cost is the only thing that can move.
    """
    _seed(conn)
    _move(conn, "BROKER_FEE", "900")
    _move(conn, "REBATE", "200")
    k = build_dashboard(conn, now=NOW, reporting=TWD).kpis

    # `is not None` FIRST: both fields are Optional, and `None == D("0")` is a silent False
    # that reads as an ordinary value mismatch rather than "the figure was never computed".
    # It also narrows the type for the decomposition below, which is why mypy flagged the sum.
    assert k.trading_financing_cost is not None and k.principal_fx_effect is not None
    assert k.total_return is not None and k.total_return_fx_complete is not None
    assert k.trading_financing_cost == D("-700")  # P&L sign: a fee is negative
    assert k.principal_fx_effect == D("0")  # one currency -> no principal-FX effect at all
    assert k.total_return_fx_complete == (
        k.total_return + k.principal_fx_effect + k.trading_financing_cost
    )
