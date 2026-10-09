"""DEF-025 (owner ruling 2026-09-24): a bulk-imported 賣超 row is written only under ITS OWN ack.

Reproduced (R1 I-06): 台灣券商 2884 holds 100 → paste 「tw_broker,2884,sell,2026-09-23,150,45.20」
into CSV 匯入 → the preview marked it 「⚠ 警告 賣出 150 股，超過持有的 100 股」 with its box
ALREADY TICKED → 確認寫入勾選列 → 422 ``warnings_unacknowledged`` → ONE generic dialog 「部分列
有警告（如賣超）— 確認後一併寫入？」 → 確認寫入 wrote it: cost basis discarded, sticky 待釐清. The
manual door asks for that exact consequence by name (``ack_oversell``); the bulk door let a
file-level ``ack_warnings: true`` stand in for it — for the CSV door, the broker statement door
(its per-row dialog was only a PAGE convention, DEF-027) and the AI door alike, because all
three commit through ``POST /api/import/commit``.

The fix is server-side, so no page can write one by sending a flag: a 賣超 row that would be
written must be named in ``ack_rows``; otherwise 422 ``oversell_rows_unacknowledged`` names
every such row and the consequence, and NOTHING is written.

Why nothing caught it: the DEF-027 guard was a string scan of ``broker-import.js`` (no literal
``ack_warnings: true``) — it certified one page's source, not the endpoint every door shares,
and four contract tests asserted the blanket ack WRITES an oversell (updated to ``ack_rows``).
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import DashboardClientFactory, _seed_golden

_HEADER = "account,symbol,side,date,shares,price\n"
_OVERSELL = "tw_broker,2884,sell,2026-06-05,150,45.20\n"   # holds 100
_CLEAN = "tw_broker,2330,buy,2026-06-05,10,600\n"


def _seed(conn: sqlite3.Connection) -> None:
    _seed_golden(conn)
    upsert_instrument(conn, Instrument(symbol="2884", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Financials", name="玉山金", board="TWSE"))
    insert_transaction(conn, account_id="tw_broker", symbol="2884", side=Side.BUY,
                       quantity=Decimal("100"), price=Decimal("30"), fees=Decimal("20"),
                       tax=Decimal("0"), trade_date=date(2026, 3, 2))
    conn.commit()


def _commit(client: TestClient, csv_text: str, **extra: Any) -> Any:
    body: dict[str, Any] = {"kind": "transactions", "csv_text": csv_text}
    body.update(extra)
    return client.post("/api/import/commit", json=body)


def _sells(client: TestClient, symbol: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = client.get("/api/ledgers/transactions").json()["rows"]
    return [r for r in rows if r["symbol"] == symbol and r["side"].lower() == "sell"]


def test_the_preview_flags_the_row_by_kind(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed)
    pv = client.post("/api/import/preview",
                     json={"kind": "transactions", "csv_text": _HEADER + _OVERSELL}).json()
    row = pv["rows"][0]
    assert row["status"] == "warn" and "sell_exceeds_holdings" in row["kinds"]


def test_the_blanket_ack_no_longer_writes_an_oversell(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The R1 reproduction, at the endpoint every bulk door shares."""
    client = dashboard_client_factory(_seed)
    csv_text = _HEADER + _CLEAN + _OVERSELL
    refused = _commit(client, csv_text, ack_warnings=True, select=[0, 1])
    assert refused.status_code == 422, refused.text
    err = refused.json()["error"]
    assert err["code"] == "oversell_rows_unacknowledged"
    assert "第 2 列 2884" in err["message"] and "成本基礎會被永久捨棄" in err["message"]
    assert [(i["row"], i["code"]) for i in err["issues"]] == [(2, "sell_exceeds_holdings")]
    # NOTHING is written — not even the clean row: the owner must answer first.
    assert _sells(client, "2884") == []
    assert client.get("/api/import/batches").json()["batches"] == []


def test_the_rows_own_ack_writes_it(dashboard_client_factory: DashboardClientFactory) -> None:
    client = dashboard_client_factory(_seed)
    csv_text = _HEADER + _CLEAN + _OVERSELL
    out = _commit(client, csv_text, ack_warnings=True, select=[0, 1], ack_rows=[1])
    assert out.status_code == 200, out.text
    assert out.json()["written"] == 2
    assert [r["shares"] for r in _sells(client, "2884")] == ["150"]


def test_an_ack_for_another_row_does_not_cover_it(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed)
    out = _commit(client, _HEADER + _CLEAN + _OVERSELL, ack_warnings=True, ack_rows=[0])
    assert out.status_code == 422
    assert out.json()["error"]["code"] == "oversell_rows_unacknowledged"
    assert _sells(client, "2884") == []


def test_a_deselected_oversell_needs_no_ack_and_is_not_written(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The ruling's default: the row is unticked, so the rest of the file writes."""
    client = dashboard_client_factory(_seed)
    out = _commit(client, _HEADER + _CLEAN + _OVERSELL, ack_warnings=True, select=[0])
    assert out.status_code == 200, out.text
    body = out.json()
    assert body["written"] == 1 and body["skipped"] == 1
    assert body["skipped_rows"][0]["code"] == "deselected"
    assert _sells(client, "2884") == []


def test_every_door_without_a_selection_meets_the_same_rule(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The AI door (no ``select``) and the broker door (``pending_actions_csv``) commit
    through the same endpoint: the same refusal, the same remedy."""
    client = dashboard_client_factory(_seed)
    ai = _commit(client, _HEADER + _OVERSELL, ack_warnings=True, source_name="AI 輸入")
    assert ai.status_code == 422
    assert ai.json()["error"]["code"] == "oversell_rows_unacknowledged"
    broker = _commit(client, _HEADER + _OVERSELL, ack_warnings=True, broker="schwab",
                     pending_actions_csv=(
                         "account,date,kind,from_symbol,to_symbol,ratio_to,ratio_from,"
                         "cost_carry,note\n"))
    assert broker.status_code == 422
    assert broker.json()["error"]["code"] == "oversell_rows_unacknowledged"
    assert _sells(client, "2884") == []


def test_a_reimported_oversell_is_a_duplicate_not_a_question(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """Re-uploading the same file: the sell is already in the ledger, so it is skipped as a
    duplicate and nobody is asked to acknowledge a row that will not be written."""
    client = dashboard_client_factory(_seed)
    first = _commit(client, _HEADER + _OVERSELL, ack_warnings=True, ack_rows=[0])
    assert first.status_code == 200, first.text
    again = _commit(client, _HEADER + _OVERSELL, ack_warnings=True)
    assert again.status_code == 200, again.text
    assert again.json()["written"] == 0 and again.json()["duplicates"] == 1
    assert len(_sells(client, "2884")) == 1


def test_the_file_level_gate_still_comes_first(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """Unchanged contract for every existing caller: no ``ack_warnings`` → the old code."""
    client = dashboard_client_factory(_seed)
    out = _commit(client, _HEADER + _OVERSELL, ack_rows=[0])
    assert out.status_code == 422
    assert out.json()["error"]["code"] == "warnings_unacknowledged"
