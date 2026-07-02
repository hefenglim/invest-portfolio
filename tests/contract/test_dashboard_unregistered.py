"""Dashboard degradation for unregistered ledger symbols (2026-07-02).

A ledger row whose symbol has no Instrument registration cannot be booked (no quote
currency) or priced (not in the worklist). The dashboard must NEVER 500 over it:
those events are excluded from every computed number and the symbols surface in
``freshness.unregistered_symbols`` so the UI can prompt the user to register them.
The rows are seeded through the raw store (bypassing the input gate) to model
legacy/imported data that predates the hard gate.
"""

import sqlite3
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import insert_dividend, insert_transaction
from portfolio_dash.shared.models.enums import Side


def _seed_ghost_tx(conn: sqlite3.Connection) -> None:
    insert_transaction(conn, account_id="tw_broker", symbol="GHOST", side=Side.BUY,
                       quantity=Decimal("100"), price=Decimal("10"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 2, 1))
    conn.commit()


def test_dashboard_200_with_unregistered_tx(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _seed_ghost_tx(golden_db)
    r = api_client.get("/api/dashboard")
    assert r.status_code == 200
    body = r.json()
    # The ghost symbol is reported, and excluded from holdings/prices.
    assert body["freshness"]["unregistered_symbols"] == ["GHOST"]
    assert all(h["symbol"] != "GHOST" for h in body["holdings"])
    assert "GHOST" not in body["freshness"]["missing_prices"]
    # Registered holdings still compute normally (golden 2330 + AAPL).
    syms = {h["symbol"] for h in body["holdings"]}
    assert {"2330", "AAPL"} <= syms
    assert body["kpis"]["total_market_value"] is not None


def test_dashboard_200_with_unregistered_dividend(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    insert_dividend(golden_db, account_id="tw_broker", symbol="PHANTOM",
                    div_date=date(2026, 3, 15), div_type="CASH", gross=Decimal("100"),
                    withholding=Decimal("0"), net=Decimal("100"))
    golden_db.commit()
    r = api_client.get("/api/dashboard")
    assert r.status_code == 200
    assert r.json()["freshness"]["unregistered_symbols"] == ["PHANTOM"]


def test_dashboard_clean_ledger_reports_empty_list(api_client: TestClient) -> None:
    r = api_client.get("/api/dashboard")
    assert r.status_code == 200
    assert r.json()["freshness"]["unregistered_symbols"] == []
