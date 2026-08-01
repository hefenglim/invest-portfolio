"""API guard rails for the declared short sale + the date-aware 賣超 check (2026-07-31)."""

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import insert_dividend
from portfolio_dash.shared.models.enums import DividendType


def _commit(client: TestClient, **kw: Any) -> Any:
    body = {"account_id": "schwab", "symbol": "AAPL", "side": "sell",
            "date": "2026-06-10", "shares": "100", "price": "260"}
    body.update(kw)
    return client.post("/api/input/manual/commit", json=body)


def test_backdated_sell_covered_only_by_a_later_buy_is_blocked(
    api_client: TestClient,
) -> None:
    """The 2026-07-30 defect: `current_shares` nets across ALL dates, so a sell dated before
    the buy that covers it slipped through and the replay then discarded the cost basis."""
    api_client.post("/api/input/manual/commit", json={
        "account_id": "schwab", "symbol": "AAPL", "side": "buy",
        "date": "2026-07-23", "shares": "100", "price": "366"})
    r = _commit(api_client)                      # dated BEFORE that buy
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "oversell_unacknowledged"
    msg = " ".join(i["text"] for i in r.json()["error"].get("issues", []))
    assert "2026-06-10" in msg, msg               # names the date it was short on


def test_declared_short_sale_needs_no_acknowledgement(api_client: TestClient) -> None:
    r = _commit(api_client, short_sale=True)
    assert r.status_code == 201, r.text
    rows = api_client.get("/api/ledgers/transactions", params={"limit": 500}).json()["rows"]
    row = next(t for t in rows if t["id"] == r.json()["txn_id"])
    assert row["side"] == "sell" and row["shares"] == "100"


def test_short_position_is_reported_signed_and_labelled(api_client: TestClient) -> None:
    _commit(api_client, short_sale=True)
    h = next(x for x in api_client.get("/api/dashboard").json()["holdings"]
             if x["symbol"] == "AAPL" and x["account_id"] == "schwab")
    assert h["shares"].startswith("-")
    assert h["short_open"] is True and h["oversold"] is False


def test_short_flag_is_off_by_default_so_a_typo_cannot_become_a_short(
    api_client: TestClient,
) -> None:
    r = _commit(api_client)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "oversell_unacknowledged"


def _open_short_with_dividend(client: TestClient, conn: sqlite3.Connection) -> None:
    """A ledger the strict replay cannot book: a dividend inside an open-short window.

    The dividend is written straight to the store because the app has no single-row
    dividend POST (that form commits through the CSV import path); the point of these
    tests is the READ side's behaviour on such a ledger, not how it got there.
    """
    client.post("/api/input/manual/commit", json={
        "account_id": "schwab", "symbol": "AAPL", "side": "sell", "date": "2026-06-10",
        "shares": "100", "price": "260", "short_sale": True})
    insert_dividend(conn, account_id="schwab", symbol="AAPL", div_date=date(2026, 6, 20),
                    div_type=DividendType.CASH, gross=Decimal("50"),
                    withholding=Decimal("0"), net=Decimal("50"))


def test_recompute_answers_422_not_500_on_an_unbookable_ledger(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """never-500 at EVERY build_book call site: the strict replay's refusal must reach the
    user as an actionable 4xx, not an internal error (found by the phase-2 audit)."""
    _open_short_with_dividend(api_client, golden_db)
    r = api_client.post("/api/actions/recompute", json={})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "unbookable_ledger"
    assert "放空" in r.json()["error"]["message"]


def test_tax_export_answers_422_not_500_on_an_unbookable_ledger(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    _open_short_with_dividend(api_client, golden_db)
    r = api_client.post("/api/export/tax-package", json={"year": 2026})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "unbookable_ledger"


def test_dashboard_still_degrades_instead_of_failing(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """The same ledger must NOT break the dashboard — it flags the position instead."""
    _open_short_with_dividend(api_client, golden_db)
    r = api_client.get("/api/dashboard")
    assert r.status_code == 200
    h = next(x for x in r.json()["holdings"]
             if x["symbol"] == "AAPL" and x["account_id"] == "schwab")
    assert h["unbookable_dividend"] is True and h["short_open"] is True


def test_short_sale_is_readable_back_from_the_ledger_wire(api_client: TestClient) -> None:
    """A flag that changes the row's ACCOUNTING must be on the public read surface, or the
    book cannot be rebuilt from the ledger (domain-ledger.md)."""
    r = api_client.post("/api/input/manual/commit", json={
        "account_id": "schwab", "symbol": "AAPL", "side": "sell", "date": "2026-06-10",
        "shares": "100", "price": "260", "short_sale": True})
    rows = api_client.get("/api/ledgers/transactions", params={"limit": 500}).json()["rows"]
    row = next(t for t in rows if t["id"] == r.json()["txn_id"])
    assert row["short_sale"] is True
    assert all(t["short_sale"] is False for t in rows if t["id"] != r.json()["txn_id"])
