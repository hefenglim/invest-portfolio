"""Contract tests for POST /api/export/realized (realized-P&L reconciliation CSV)."""

import sqlite3
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import insert_transaction
from portfolio_dash.shared.models.enums import Side

_HEADER = ("account_id,symbol,quote_ccy,sell_date,shares_sold,proceeds_net,"
           "original_cost_removed,adjusted_cost_removed,realized")


def test_export_realized_empty_golden_header_only(api_client: TestClient) -> None:
    r = api_client.post("/api/export/realized", json={})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "realized_pnl_2026-06-11.csv" in r.headers["content-disposition"]
    assert r.content[:3] == b"\xef\xbb\xbf"
    text = r.content[3:].decode("utf-8")
    assert text.split("\r\n", 1)[0] == _HEADER
    body = [ln for ln in text.split("\r\n") if ln and not ln.startswith("#")]
    assert body == [_HEADER]  # golden has no sells


def test_export_realized_row_matches_dashboard_core(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    insert_transaction(golden_db, account_id="tw_broker", symbol="2330", side=Side.SELL,
                       quantity=Decimal("100"), price=Decimal("600"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 6, 10))
    golden_db.commit()
    # The exported figures must EXACTLY equal what the calc core reports on the dashboard.
    dash = api_client.get("/api/dashboard").json()
    rows = dash["realized"]["rows"]
    assert rows, "the sell must produce a realized row"
    row = next(r for r in rows if r["symbol"] == "2330")

    text = api_client.post("/api/export/realized", json={}).content[3:].decode("utf-8")
    assert row["realized"] in text
    assert row["proceeds_net"] in text
    assert row["adjusted_cost_removed"] in text
    assert f'2330,{row["quote_ccy"]},' in text
