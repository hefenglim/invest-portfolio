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
cannot be forced). An item with a SOFT finding stays confirmable and its note says why —
owner rulings 2026-10-05: ② made ``ex_date_after_pay_date`` a warning the owner confirms
(CR-09 shipped it hard), and ⑩ keeps soft findings in the inbox note, where confirming the
item is the owner's acknowledgement of what it shows.

S11 (round 3): the inbox's own copy of the CR-02 no-position check — a second ``position_exists_by``
call per item with its own wording — is gone; the validator's `dividend_without_position` is
the one rule and the one sentence, joined with every other hard reason the row carries.
"""

import sqlite3
from datetime import UTC, datetime
from datetime import date as ddate
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

from portfolio_dash.api import dividend_inbox
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


def test_an_event_paid_before_its_ex_date_says_why_and_still_confirms(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    # Golden: tw_broker bought 2330 on 2026-01-05, so the position exists on both dates —
    # only the date order is unusual. Rulings ② + ⑩: a warning in the note, not a block.
    upsert_dividend_events(golden_db, [_event("2026-05-20", "2026-05-10")], fetched_at=_NOW)
    item = next(x for x in _rows(api_client) if x["ex_date"] == "2026-05-20")
    assert item["confirmable"] is True
    assert "除息日" in str(item["note"]) and "晚於發放日" in str(item["note"])
    before = len(_ledger(api_client))
    r = api_client.post("/api/dividend-inbox/confirm",
                        json={"fingerprints": [item["fingerprint"]]})
    assert r.status_code == 200 and r.json()["written"] == 1
    assert len(_ledger(api_client)) == before + 1


def test_a_stock_distribution_paid_before_its_ex_date_says_why_and_still_confirms(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    upsert_dividend_events(golden_db, [_event("2026-05-20", "2026-05-10", cash=None,
                                              stock="1")], fetched_at=_NOW)
    item = next(x for x in _rows(api_client) if x["kind"] == "stock")
    assert item["confirmable"] is True
    assert "晚於發放日" in str(item["note"])
    r = api_client.post("/api/dividend-inbox/confirm",
                        json={"fingerprints": [item["fingerprint"]]})
    assert r.json()["written"] == 1


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


def _late_aapl(api_client: TestClient, golden_db: sqlite3.Connection) -> list[dict[str, Any]]:
    """moomoo_my opens AAPL on 05-05; the provider's event goes ex 05-20 but "pays" 05-01 —
    before the position existed. (moomoo_my, not schwab: the golden ledger already holds
    AAPL at schwab since January.) No stored close near 05-01, so the DRIP estimate has no
    reinvest price either: three reasons on one item."""
    insert_transaction(golden_db, account_id="moomoo_my", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("5"), price=Decimal("100"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=ddate(2026, 5, 5))
    upsert_dividend_events(golden_db, [DividendEvent(
        instrument="AAPL", market=Market.US, ex_date=ddate(2026, 5, 20),
        pay_date=ddate(2026, 5, 1), cash_amount=Decimal("0.25"), currency=Currency.USD,
        source="yfinance")], fetched_at=_NOW)
    return [x for x in _rows(api_client)
            if x["account_id"] == "moomoo_my" and x["ex_date"] == "2026-05-20"]


def test_the_no_position_refusal_is_the_validators_and_names_every_reason(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """S11: the no-position reason now comes from ``validate_dividend``'s
    ``dividend_without_position`` (the wording legitimately changes from the inbox's own
    「付款日 … 早於此帳戶在 … 的第一筆持倉紀錄」 to the validator's sentence), and an item that
    was already blocked by a missing reinvest price still names it — every reason is shown,
    none replaces another."""
    rows = _late_aapl(api_client, golden_db)
    assert len(rows) == 1, rows
    item = rows[0]
    note = str(item["note"])
    assert item["confirmable"] is False
    assert "沒有任何持倉紀錄" in note          # the validator's CR-02 sentence
    assert "晚於發放日" in note                # the validator's CR-09 sentence
    assert "缺再投資價" in note                # the item's own reason, kept
    assert "早於此帳戶在" not in note          # the bespoke copy is gone
    assert "必須有股數" not in note            # …and the price reason is not restated as a hard one
    r = api_client.post("/api/dividend-inbox/confirm",
                        json={"fingerprints": [item["fingerprint"]]})
    assert r.json()["written"] == 0


def test_the_inbox_keeps_no_copy_of_the_position_rule() -> None:
    """One rule, one copy: the position question is asked by the validator, once per item."""
    assert not hasattr(dividend_inbox, "position_exists_by")