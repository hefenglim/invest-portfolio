"""DEF-075 class (owner ruling ② B, 2026-09-26): every REGISTRY reader of 「持有」 agrees with
``data_ingestion/holdings.py::holds_position``.

The watchlist badge was the reported instance; the class scan found the same
``current_shares(...) > 0`` (net over ALL dates, long only) behind four more readers:

* ``GET /api/target-weights`` — the 預警規則 › 目標配置 badge (持有 / 觀察);
* ``GET /api/signals`` and ``GET /api/signals/{symbol}`` — the signal ``held`` flag, also fed
  to the LLM through the ``rule_signals_json`` variable;
* ``api/routers/actions.py::held_symbols`` — the quote jobs' partial-failure threshold
  (a HELD instrument lost ⇒ ``partial``);
* ``api/fundamentals_service.py::run_fundamentals_av`` — the Alpha Vantage held universe.

Each is exercised through its own public door, for the two shapes the owner's reasoning is
about — a position closed only by a FUTURE-dated sale (the sale has not happened yet, so it is
still held) and a declared short (a live, priced position) — plus a held long and a closed
position as controls. The VALUATION readers (the dashboard book cut at the valuation day:
insight / alert producers, digest, 可賣股數) are a different question and are not here.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.api import fundamentals_service
from portfolio_dash.api.routers import actions
from portfolio_dash.data_ingestion.holdings import holds_position
from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.pricing import ingest
from portfolio_dash.pricing.refs import InstrumentRef
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import GOLDEN_NOW

_FUTURE_SOLD = "0056"   # 5,000 bought in January, 5,000 sold on 2026-10-15 (after GOLDEN_NOW)
_SHORT = "2303"         # a declared short of 500
_CLOSED = "2454"        # bought and sold in the past
_LONG = "2330"          # the golden ledger's held long
_SYMBOLS = (_FUTURE_SOLD, _SHORT, _CLOSED, _LONG)


def _tw(conn: sqlite3.Connection, symbol: str) -> None:
    upsert_instrument(conn, Instrument(symbol=symbol, market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Tech", name=symbol, board="TWSE"))


def _trade(conn: sqlite3.Connection, symbol: str, side: Side, qty: str, day: date,
           *, short: bool = False) -> None:
    insert_transaction(conn, account_id="tw_broker", symbol=symbol, side=side,
                       quantity=Decimal(qty), price=Decimal("35"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=day, short_sale=short)


@pytest.fixture
def ledger(golden_db: sqlite3.Connection) -> sqlite3.Connection:
    for sym in (_FUTURE_SOLD, _SHORT, _CLOSED):
        _tw(golden_db, sym)
    _trade(golden_db, _FUTURE_SOLD, Side.BUY, "5000", date(2026, 1, 5))
    _trade(golden_db, _FUTURE_SOLD, Side.SELL, "5000", date(2026, 10, 15))
    _trade(golden_db, _SHORT, Side.SELL, "500", date(2026, 3, 2), short=True)
    _trade(golden_db, _CLOSED, Side.BUY, "1000", date(2026, 1, 5))
    _trade(golden_db, _CLOSED, Side.SELL, "1000", date(2026, 3, 2))
    golden_db.commit()
    return golden_db


def _watchlist(client: TestClient, _conn: sqlite3.Connection,
               _mp: pytest.MonkeyPatch) -> set[str]:
    return {r["symbol"] for r in client.get("/api/instruments").json()["list"] if r["held"]}


def _target_weights(client: TestClient, _conn: sqlite3.Connection,
                    _mp: pytest.MonkeyPatch) -> set[str]:
    r = client.get("/api/target-weights")
    assert r.status_code == 200, r.text
    return {s["symbol"] for s in r.json()["symbols"] if s["held"]}


def _signals_list(client: TestClient, _conn: sqlite3.Connection,
                  _mp: pytest.MonkeyPatch) -> set[str]:
    r = client.get("/api/signals")
    assert r.status_code == 200, r.text
    return {s["symbol"] for s in r.json()["signals"] if s["held"]}


def _signal_one(client: TestClient, _conn: sqlite3.Connection,
                _mp: pytest.MonkeyPatch) -> set[str]:
    out: set[str] = set()
    for sym in _SYMBOLS:
        r = client.get(f"/api/signals/{sym}")
        assert r.status_code == 200, r.text
        if r.json()["held"]:
            out.add(sym)
    return out


def _quote_threshold(_client: TestClient, conn: sqlite3.Connection,
                     mp: pytest.MonkeyPatch) -> set[str]:
    """The scheduler seam takes only a connection, so "today" is the app clock — pinned to
    the golden clock here (the ledger's dates are relative to it)."""
    mp.setattr(actions, "app_now", lambda: GOLDEN_NOW, raising=False)
    return actions.held_symbols(conn)


def _av_universe(_client: TestClient, conn: sqlite3.Connection,
                 mp: pytest.MonkeyPatch) -> set[str]:
    seen: list[str] = []

    def fake(c: sqlite3.Connection, *, now: datetime, sources: tuple[str, ...],
             universe: list[InstrumentRef]) -> int:
        seen.extend(r.symbol for r in universe)
        return 0

    mp.setattr(ingest, "ingest_fundamentals_union", fake)
    fundamentals_service.run_fundamentals_av(conn, now=GOLDEN_NOW)
    return set(seen)


_READERS: dict[str, Callable[[TestClient, sqlite3.Connection, pytest.MonkeyPatch], set[str]]] = {
    "watchlist badge": _watchlist,
    "target-weights badge": _target_weights,
    "signals list": _signals_list,
    "signal drawer": _signal_one,
    "quote-job threshold": _quote_threshold,
    "alpha-vantage universe": _av_universe,
}


@pytest.mark.parametrize("reader", sorted(_READERS))
def test_every_registry_reader_agrees_with_holds_position(
    reader: str, api_client: TestClient, ledger: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {s for s in _SYMBOLS
                if holds_position(ledger, s, today=GOLDEN_NOW.date())}
    assert expected == {_FUTURE_SOLD, _SHORT, _LONG}, "fixture: the predicate itself"
    got: Any = _READERS[reader](api_client, ledger, monkeypatch) & set(_SYMBOLS)
    assert got == expected, (
        f"{reader}: held={sorted(got)} but holds_position says {sorted(expected)} — "
        f"missing {sorted(expected - got)}, extra {sorted(got - expected)}")
