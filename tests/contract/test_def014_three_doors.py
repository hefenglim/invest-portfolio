"""DEF-014 at the doors: the manual form, the CSV paste and the broker import all surface
the two date findings, through the ONE validator they share.

* Manual (``POST /api/input/manual/preview``): ``trade_before_opening`` arrives as
  ``sev: warn`` (the form gates its confirm on it and asks for a tick, as it does for
  ``future_trade_date``); ``trade_before_ledger_start`` arrives as ``sev: info`` (a notice,
  never gating — the severity the auto-register note already uses).
* CSV (``POST /api/import/preview`` / ``commit``): a ``trade_before_opening`` row is
  ``warn`` and the commit demands ``ack_warnings``; an advisory-only row is ``ok``, carries
  the advisory under ``info`` (and as its ``reason``, so it is visible today), and commits
  with no acknowledgement at all.
* Broker (``web/broker-import.js``): the converter's transactions go through the SAME two
  endpoints — ``/api/broker/convert`` produces CSVs, the page previews and commits them
  through ``/api/import/preview`` → ``/api/import/commit`` — so the broker door is the CSV
  door with a different file. Pinned by reading the page, so a broker-specific write path
  added later fails here first.
"""

import re
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_transaction,
    upsert_instrument,
    upsert_opening,
)
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import DashboardClientFactory

D = Decimal
_HEADER = "account,symbol,side,date,shares,price\n"
_WEB = Path(__file__).resolve().parents[2] / "web"


def _seed(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="8299", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Tech", name="群聯"))
    upsert_instrument(conn, Instrument(symbol="2884", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Financials", name="玉山金"))
    upsert_opening(conn, account_id="tw_broker", symbol="8299", shares=D("500"),
                   original_cost_total=D("200000"), build_date=date(2026, 7, 21))
    insert_transaction(conn, account_id="tw_broker", symbol="2884", side=Side.BUY,
                       quantity=D("100"), price=D("40"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 1, 5))


def _manual(client: TestClient, symbol: str, day: str) -> dict[str, Any]:
    r = client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": symbol, "side": "buy",
        "date": day, "shares": "100", "price": "45.20"})
    assert r.status_code == 200, r.text
    return {i["code"]: i for i in r.json()["issues"]}


# --- manual door --------------------------------------------------------------------------

def test_manual_preview_warns_before_the_opening_and_informs_before_the_ledger_start(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed)
    before_opening = _manual(client, "8299", "2026-07-01")
    issue = before_opening["trade_before_opening"]
    assert issue["sev"] == "warn"
    assert "2026-07-21" in issue["text"] and "{account:tw_broker}" in issue["text"]
    assert "trade_before_ledger_start" not in before_opening

    before_start = _manual(client, "2884", "2025-01-02")
    assert before_start["trade_before_ledger_start"]["sev"] == "info"
    assert "2026-01-05" in before_start["trade_before_ledger_start"]["text"]
    assert "trade_before_opening" not in before_start

    assert not {"trade_before_opening", "trade_before_ledger_start"} & set(
        _manual(client, "8299", "2026-07-21"))


# --- CSV door -----------------------------------------------------------------------------

def test_csv_preview_and_commit_gate_the_warning_and_let_the_advisory_through(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed)
    csv = (_HEADER + "tw_broker,8299,buy,2026-07-01,100,45.20\n"     # 0: before the opening
           + "tw_broker,2884,buy,2025-01-02,100,45.20\n")             # 1: before the ledger start
    rows = client.post("/api/import/preview",
                       json={"kind": "transactions", "csv_text": csv}).json()["rows"]
    assert rows[0]["status"] == "warn" and "期初庫存建檔日" in rows[0]["reason"]
    assert "info" not in rows[0]
    assert rows[1]["status"] == "ok", rows[1]
    assert rows[1]["info"] == [rows[1]["reason"]]
    assert "最早紀錄（2026-01-05）" in rows[1]["reason"]

    body: dict[str, Any] = {"kind": "transactions", "csv_text": csv}
    refused = client.post("/api/import/commit", json=body)
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "warnings_unacknowledged"

    # The advisory row ALONE needs no acknowledgement.
    only_advisory = client.post("/api/import/commit", json={
        "kind": "transactions", "csv_text": csv, "select": [1]})
    assert only_advisory.status_code == 422, "the unselected warn row still gates the file"
    advisory_file = _HEADER + "tw_broker,2884,buy,2025-01-02,100,45.20\n"
    written = client.post("/api/import/commit",
                          json={"kind": "transactions", "csv_text": advisory_file})
    assert written.status_code == 200 and written.json()["written"] == 1, written.text

    acked = client.post("/api/import/commit", json={**body, "ack_warnings": True})
    assert acked.status_code == 200 and acked.json()["written"] == 1, acked.text


# --- broker door --------------------------------------------------------------------------

def test_the_broker_page_writes_through_the_csv_door() -> None:
    """``/api/broker/convert`` only converts; the transactions it produces are previewed
    and committed by the page through the two CSV endpoints, so every finding above
    reaches a broker statement unchanged. A broker-specific write endpoint would have to
    appear here first."""
    src = (_WEB / "broker-import.js").read_text(encoding="utf-8")
    posts = set(re.findall(r"api\.post\(\s*'(/api/[^']+)'", src))
    assert "/api/broker/convert" in posts
    assert "/api/import/commit" in posts
    assert not [p for p in posts if p.startswith("/api/broker/") and p != "/api/broker/convert"], (
        f"a broker-specific write path appeared: {sorted(posts)}")
