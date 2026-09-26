"""DEF-075 (owner ruling ② B, 2026-09-26): the watchlist 「持有」 badge and the permanent-removal
guard read the ONE "held" predicate — ``data_ingestion/holdings.py::holds_position`` — that the
archive / 移除 guard has read since DEF-064.

Measured by the verifier on 3be67db: 0056 held 5,000 shares at 台灣券商, plus a sale of 5,000
dated 2026-10-15 → the watchlist badge flipped from 「持有」 to 「觀察」, yet 移除 answered
「無法移除：持倉中的標的不可移除或封存」. The badge read ``current_shares(...) > 0`` — the net over
ALL dates — while the guard read ``holds_position``. Owner's reason for B: a future-dated sale
has not happened yet, so the position is still held.

Pinned through the real doors (``GET /api/instruments``, ``DELETE /api/instruments/{s}``,
``POST /api/instruments/{s}/purge``), with the golden clock at 2026-06-11:

* a position closed only by a FUTURE sale → 持有, 移除 422, 永久移除 422 ``held``;
* the future sale deleted → 持有 by the real 5,000 shares;
* the sale re-dated into the past → 觀察, and 移除 succeeds;
* a closed position → 觀察;
* a declared short → 持有 (a live, priced position — same predicate, same word);
* the badge and the guard never disagree, for every registered symbol.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.holdings import current_shares, holds_position
from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import GOLDEN_NOW

_ETF = "0056"
_FUTURE = date(2026, 10, 15)     # after GOLDEN_NOW (2026-06-11)
_PAST = date(2026, 3, 2)


def _tw(conn: sqlite3.Connection, symbol: str, name: str) -> None:
    upsert_instrument(conn, Instrument(symbol=symbol, market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="ETF", name=name, board="TWSE"))


def _trade(conn: sqlite3.Connection, symbol: str, side: Side, qty: str, day: date,
           *, short: bool = False) -> int:
    rid = insert_transaction(conn, account_id="tw_broker", symbol=symbol, side=side,
                             quantity=Decimal(qty), price=Decimal("35"), fees=Decimal("0"),
                             tax=Decimal("0"), trade_date=day, short_sale=short)
    conn.commit()
    return rid


@pytest.fixture
def future_sold(golden_db: sqlite3.Connection) -> tuple[sqlite3.Connection, int]:
    """0056: 5,000 shares bought in January, all 5,000 sold on a date still ahead."""
    _tw(golden_db, _ETF, "元大高股息")
    _trade(golden_db, _ETF, Side.BUY, "5000", date(2026, 1, 5))
    sell_id = _trade(golden_db, _ETF, Side.SELL, "5000", _FUTURE)
    return golden_db, sell_id


def _row(api_client: TestClient, symbol: str) -> dict[str, Any]:
    body = api_client.get("/api/instruments").json()
    row: dict[str, Any] = next(r for r in body["list"] if r["symbol"] == symbol)
    return row


def test_a_position_closed_only_by_a_future_sale_is_held_everywhere(
    api_client: TestClient, future_sold: tuple[sqlite3.Connection, int]
) -> None:
    conn, _ = future_sold
    assert current_shares(conn, "tw_broker", _ETF) == 0, "precondition: the all-dates net is 0"
    assert _row(api_client, _ETF)["held"] is True, (
        "a sale that has not happened yet leaves the position held — the badge must say 持有")
    r = api_client.delete(f"/api/instruments/{_ETF}")
    assert r.status_code == 422 and r.json()["error"]["code"] == "held", r.text
    r = api_client.post(f"/api/instruments/{_ETF}/purge")
    assert r.status_code == 422 and r.json()["error"]["code"] == "held", (
        f"永久移除 must refuse for the same reason 移除 does: {r.text}")


def test_deleting_the_future_sale_keeps_the_real_shares_held(
    api_client: TestClient, future_sold: tuple[sqlite3.Connection, int]
) -> None:
    conn, sell_id = future_sold
    r = api_client.delete(f"/api/ledgers/transactions/{sell_id}")
    assert r.status_code == 200, r.text
    assert current_shares(conn, "tw_broker", _ETF) == Decimal("5000")
    assert _row(api_client, _ETF)["held"] is True


def test_a_sale_dated_in_the_past_closes_the_position(
    api_client: TestClient, future_sold: tuple[sqlite3.Connection, int]
) -> None:
    _, sell_id = future_sold
    r = api_client.put(f"/api/ledgers/transactions/{sell_id}", json={
        "account_id": "tw_broker", "symbol": _ETF, "side": "SELL", "shares": "5000",
        "price": "35", "date": _PAST.isoformat(), "fee": "0", "tax": "0"})
    assert r.status_code == 200, r.text
    assert _row(api_client, _ETF)["held"] is False, "a sale that HAS happened closes it: 觀察"
    r = api_client.delete(f"/api/instruments/{_ETF}")
    assert r.status_code == 200, f"a closed position archives as before: {r.text}"


def test_a_closed_position_reads_watch(api_client: TestClient, golden_db: sqlite3.Connection
                                      ) -> None:
    _tw(golden_db, "2454", "聯發科")
    _trade(golden_db, "2454", Side.BUY, "1000", date(2026, 1, 5))
    _trade(golden_db, "2454", Side.SELL, "1000", _PAST)
    assert _row(api_client, "2454")["held"] is False


def test_a_declared_short_reads_held(api_client: TestClient, golden_db: sqlite3.Connection
                                    ) -> None:
    """A short is a live, priced position — the predicate that refuses to archive it is the
    one that labels it. The watchlist carries no separate 「放空」 word: the drawer and the
    dashboard already flag ``short_open``; the badge answers only 「is there a position?」."""
    _tw(golden_db, "2303", "聯電")
    _trade(golden_db, "2303", Side.SELL, "500", date(2026, 3, 2), short=True)
    assert current_shares(golden_db, "tw_broker", "2303") == Decimal("-500")
    assert _row(api_client, "2303")["held"] is True
    r = api_client.post("/api/instruments/2303/purge")
    assert r.status_code == 422 and r.json()["error"]["code"] == "held", r.text


def test_the_badge_and_the_guard_never_disagree(
    api_client: TestClient, future_sold: tuple[sqlite3.Connection, int]
) -> None:
    """For every registered symbol: badge 持有 ⇔ ``holds_position`` (the archive guard)."""
    conn, _ = future_sold
    _tw(conn, "2303", "聯電")
    _trade(conn, "2303", Side.SELL, "500", date(2026, 3, 2), short=True)
    rows = api_client.get("/api/instruments").json()["list"]
    assert rows
    for r in rows:
        assert r["held"] is holds_position(conn, r["symbol"], today=GOLDEN_NOW.date()), r
