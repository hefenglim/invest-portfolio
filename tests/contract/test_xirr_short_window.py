"""XIRR is not annualized over a window too short to annualize (owner ruling 2026-08-05).

Found on a freshly reset instance carrying exactly one same-week trade — the state the
owner will be in on day one, and one no fixture covered because every fixture starts with
history. XIRR raises the period return to the power 365/window, so a book gain of +131.7%
reports 2,749,353% at a 30-day window and **1.5e133 at a 1-day window**. That 136-digit
value was rendered as the headline return AND pushed the dashboard 1,915px sideways.

The ruling withholds the *annualization*, never the return: `total_return_rate` carries the
same information un-annualized and is asserted here to be untouched.
"""

from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side

_NOW = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))
_AS_OF = date(2026, 6, 11)
_TSMC = Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                   sector="Tech", name="台積電")


def _seeder(trade_date: date) -> Callable[..., None]:
    """One TW buy on *trade_date*, priced at as_of — so the ONLY thing that varies between
    the cases below is the length of the observation window."""
    def seed(conn) -> None:                        # type: ignore[no-untyped-def]
        seed_accounts(conn)
        upsert_instrument(conn, _TSMC)
        insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                           quantity=Decimal("1000"), price=Decimal("1000"),
                           fees=Decimal("1425"), tax=Decimal("0"), trade_date=trade_date)
        upsert_prices(conn, [PriceRow(instrument="2330", market=Market.TW, as_of=_AS_OF,
                                      close=Decimal("2320"), source="test")],
                      fetched_at=datetime(2026, 6, 11, 13, 0))
    return seed


def _dash(factory: Callable[..., TestClient], trade_date: date) -> dict:  # type: ignore[type-arg]
    client = factory(_seeder(trade_date), reporting=Currency.TWD, now=_NOW)
    r = client.get("/api/dashboard")
    assert r.status_code == 200, r.text
    body: dict = r.json()                          # type: ignore[type-arg]
    return body


def test_one_day_window_reports_no_annualized_figure(
    dashboard_client_factory: Callable[..., TestClient],
) -> None:
    body = _dash(dashboard_client_factory, date(2026, 6, 10))
    assert body["kpis"]["xirr_window_days"] == 1          # the window is still reported
    assert body["kpis"]["xirr"] is None, "1.5e133 is arithmetic, not a return rate"
    reason = body["freshness"]["xirr_unavailable_reason"]
    assert reason and "1" in reason and "年化" in reason, reason


def test_the_return_itself_is_never_withheld(
    dashboard_client_factory: Callable[..., TestClient],
) -> None:
    """Only the annualization is suppressed — the cumulative return is the honest figure
    and stays on the wire, so the KPI band is not left blank."""
    body = _dash(dashboard_client_factory, date(2026, 6, 10))
    rate = Decimal(body["kpis"]["total_return_rate"])
    assert rate > Decimal("1.31") and rate < Decimal("1.32"), rate
    assert body["kpis"]["total_return"] == "1318575.000"


def test_a_window_at_the_threshold_still_annualizes(
    dashboard_client_factory: Callable[..., TestClient],
) -> None:
    """30 days is IN. The boundary is asserted from both sides so a future edit cannot
    quietly turn `<` into `<=` and swallow a whole month of legitimate figures."""
    body = _dash(dashboard_client_factory, date(2026, 5, 12))   # exactly 30 days
    assert body["kpis"]["xirr_window_days"] == 30
    assert body["kpis"]["xirr"] is not None
    assert body["freshness"]["xirr_unavailable_reason"] in (None, "")


def test_one_day_under_the_threshold_is_out(
    dashboard_client_factory: Callable[..., TestClient],
) -> None:
    body = _dash(dashboard_client_factory, date(2026, 5, 13))   # 29 days
    assert body["kpis"]["xirr_window_days"] == 29
    assert body["kpis"]["xirr"] is None


def test_a_long_window_is_unaffected(
    dashboard_client_factory: Callable[..., TestClient],
) -> None:
    """The guard must not touch ordinary ledgers — this is the case that already worked."""
    body = _dash(dashboard_client_factory, date(2025, 6, 11))   # 365 days
    assert body["kpis"]["xirr"] is not None
    assert Decimal(body["kpis"]["xirr"]) < Decimal("10")        # a readable magnitude
