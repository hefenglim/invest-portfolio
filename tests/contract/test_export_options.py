"""POST /api/export/options — the open option contracts as their own CSV (owner ruling
2026-10-10: B). The holdings CSV is the stock table only; this door carries the contracts, with
the same body, the same (account, market) filter and the same filename / footer conventions as
``/api/export/holdings`` (``tests/contract/test_export_holdings.py``).
"""

from __future__ import annotations

import csv
import io
import sqlite3
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.register import register_option_contract
from portfolio_dash.data_ingestion.store import insert_transaction
from portfolio_dash.export.holdings import OPTION_COLUMNS
from portfolio_dash.shared.models.enums import Side

PUT = "AAPL261218P00100000"


def _add_put(conn: sqlite3.Connection) -> None:
    """The golden ledger + one sold AAPL put (2 口) in schwab, open on the golden day."""
    register_option_contract(conn, PUT)
    insert_transaction(conn, account_id="schwab", symbol=PUT, side=Side.SELL,
                       quantity=Decimal("200"), price=Decimal("2.00"), fees=Decimal("1.30"),
                       tax=Decimal("0"), trade_date=date(2026, 3, 2), short_sale=True)
    conn.commit()


def _rows(content: bytes) -> list[list[str]]:
    assert content[:3] == b"\xef\xbb\xbf"
    text = content[3:].decode("utf-8")
    return [r for r in csv.reader(io.StringIO(text)) if not (r and r[0].startswith("#"))]


def test_export_options_csv(api_client: TestClient, golden_db: sqlite3.Connection) -> None:
    _add_put(golden_db)
    r = api_client.post("/api/export/options")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    assert "options_snapshot_2026-06-11.csv" in r.headers["content-disposition"]
    rows = _rows(r.content)
    assert rows[0] == OPTION_COLUMNS
    [put] = [dict(zip(OPTION_COLUMNS, row, strict=True)) for row in rows[1:]]
    assert {k: put[k] for k in ("account_id", "symbol", "contract", "direction", "contracts",
                                "share_equivalents", "premium_total", "obligation")} == {
        "account_id": "schwab", "symbol": PUT, "contract": "AAPL 2026-12-18 100 Put",
        "direction": "short", "contracts": "2", "share_equivalents": "-200",
        "premium_total": "398.7000", "obligation": "20000"}
    text = r.content[3:].decode("utf-8")
    assert "# as_of=2026-06-11, fx_rates=" in text and "filter:" not in text


def test_export_options_csv_follows_the_filter(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    _add_put(golden_db)
    r = api_client.post("/api/export/options", json={"account": "tw_broker"})
    assert r.status_code == 200
    assert "options_snapshot_2026-06-11_tw_broker_all.csv" in r.headers["content-disposition"]
    assert _rows(r.content) == [OPTION_COLUMNS]
    assert "filter: account=tw_broker, market=all" in r.content.decode("utf-8-sig")
    r = api_client.post("/api/export/options", json={"market": "US"})
    assert "options_snapshot_2026-06-11_all_US.csv" in r.headers["content-disposition"]
    assert [row[1] for row in _rows(r.content)[1:]] == [PUT]


def test_export_options_csv_refuses_an_unknown_filter_key(api_client: TestClient) -> None:
    """The holdings body's ``extra="forbid"`` (audit L3): a misspelled key never widens the
    scope silently — it is the app's loud 400 validation_error."""
    r = api_client.post("/api/export/options", json={"acount": "schwab"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "validation_error"


def test_export_options_csv_on_a_stock_ledger_is_the_header_alone(
    api_client: TestClient,
) -> None:
    r = api_client.post("/api/export/options", json={})
    assert r.status_code == 200
    assert "options_snapshot_2026-06-11.csv" in r.headers["content-disposition"]
    assert _rows(r.content) == [OPTION_COLUMNS]


def test_the_holdings_csv_beside_it_carries_no_contract(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """B: the two files split the tables — the holdings CSV keeps its stock rows and the
    footer line naming the options' part of the KPI, and no contract row."""
    _add_put(golden_db)
    r = api_client.post("/api/export/holdings")
    text = r.content[3:].decode("utf-8")
    assert PUT not in text and "選擇權" not in text
    assert "\r\n2330," in text and "\r\nAAPL," in text
    assert "# total_market_value=" in text and "options_market_value=" in text


def test_export_options_audit_in_action_log(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    api_client.post("/api/export/options")
    log = api_client.get("/api/system-log", params={"limit": 20}).json()["rows"]
    assert any(x["action"] == "匯出報表" and x["path"] == "/api/export/options" for x in log)
