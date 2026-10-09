"""Y3 (owner ruling 2026-10-05), through ``GET /api/dashboard``: a held position flagged
``unbookable_dividend`` keeps its value in the trend, the net-worth line and the KPI total alike.

The ledger: 2454 bought (1,000 @500), exchanged 1:1 into 2317 and still HELD, then a 3,000 cash
payout on 2454 — refused (E24) and flagged on 2317. Before the fix the trend dropped 2317's
whole value from the payout day on: the trend's last point read 0 while the KPI total read
530,000 for the same day, and the net-worth line fell by the position's value although no cash
and no share moved. The day stays marked incomplete — the payout is 待釐清, the position is not.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_corporate_action,
    insert_dividend,
    insert_transaction,
    upsert_instrument,
)
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import GOLDEN_NOW, DashboardClientFactory

D = Decimal


def _seed(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    for sym in ("2454", "2317"):
        upsert_instrument(conn, Instrument(symbol=sym, market=Market.TW,
                                           quote_ccy=Currency.TWD, sector="Electronics",
                                           name=sym, board="TWSE"))
    insert_transaction(conn, account_id="tw_broker", symbol="2454", side=Side.BUY,
                       quantity=D("1000"), price=D("500"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 1, 10))
    insert_corporate_action(conn, account_id="tw_broker", action_date=date(2026, 2, 1),
                            kind=CorporateActionKind.EXCHANGE, from_symbol="2454",
                            to_symbol="2317", ratio_to=D("1"), ratio_from=D("1"))
    insert_dividend(conn, account_id="tw_broker", symbol="2454", div_date=date(2026, 4, 1),
                    div_type="CASH", gross=D("3000"), withholding=D("0"), net=D("3000"))
    upsert_prices(conn, [
        PriceRow(instrument=s, market=Market.TW, as_of=date(2026, 1, 9), close=D("500"),
                 source="test") for s in ("2454", "2317")
    ] + [PriceRow(instrument="2317", market=Market.TW, as_of=date(2026, 6, 9),
                  close=D("530"), source="test")], fetched_at=GOLDEN_NOW)


def _points(dash: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {p["date"]: p for p in dash["trend"]["points"]}


def test_the_payout_day_keeps_the_position_value_and_is_marked(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    dash = dashboard_client_factory(_seed).get("/api/dashboard").json()
    (row,) = dash["holdings"]
    assert row["symbol"] == "2317" and row["unbookable_dividend"] is True
    pts = _points(dash)
    before, payout = pts["2026-03-31"], pts["2026-04-01"]
    assert (before["incomplete"], before["total_value"]) == (False, "500000")
    assert (payout["incomplete"], payout["total_value"]) == (True, "500000")
    # The refused payout moves no cash (K2) and no share: net worth may not move either.
    assert D(payout["net_worth"]) - D(payout["total_value"]) == (
        D(before["net_worth"]) - D(before["total_value"]))
    assert D(payout["net_worth"]) == D(before["net_worth"])


def test_the_trend_and_the_kpi_total_agree_on_the_same_day(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    dash = dashboard_client_factory(_seed).get("/api/dashboard").json()
    last = dash["trend"]["points"][-1]
    assert last["date"] == GOLDEN_NOW.date().isoformat() and last["incomplete"] is True
    assert last["total_value"] == dash["kpis"]["total_market_value"] == "530000"
    # B stays withheld — the day is still marked — and says why.
    assert dash["kpis"]["total_return_fx_complete"] is None
    assert "股利無法入帳" in dash["kpis"]["fx_complete_reason"]
