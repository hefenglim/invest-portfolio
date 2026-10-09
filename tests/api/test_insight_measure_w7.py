"""Portfolio-scope prediction measurement via the TWR chain (W7, AI-D35).

The ``symbol=None`` arm of ``_measure_actual`` measures ``price_change`` over the
create→due window from the SAME ``daily_value_series`` the dashboard trend plots,
chain-linked by ``twr_index`` so a mid-window flow (a deposit-funded buy) never reads
as P&L. ``relative``/``volatility`` stay honestly unmeasurable at portfolio scope (a
blended three-market benchmark is a separate ruling).
"""

import sqlite3
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from portfolio_dash.api import insight_service
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.holdings import load_action_index
from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.llm_insight import evaluations_store as es
from portfolio_dash.llm_insight.cards import Prediction
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import init_golden_base

NOW = datetime(2026, 6, 30, 12, 0)
TWD = Currency.TWD
CREATED = date(2026, 6, 1)
DUE = date(2026, 6, 20)


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    init_golden_base(c)
    seed_accounts(c)
    yield c
    c.close()


def _due_portfolio(metric: str = "price_change") -> es.DueInsight:
    return es.DueInsight(
        insight_id=1, insight_type_id=1, symbol=None, calibration_version=None,
        is_shadow=False, confidence=60,
        prediction=f'{{"metric":"{metric}","direction":"up","horizon_days":30}}',
        due_at=datetime.combine(DUE, datetime.min.time()).isoformat(),
        created_at=datetime.combine(CREATED, datetime.min.time()).isoformat(),
        price_at_create=None,
    )


def _pred(metric: str = "price_change") -> Prediction:
    return Prediction(
        metric=metric,  # type: ignore[arg-type]
        direction="up", horizon_days=30,
    )


def _buy(conn: sqlite3.Connection, day: date, qty: str, price: str) -> None:
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=Decimal(qty), price=Decimal(price),
                       fees=Decimal(0), tax=Decimal(0), trade_date=day)


def _plant(conn: sqlite3.Connection, points: list[tuple[date, str]]) -> None:
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW, quote_ccy=TWD,
                                       sector="Tech", name="2330"))
    upsert_prices(
        conn,
        [PriceRow(instrument="2330", market=Market.TW, as_of=d, close=Decimal(c),
                  source="test") for d, c in points],
        fetched_at=NOW,
    )


def _measure(
    conn: sqlite3.Connection, due: es.DueInsight, metric: str = "price_change"
) -> object:
    return insight_service._measure_actual(
        conn, due, _pred(metric), actions=load_action_index(conn),
        now=NOW, reporting=TWD,
    )


def test_portfolio_price_change_measured_via_twr(conn: sqlite3.Connection) -> None:
    """Hand-checked: 10 @ 100 on 6/1; the close steps 100 → 110 on 6/11 → TWR +10%."""
    _buy(conn, CREATED, "10", "100")
    _plant(conn, [(CREATED + timedelta(days=i), "100") for i in range(11)]
           + [(CREATED + timedelta(days=i), "110") for i in range(11, 20)])
    actual = _measure(conn, _due_portfolio())
    assert actual is not None
    assert actual.price_change_pct == Decimal("0.1")  # type: ignore[attr-defined]


def test_a_mid_window_buy_does_not_read_as_profit(conn: sqlite3.Connection) -> None:
    """DISPROOF (AI-D35's whole point): flat prices, a second 10 @ 100 buy on 6/10
    doubles the position — naive ``V_end/V_start − 1`` reads +100%, and the flow-adjusted
    TWR must read exactly 0."""
    _buy(conn, CREATED, "10", "100")
    _buy(conn, CREATED + timedelta(days=9), "10", "100")  # 6/10, mid-window
    _plant(conn, [(CREATED + timedelta(days=i), "100") for i in range(20)])
    actual = _measure(conn, _due_portfolio())
    assert actual is not None
    assert actual.price_change_pct == Decimal("0")  # type: ignore[attr-defined]


def test_portfolio_relative_and_volatility_stay_honest_none(
    conn: sqlite3.Connection,
) -> None:
    _buy(conn, CREATED, "10", "100")
    _plant(conn, [(CREATED + timedelta(days=i), "100") for i in range(20)])
    assert _measure(conn, _due_portfolio("relative"), "relative") is None
    assert _measure(conn, _due_portfolio("volatility"), "volatility") is None


def test_portfolio_thin_ledger_defers(conn: sqlite3.Connection) -> None:
    """No ledger events → the trend is unavailable → None (pending_data, never a miss)."""
    assert _measure(conn, _due_portfolio()) is None
