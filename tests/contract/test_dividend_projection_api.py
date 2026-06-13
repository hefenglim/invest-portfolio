"""Spec 05 contract — dividend_projection in GET /api/dashboard (money as strings)."""

import sqlite3
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from portfolio_dash.pricing.results import DividendEvent
from portfolio_dash.pricing.store import upsert_dividend_events
from portfolio_dash.shared.enums import Currency, Market

_NOW = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))


def test_dividend_projection_present_empty(api_client: TestClient) -> None:
    body = api_client.get("/api/dashboard").json()
    dp = body["dividend_projection"]
    assert dp["year"] == 2026
    assert dp["basis"] == "declared_only"
    assert dp["by_currency"] == {}


def test_dividend_projection_declared(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    upsert_dividend_events(
        golden_db,
        [
            DividendEvent(
                instrument="2330", market=Market.TW, ex_date=date(2026, 12, 1),
                pay_date=None, cash_amount=Decimal("5"), stock_amount=None,
                currency=Currency.TWD, source="test",
            )
        ],
        fetched_at=_NOW,
    )
    dp = api_client.get("/api/dashboard").json()["dividend_projection"]
    tw = dp["by_currency"]["TWD"]
    assert tw["declared_net"] == "5000"
    assert tw["declared_gross"] == "5000"
    assert tw["events"] == 1
    assert isinstance(tw["declared_net"], str)  # money is a string
