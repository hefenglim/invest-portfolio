"""GET /api/input/holdings lists a position a corporate action created (owner 2026-10-10).

The picker collected its symbols from trades, openings and dividends only, so a holding that
exists ONLY because an EXCHANGE moved shares onto it, or a SPINOFF carved it off its parent,
was missing from 已持有 — no sell hint, no dividend picker entry — until the owner happened to
trade it. Found while building the options ledger (an OPTION_ADJUST destination had the same
gap, fixed first for options only); the owner ruled the stock case fixed too.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import insert_corporate_action, upsert_instrument
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument


def _us(conn: sqlite3.Connection, symbol: str, name: str) -> None:
    upsert_instrument(conn, Instrument(symbol=symbol, market=Market.US, quote_ccy=Currency.USD,
                                       sector="Tech", name=name))


def _holdings(client: TestClient) -> tuple[dict[str, Any], set[str]]:
    body = client.get("/api/input/holdings", params={"account": "schwab"}).json()
    return {r["symbol"]: r for r in body["held"]}, {r["symbol"] for r in body["closed"]}


def test_an_exchange_destination_with_no_trade_of_its_own_is_held(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    # The golden ledger holds 10 AAPL in schwab (bought 2026-01-10).
    _us(golden_db, "NEWX", "New X")
    insert_corporate_action(golden_db, account_id="schwab", action_date=date(2026, 3, 2),
                            kind=CorporateActionKind.EXCHANGE, from_symbol="AAPL",
                            to_symbol="NEWX", ratio_to=Decimal(1), ratio_from=Decimal(1))
    held, closed = _holdings(api_client)
    assert held["NEWX"]["shares"] == "10"
    assert held["NEWX"]["adjusted_avg"] is not None
    assert "AAPL" in closed and "AAPL" not in held


def test_a_spinoff_child_with_no_trade_of_its_own_is_held(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    _us(golden_db, "KIDX", "Kid X")
    insert_corporate_action(golden_db, account_id="schwab", action_date=date(2026, 3, 2),
                            kind=CorporateActionKind.SPINOFF, from_symbol="AAPL",
                            to_symbol="KIDX", ratio_to=Decimal(1), ratio_from=Decimal(2),
                            cost_carry=Decimal("0.1"))
    held, _closed = _holdings(api_client)
    assert held["KIDX"]["shares"] == "5"
    assert held["AAPL"]["shares"] == "10"


def test_a_destination_dated_after_today_is_not_listed_yet(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """DEF-056: a row dated after the valuation day does not count yet — an action too."""
    _us(golden_db, "LATE", "Late X")
    insert_corporate_action(golden_db, account_id="schwab", action_date=date(2026, 12, 1),
                            kind=CorporateActionKind.EXCHANGE, from_symbol="AAPL",
                            to_symbol="LATE", ratio_to=Decimal(1), ratio_from=Decimal(1))
    held, closed = _holdings(api_client)
    assert "LATE" not in held and "LATE" not in closed
    assert held["AAPL"]["shares"] == "10"
