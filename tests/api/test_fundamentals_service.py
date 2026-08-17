"""The AV-leg runner (W3, AI-D16): held-only universe, computed by the api seam.

The scheduler's ``fundamentals_av_weekly`` job dispatches here because the held set is a
portfolio replay result that pricing/ + scheduler/ cannot compute. These tests pin the
two properties the ruling depends on: only HELD symbols reach the ingest, and the ingest
is restricted to the alphavantage source (the Saturday quota leg).
"""

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest

from portfolio_dash.api import fundamentals_service
from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.pricing import ingest
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side

_NOW = datetime(2026, 8, 17, 9, 40)


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    seed_accounts(c)
    yield c
    c.close()


def _add(conn: sqlite3.Connection, symbol: str, market: Market, *, held: bool) -> None:
    upsert_instrument(conn, Instrument(
        symbol=symbol, market=market,
        quote_ccy=Currency.USD if market is Market.US else Currency.TWD,
        sector="Tech", name=symbol,
    ))
    if held:
        insert_transaction(conn, account_id="tw_broker" if market is Market.TW else "schwab",
                           symbol=symbol, side=Side.BUY, quantity=Decimal("10"),
                           price=Decimal("100"), fees=Decimal("0"), tax=Decimal("0"),
                           trade_date=_NOW.date())
    conn.commit()


def test_av_runner_covers_held_symbols_only(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    _add(conn, "AAPL", Market.US, held=True)
    _add(conn, "2330", Market.TW, held=True)
    _add(conn, "MSFT", Market.US, held=False)  # watchlist-only: excluded
    captured: dict[str, Any] = {}

    def fake_union(conn: sqlite3.Connection, *, now: datetime, **kwargs: Any) -> int:
        captured.update(kwargs)
        return 7

    monkeypatch.setattr(ingest, "ingest_fundamentals_union", fake_union)
    written = fundamentals_service.run_fundamentals_av(conn, now=_NOW)
    assert written == 7
    assert captured["sources"] == ("alphavantage",)
    universe = captured["universe"]
    assert {r.symbol for r in universe} == {"AAPL", "2330"}
    # board survives so yf_symbol's TPEx mapping still works downstream
    assert all(isinstance(r.market, Market) for r in universe)


def test_av_runner_no_held_positions_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    _add(conn, "AAPL", Market.US, held=False)

    def fail_if_called(*args: Any, **kwargs: Any) -> int:
        raise AssertionError("ingest must not run with an empty held set")

    monkeypatch.setattr(ingest, "ingest_fundamentals_union", fail_if_called)
    assert fundamentals_service.run_fundamentals_av(conn, now=_NOW) == 0
