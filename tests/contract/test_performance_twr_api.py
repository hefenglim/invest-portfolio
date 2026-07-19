"""Contract: GET /api/performance/twr (FU-D27) — shape, Decimal strings, FX, degrade.

Uses the ``dashboard_client_factory`` to seed custom scenarios (a small portfolio + a
benchmark series written directly into ``prices``), then asserts the endpoint's wire
shape, Decimal-string invariant, FX embedding, and honest degradation. Benchmarks are NOT
registered instruments — the rows live in ``prices`` under their storage key.
"""

import sqlite3
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.pricing.store import upsert_fx, upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import DashboardClientFactory

_NOW = datetime(2026, 6, 3, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))
_FETCHED = _NOW


def _seed_portfolio(conn: sqlite3.Connection) -> None:
    """A single TWD holding priced daily 06-01..06-03 (values 1000/1100/1200)."""
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="BBB", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Semis", name="BBB Corp", board="TWSE"))
    insert_transaction(conn, account_id="tw_broker", symbol="BBB", side=Side.BUY,
                       quantity=Decimal("100"), price=Decimal("10"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 6, 1))
    upsert_prices(conn, [
        PriceRow(instrument="BBB", market=Market.TW, as_of=date(2026, 6, 1),
                 close=Decimal("10"), source="test"),
        PriceRow(instrument="BBB", market=Market.TW, as_of=date(2026, 6, 2),
                 close=Decimal("11"), source="test"),
        PriceRow(instrument="BBB", market=Market.TW, as_of=date(2026, 6, 3),
                 close=Decimal("12"), source="test"),
    ], fetched_at=_FETCHED)


def _seed_0050(conn: sqlite3.Connection) -> None:
    """The 0050 benchmark series (TWD, no FX) written straight into ``prices``."""
    upsert_prices(conn, [
        PriceRow(instrument="0050", market=Market.TW, as_of=date(2026, 6, 1),
                 close=Decimal("100"), source="test"),
        PriceRow(instrument="0050", market=Market.TW, as_of=date(2026, 6, 2),
                 close=Decimal("110"), source="test"),
        PriceRow(instrument="0050", market=Market.TW, as_of=date(2026, 6, 3),
                 close=Decimal("99"), source="test"),
    ], fetched_at=_FETCHED)


def _seed_with_0050(conn: sqlite3.Connection) -> None:
    _seed_portfolio(conn)
    _seed_0050(conn)


def _seed_sp500_fx(conn: sqlite3.Connection) -> None:
    """The S&P 500 benchmark in USD + a USD/TWD series whose rate jumps on 06-03.

    Proves the benchmark embeds FX: 06-03 rebases to 132 (price 120 * rate 33 / base 3000)
    even though the raw index only rose to 120 — the extra came from the FX move.
    """
    _seed_portfolio(conn)
    upsert_prices(conn, [
        PriceRow(instrument="^GSPC", market=Market.US, as_of=date(2026, 6, 1),
                 close=Decimal("100"), source="test"),
        PriceRow(instrument="^GSPC", market=Market.US, as_of=date(2026, 6, 2),
                 close=Decimal("110"), source="test"),
        PriceRow(instrument="^GSPC", market=Market.US, as_of=date(2026, 6, 3),
                 close=Decimal("120"), source="test"),
    ], fetched_at=_FETCHED)
    upsert_fx(conn, [
        FxRow(base=Currency.USD, quote=Currency.TWD, as_of=date(2026, 6, 1),
              rate=Decimal("30"), source="test"),
        FxRow(base=Currency.USD, quote=Currency.TWD, as_of=date(2026, 6, 2),
              rate=Decimal("30"), source="test"),
        FxRow(base=Currency.USD, quote=Currency.TWD, as_of=date(2026, 6, 3),
              rate=Decimal("33"), source="test"),
    ], fetched_at=_FETCHED)


def test_twr_shape_decimal_strings_and_rebased_values(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed_with_0050, now=_NOW, reporting=Currency.TWD)
    r = client.get("/api/performance/twr", params={"benchmark": "0050", "window": "all"})
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert body["reason"] is None
    assert body["window"] == "all"
    assert body["as_of"] == "2026-06-03"
    assert body["benchmark"] == {"key": "0050", "label": "元大台灣50"}
    assert body["basis_notes"] == {
        "portfolio": "時間加權報酬（報告幣）",
        "benchmark": "指數價格報酬（不含股息，換算報告幣）",
    }
    pts = body["points"]
    assert [p["date"] for p in pts] == ["2026-06-01", "2026-06-02", "2026-06-03"]
    # Every money/index number is a STRING, never a JSON number.
    for p in pts:
        assert isinstance(p["portfolio"], str) and isinstance(p["benchmark"], str)
    assert [p["portfolio"] for p in pts] == ["100.0000", "110.0000", "120.0000"]
    assert [p["benchmark"] for p in pts] == ["100.0000", "110.0000", "99.0000"]


def test_twr_default_query_is_1y_window(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed_with_0050, now=_NOW, reporting=Currency.TWD)
    body = client.get("/api/performance/twr").json()  # defaults benchmark=0050, window=1y
    assert body["window"] == "1y"
    assert body["available"] is True
    assert [p["date"] for p in body["points"]] == [
        "2026-06-01", "2026-06-02", "2026-06-03"]


def test_twr_benchmark_embeds_fx(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed_sp500_fx, now=_NOW, reporting=Currency.TWD)
    body = client.get(
        "/api/performance/twr", params={"benchmark": "sp500", "window": "all"}).json()
    assert body["available"] is True
    assert body["benchmark"]["label"] == "S&P 500"
    pts = {p["date"]: p for p in body["points"]}
    # Portfolio (TWD, no FX) is unchanged; the benchmark carries the 06-03 FX jump.
    assert pts["2026-06-03"]["portfolio"] == "120.0000"
    assert pts["2026-06-03"]["benchmark"] == "132.0000"


def test_twr_degrades_when_no_benchmark_rows(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    # Portfolio present, but NO 0050 rows in `prices` -> honest degrade, never 500.
    client = dashboard_client_factory(_seed_portfolio, now=_NOW, reporting=Currency.TWD)
    body = client.get(
        "/api/performance/twr", params={"benchmark": "0050", "window": "all"}).json()
    assert body["available"] is False
    assert body["points"] == []
    assert body["reason"] is not None and "基準" in body["reason"]


def test_twr_degrades_when_no_portfolio(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    # Empty ledger (only the 0050 benchmark) -> no portfolio series -> honest degrade.
    client = dashboard_client_factory(_seed_0050, now=_NOW, reporting=Currency.TWD)
    body = client.get(
        "/api/performance/twr", params={"benchmark": "0050", "window": "all"}).json()
    assert body["available"] is False
    assert body["points"] == []
    assert body["reason"] is not None and "投資組合" in body["reason"]


def test_twr_unknown_benchmark_404(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed_with_0050, now=_NOW, reporting=Currency.TWD)
    r = client.get("/api/performance/twr", params={"benchmark": "nope", "window": "all"})
    assert r.status_code == 404
    assert "未知" in r.json()["error"]["message"]


def test_twr_unknown_window_400(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed_with_0050, now=_NOW, reporting=Currency.TWD)
    r = client.get("/api/performance/twr", params={"benchmark": "0050", "window": "5y"})
    assert r.status_code == 400
    assert "未知" in r.json()["error"]["message"]
