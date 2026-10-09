"""I-6 (F-6): the corporate-action LIST carries the target-weight record and its promise, so
the delete confirm can state beforehand what the delete will do to BOTH per-symbol settings.

F-3 put ``weight_restored`` / ``weight_restore`` on the delete RESPONSE only. The list row —
which is what ``web/ledger.js``'s confirm dialog reads — carried ``band_move`` /
``band_restore`` (DEF-021) and nothing about the weight, so the confirm promised the band's
fate and was silent about the weight's, and the response's weight verdict was never shown
either. Same predicate as the delete (``strategy.target_weights.pending_weight_restore``), so
the sentence on screen cannot promise what the delete then declines.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import upsert_instrument
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.strategy.target_weights import save_target_weights
from tests.conftest import GOLDEN_NOW

D = Decimal
_BASE = "/api/ledgers/corporate-actions"
_WEB = Path(__file__).resolve().parents[2] / "web"


def _exchange_with_weight(client: TestClient, conn: sqlite3.Connection) -> None:
    upsert_instrument(conn, Instrument(symbol="NEWCO", market=Market.TW,
                                       quote_ccy=Currency.TWD, sector="Semis",
                                       name="NewCo", board="TWSE"))
    conn.commit()
    save_target_weights(conn, {"2330": D("0.25")}, now=GOLDEN_NOW)
    r = client.post(_BASE, json={
        "account_id": "tw_broker", "date": date(2026, 6, 10).isoformat(), "kind": "EXCHANGE",
        "from_symbol": "2330", "to_symbol": "NEWCO", "ratio_to": "1", "ratio_from": "1",
        "ack_warnings": True})
    assert r.status_code == 201, r.text


def test_the_list_row_promises_the_weight_reversal(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _exchange_with_weight(api_client, golden_db)
    (row,) = api_client.get(_BASE).json()["rows"]
    assert row["weight_move"] == {"from_symbol": "2330", "to_symbol": "NEWCO", "weight": "0.25"}
    assert row["weight_restore"]["restorable"] is True
    assert row["weight_restore"]["restored"] is False
    assert row["weight_restore"]["reason"] is None


def test_the_list_row_says_why_a_touched_weight_will_stay(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _exchange_with_weight(api_client, golden_db)
    save_target_weights(golden_db, {"NEWCO": D("0.30")}, now=GOLDEN_NOW)
    (row,) = api_client.get(_BASE).json()["rows"]
    assert row["weight_restore"]["restorable"] is False
    assert "已改動" in row["weight_restore"]["reason"]


def test_a_row_without_a_weight_record_carries_nulls(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    r = api_client.post(_BASE, json={
        "account_id": "tw_broker", "date": date(2026, 6, 10).isoformat(), "kind": "SPLIT",
        "from_symbol": "2330", "to_symbol": "2330", "ratio_to": "10", "ratio_from": "1"})
    assert r.status_code == 201, r.text
    (row,) = api_client.get(_BASE).json()["rows"]
    assert row["weight_move"] is None and row["weight_restore"] is None


def test_the_confirm_and_the_toast_read_the_weight_verdict() -> None:
    """The page half: the delete confirm quotes the list row's promise, and the toast after
    the delete reports the response's verdict — for the weight as for the band."""
    ledger = (_WEB / "ledger.js").read_text(encoding="utf-8")
    assert "a.weight_move" in ledger and "a.weight_restore" in ledger
    assert "的目標權重（" in ledger and "會自動移回" in ledger
    assert "目標權重不會移回" in ledger
    assert "resp.weight_restore" in ledger and "目標權重已移回" in ledger
    assert "目標權重未移回" in ledger
