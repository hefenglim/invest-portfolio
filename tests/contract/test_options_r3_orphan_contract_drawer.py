"""Options — final review R3 n3: a registered contract with NO ledger rows (spec §6.2 Drawer).

The drawer branched on the dashboard's ``options`` block, which exists only once the ledger
carries an option row. A contract that is registered but has no row — its last row deleted, or
reached through the watchlist's 「顯示選擇權合約」 — therefore got the STOCK drawer: a price
chart for an instrument that is never quoted, and a 試算 the what-if engine then refuses. The
branch is the instrument's own fact (``is_option``): the contract header, 「選擇權不提供試算」,
no open position, no trade.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from portfolio_dash.data_ingestion.register import register_option_contract
from tests.conftest import DashboardClientFactory, _seed_golden

_NOW = datetime(2026, 10, 9, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))
ORPHAN = "AAPL261218P00150000"


def _orphan(conn: sqlite3.Connection) -> None:
    _seed_golden(conn)
    register_option_contract(conn, ORPHAN)
    conn.commit()


def test_a_contract_with_no_rows_gets_the_contract_drawer(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_orphan, now=_NOW)
    body = client.get(f"/api/symbol/{ORPHAN}/detail").json()
    opt = body["options"]
    assert opt["is_contract"] is True
    assert opt["contract"]["name"] == "AAPL 2026-12-18 150 Put"
    assert (opt["contract"]["underlying"], opt["contract"]["right"]) == ("AAPL", "P")
    assert opt["whatif_note"] == "選擇權不提供試算"
    assert opt["open"] == [] and opt["refused_closes"] == []
    assert body["activity"] == [] and body["realized_rows"] == []
    assert body["position"] is None and body["cost_basis"] is None
    assert body["price_history"]["available"] is False
    assert body["price_history"]["note"].startswith("選擇權合約不抓取報價")


def test_the_stock_drawer_on_that_ledger_is_unchanged(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """Invariant 1: a registered-only contract puts no option key on a STOCK's drawer."""
    client = dashboard_client_factory(_orphan, now=_NOW)
    body = client.get("/api/symbol/AAPL/detail").json()
    assert "options" not in body
    for row in body["activity"]:
        assert not {"contracts", "option_side", "close_reason", "refused_close"} & set(row)
