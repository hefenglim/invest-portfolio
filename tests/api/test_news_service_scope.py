"""Unit tests for news_service scope resolution (P3 batch 3 · 3C).

``run_news_for`` is the injectable core over an EXPLICIT (symbol, market) universe;
``run_news_daily`` stays held-only (dashboard holdings); ``resolve_news_scope`` maps a
manual scope to the registry universe ("all" = held ∪ watchlist, a bare symbol = that one,
unknown = None -> the router 400s). These assert the universe resolution WITHOUT touching
the network (run_news_for is monkeypatched to capture its argument)."""

import sqlite3
from collections.abc import Iterable

import pytest

from portfolio_dash.api import news_service
from portfolio_dash.data_ingestion.store import upsert_instrument
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from tests.conftest import GOLDEN_NOW


def test_resolve_scope_all_is_every_registered(golden_db: sqlite3.Connection) -> None:
    universe = news_service.resolve_news_scope(golden_db, "all")
    assert universe is not None
    assert sorted(universe) == [("2330", "TW"), ("AAPL", "US")]


def test_resolve_scope_single_symbol_from_registry(golden_db: sqlite3.Connection) -> None:
    assert news_service.resolve_news_scope(golden_db, "2330") == [("2330", "TW")]
    assert news_service.resolve_news_scope(golden_db, "AAPL") == [("AAPL", "US")]


def test_resolve_scope_unknown_is_none(golden_db: sqlite3.Connection) -> None:
    assert news_service.resolve_news_scope(golden_db, "NOSUCH") is None
    assert news_service.resolve_news_scope(golden_db, "") is None


def test_scope_all_includes_watchlist_but_daily_stays_held_only(
    golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registered-but-unheld (watchlist) symbol IS in scope "all" but NOT the nightly
    held-only run."""
    upsert_instrument(golden_db, Instrument(
        symbol="MSFT", market=Market.US, quote_ccy=Currency.USD,
        sector="Tech", name="Microsoft"))

    all_universe = news_service.resolve_news_scope(golden_db, "all")
    assert all_universe is not None and ("MSFT", "US") in all_universe

    captured: dict[str, list[tuple[str, str]]] = {}

    def _fake_run_news_for(
        conn: sqlite3.Connection, symbols: Iterable[tuple[str, str]], *, now: object
    ) -> dict[str, int]:
        captured["symbols"] = list(symbols)
        return {"organized": 0}

    monkeypatch.setattr(news_service, "run_news_for", _fake_run_news_for)
    news_service.run_news_daily(golden_db, now=GOLDEN_NOW)
    # golden holds 2330 (tw_broker) + AAPL (schwab); MSFT is watchlist-only.
    assert set(captured["symbols"]) == {("2330", "TW"), ("AAPL", "US")}
    assert ("MSFT", "US") not in captured["symbols"]
