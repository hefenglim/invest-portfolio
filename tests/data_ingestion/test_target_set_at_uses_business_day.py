"""DEF-022: ``instruments.target_set_at`` is stamped with the Taipei day, not the UTC day.

Pinned at a clock reading whose UTC date is the day BEFORE — 03:00 Taipei is 19:00 UTC of
the previous day — far from any real "today", so a regression to ``datetime.now(UTC)``
(or to the host clock) cannot pass by coincidence.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

import portfolio_dash.data_ingestion.store as store
from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.store import get_instrument, upsert_instrument
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument

TAIPEI_MORNING = datetime(2030, 1, 1, 3, 0, tzinfo=ZoneInfo("Asia/Taipei"))


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    return c


def test_the_band_stamp_is_the_taipei_day(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store, "app_now", lambda: TAIPEI_MORNING)
    upsert_instrument(conn, Instrument(symbol="X", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Tech", name="X", target_low=Decimal("40")))
    inst = get_instrument(conn, "X")
    assert inst is not None
    assert inst.target_set_at == date(2030, 1, 1)      # not 2029-12-31 (the UTC day)


def test_an_explicit_today_still_wins(conn: sqlite3.Connection,
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """The injected clock (routes pass ``now.date()``) outranks the fallback."""
    monkeypatch.setattr(store, "app_now", lambda: TAIPEI_MORNING)
    upsert_instrument(conn, Instrument(symbol="X", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Tech", name="X", target_low=Decimal("40")),
                      today=date(2026, 5, 5))
    inst = get_instrument(conn, "X")
    assert inst is not None and inst.target_set_at == date(2026, 5, 5)
