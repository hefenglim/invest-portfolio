"""Round 2 (a), CR-09's class: the dividend inbox's confirm door runs the ONE dividend validator.

DEF-061 made ``validate_dividend`` the validator every door that writes a dividend runs — the
CSV / manual / AI / broker doors and the ledger correction door — and CR-09 added its
``ex_date_after_pay_date`` refusal. The inbox confirm (``POST /api/dividend-inbox/confirm``)
was the one door left: it wrote straight through ``insert_dividend``. A provider event whose
payment date precedes its ex-date (bad provider data — no distribution pays before it goes ex)
was booked as a row the other doors refuse, and a 配股 so booked counts from an ex-date after
its own payment.

Now ``detect`` asks the validator about the exact row ``confirm`` would write; an item with a
hard finding is not confirmable and says why (and ``confirm`` recomputes ``detect``, so it
cannot be forced). Fixer A's no-position refusal (CR-02) keeps its own sentence.
"""

import sqlite3
from datetime import UTC, datetime
from datetime import date as ddate
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import insert_transaction
from portfolio_dash.pricing.results import DividendEvent
from portfolio_dash.pricing.store import upsert_dividend_events
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.enums import Side

_NOW = datetime(2026, 6, 11, tzinfo=UTC)


def _event(ex: str, pay: str, *, cash: str | None = "2.75",
           stock: str | None = None) -> DividendEvent:
    return DividendEvent(
        instrument="2330", market=Market.TW, ex_date=ddate.fromisoformat(ex),
        pay_date=ddate.fromisoformat(pay),
        cash_amount=Decimal(cash) if cash is not None else None,
        stock_amount=Decimal(stock) if stock is not None else None,
        currency=Currency.TWD, source="finmind")


def _rows(api_client: TestClient) -> list[dict[str, Any]]:
    r = api_client.get("/api/dividend-inbox")
    assert r.status_code == 200
    return list(r.json()["rows"])


def _ledger(api_client: TestClient) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = api_client.get(
        "/api/ledgers/dividends", params={"symbol": "2330", "limit": 500}).json()["rows"]
    return rows


def test_an_event_paid_before_its_ex_date_is_not_confirmable_and_says_why(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    # Golden: tw_broker bought 2330 on 2026-01-05, so the position exists on both dates —
    # only the date order is wrong.
    upsert_dividend_events(golden_db, [_event("2026-05-20", "2026-05-10")], fetched_at=_NOW)
    item = next(x for x in _rows(api_client) if x["ex_date"] == "2026-05-20")
    assert item["confirmable"] is False
    assert "除息日" in str(item["note"]) and "晚於發放日" in str(item["note"])
    before = len(_ledger(api_client))
    r = api_client.post("/api/dividend-inbox/confirm",
                        json={"fingerprints": [item["fingerprint"]]})
    assert r.status_code == 200 and r.json()["written"] == 0      # cannot be forced
    assert len(_ledger(api_client)) == before


def test_a_stock_distribution_paid_before_its_ex_date_is_not_confirmable(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    upsert_dividend_events(golden_db, [_event("2026-05-20", "2026-05-10", cash=None,
                                              stock="1")], fetched_at=_NOW)
    item = next(x for x in _rows(api_client) if x["kind"] == "stock")
    assert item["confirmable"] is False
    assert "晚於發放日" in str(item["note"])
    r = api_client.post("/api/dividend-inbox/confirm",
                        json={"fingerprints": [item["fingerprint"]]})
    assert r.json()["written"] == 0


def test_a_well_dated_event_still_confirms(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    upsert_dividend_events(golden_db, [_event("2026-05-20", "2026-06-05", stock="1")],
                           fetched_at=_NOW)
    items = [x for x in _rows(api_client) if x["ex_date"] == "2026-05-20"]
    assert {x["kind"] for x in items} == {"cash", "stock"}
    assert all(x["confirmable"] for x in items), items
    r = api_client.post("/api/dividend-inbox/confirm",
                        json={"fingerprints": [x["fingerprint"] for x in items]})
    assert r.json()["written"] == 2


def test_the_no_position_refusal_keeps_its_own_sentence(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """CR-02 (Fixer A): a payment dated before the account's first row of the symbol is
    refused with that reason — the validator's date-order sentence does not replace it."""
    # moomoo_my, not schwab: the golden ledger already holds AAPL at schwab since January.
    insert_transaction(golden_db, account_id="moomoo_my", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("5"), price=Decimal("100"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=ddate(2026, 5, 5))
    upsert_dividend_events(golden_db, [DividendEvent(
        instrument="AAPL", market=Market.US, ex_date=ddate(2026, 5, 20),
        pay_date=ddate(2026, 5, 1), cash_amount=Decimal("0.25"), currency=Currency.USD,
        source="yfinance")], fetched_at=_NOW)
    rows = [x for x in _rows(api_client)
            if x["account_id"] == "moomoo_my" and x["ex_date"] == "2026-05-20"]
    late = [x for x in rows if "早於此帳戶" in str(x["note"])]
    assert late and all(x["confirmable"] is False for x in late), rows
