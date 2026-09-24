"""DEF-040 (functional test manual D-10 / OBS-1, owner ruling 2026-09-24): deleting a SPINOFF
also removes the child's SEED price it wrote — unless a real quote has replaced it since.

Measured (R2 hand-over, OBS-1): 公司行動 › 分拆 with a typed 子公司起始價 → the save writes that
price into ``prices`` after the commit → deleting the action left the price behind, so a
symbol the ledger no longer creates kept a hand-typed quote nobody could see or remove.

The ruling applies the SAME conditional-reversal rule as the target band / weight (DEF-021 /
F-3): reversed only while intact, and when it cannot be reversed the delete confirm and the
toast say why. Pinned through the real doors:

* the list row PROMISES what the delete will do (``child_price_restore``), over the whole set;
* the delete removes the seed row and reports ``restored``; a price a provider wrote over the
  seed stays and the response carries the reason;
* the CSV import-batch undo runs the same removal (it deletes through ``_delete_actions``).
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.seed import write_seed_price
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import GOLDEN_NOW

_BASE = "/api/ledgers/corporate-actions"
_DAY = date(2026, 3, 16)


def _seed_parent(conn: sqlite3.Connection) -> None:
    for symbol, name in (("PARN", "Parent"), ("CHLD", "Child")):
        upsert_instrument(conn, Instrument(symbol=symbol, market=Market.US,
                                           quote_ccy=Currency.USD, sector="Tech", name=name))
    insert_transaction(conn, account_id="schwab", symbol="PARN", side=Side.BUY,
                       quantity=Decimal("100"), price=Decimal("100"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 12))
    conn.commit()


def _spinoff(client: TestClient, *, price: str | None = "50.00") -> int:
    body: dict[str, Any] = {
        "account_id": "schwab", "date": _DAY.isoformat(), "kind": "SPINOFF",
        "from_symbol": "PARN", "to_symbol": "CHLD", "ratio_to": "1", "ratio_from": "2",
        "cost_carry": "0.2", "ack_warnings": True}
    if price is not None:
        body["to_symbol_price"] = price
    r = client.post(_BASE, json=body)
    assert r.status_code == 201, r.text
    return int(r.json()["ids"][0])


def _child_price(conn: sqlite3.Connection) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT close, source FROM prices WHERE instrument='CHLD' AND as_of_date=?",
        (_DAY.isoformat(),)).fetchone()
    return row


def _listed(client: TestClient, action_id: int) -> dict[str, Any]:
    rows = client.get(_BASE, params={"limit": 500}).json()["rows"]
    return dict(next(r for r in rows if r["id"] == action_id))


def test_deleting_the_spinoff_takes_the_seed_it_wrote(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _seed_parent(golden_db)
    action = _spinoff(api_client)
    assert _child_price(golden_db) is not None
    promise = _listed(api_client, action)["child_price_restore"]
    assert promise is not None and promise["restorable"] is True, promise

    r = api_client.delete(f"{_BASE}/{action}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["child_price_removed"] is True
    assert body["child_price_restore"]["restored"] is True
    assert _child_price(golden_db) is None, "the seed price outlived the action that wrote it"


def test_a_seed_a_real_quote_has_replaced_stays_and_the_delete_says_why(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _seed_parent(golden_db)
    action = _spinoff(api_client)
    # A history backfill later delivers the provider's close for the SAME day.
    upsert_prices(golden_db, [PriceRow(instrument="CHLD", market=Market.US, as_of=_DAY,
                                       close=Decimal("48.70"), source="yfinance")],
                  fetched_at=GOLDEN_NOW)
    promise = _listed(api_client, action)["child_price_restore"]
    assert promise["restorable"] is False and "正式報價" in promise["reason"], promise

    body = api_client.delete(f"{_BASE}/{action}").json()
    assert body["child_price_removed"] is False
    assert "yfinance" in body["child_price_restore"]["reason"]
    row = _child_price(golden_db)
    assert row is not None and row["source"] == "yfinance" and row["close"] == "48.70"


def test_a_spinoff_saved_without_a_seed_promises_nothing(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _seed_parent(golden_db)
    action = _spinoff(api_client, price=None)
    assert _listed(api_client, action)["child_price_restore"] is None
    body = api_client.delete(f"{_BASE}/{action}").json()
    assert body["child_price_removed"] is None


def test_the_import_batch_undo_takes_the_seed_too(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """The batch undo deletes through the SAME ``_delete_actions`` (I-3). A seed for the child
    on that day — written through the one seed writer — leaves with the imported SPINOFF."""
    _seed_parent(golden_db)
    r = api_client.post("/api/import/commit", json={
        "kind": "corporate_actions",
        "csv_text": "account,date,kind,from_symbol,to_symbol,ratio_to,ratio_from,cost_carry\n"
                    f"schwab,{_DAY.isoformat()},SPINOFF,PARN,CHLD,1,2,0.2\n",
        "ack_warnings": True})
    assert r.status_code == 200, r.text
    write_seed_price(golden_db, symbol="CHLD", market=Market.US, on=_DAY,
                     close=Decimal("50"), tz=GOLDEN_NOW.tzinfo)
    batch = r.json()["import_batch_id"]
    body = api_client.delete(f"/api/import/batches/{batch}").json()
    assert [c["restored"] for c in body["child_price_restore"]] == [True], body
    assert _child_price(golden_db) is None


def test_the_seed_signature_is_the_action_day_at_midnight(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """The fingerprint the removal keys on (source + action-day stamp) is what the save
    writes — pinned, so a change to the writer cannot silently strand every seed."""
    _seed_parent(golden_db)
    _spinoff(api_client)
    row = golden_db.execute(
        "SELECT source, fetched_at FROM prices WHERE instrument='CHLD'").fetchone()
    stamp = datetime.fromisoformat(row["fetched_at"])
    assert row["source"] == "manual"
    assert stamp.date() == _DAY and (stamp.hour, stamp.minute, stamp.second) == (0, 0, 0)
