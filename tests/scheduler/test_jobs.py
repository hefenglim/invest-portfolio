import sqlite3
from datetime import UTC, datetime

import pytest

from portfolio_dash.scheduler import jobs as jobs_mod
from portfolio_dash.scheduler.jobs import quotes_us, refresh_quotes_for
from portfolio_dash.shared.enums import Market

_NOW = datetime(2026, 6, 10, tzinfo=UTC)


class _Summary:
    def __init__(self) -> None:
        self.ok = {"AAPL": "yfinance"}
        self.failed: list[str] = []


def _add(conn: sqlite3.Connection, symbol: str, market: str) -> None:
    conn.execute(
        "INSERT INTO instruments (symbol, market, quote_ccy, sector, name, board) "
        "VALUES (?, ?, 'X', NULL, NULL, NULL)",
        (symbol, market),
    )
    conn.commit()


def test_quotes_job_passes_market_worklist(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    _add(conn, "AAPL", "US")
    _add(conn, "2330", "TW")
    captured: dict[str, object] = {}

    monkeypatch.setattr(jobs_mod, "default_registry", lambda: "REG")

    def fake_refresh(c, registry, instruments, fx_pairs, *, now):  # type: ignore[no-untyped-def]
        captured["registry"] = registry
        captured["symbols"] = [i.symbol for i in instruments]
        captured["fx"] = len(fx_pairs)
        return _Summary()

    monkeypatch.setattr(jobs_mod, "refresh_quotes", fake_refresh)
    detail = quotes_us(conn, now=_NOW)
    assert captured["registry"] == "REG"
    assert captured["symbols"] == ["AAPL"]  # only US, not TW
    assert "1 ok" in detail and "0 failed" in detail


def test_refresh_quotes_for_filters_by_market(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    _add(conn, "2330", "TW")
    monkeypatch.setattr(jobs_mod, "default_registry", lambda: "REG")
    seen: dict[str, object] = {}

    def fake_refresh(c, registry, instruments, fx_pairs, *, now):  # type: ignore[no-untyped-def]
        seen["symbols"] = [i.symbol for i in instruments]
        return _Summary()

    monkeypatch.setattr(jobs_mod, "refresh_quotes", fake_refresh)
    refresh_quotes_for(conn, Market.TW, now=_NOW)
    assert seen["symbols"] == ["2330"]
