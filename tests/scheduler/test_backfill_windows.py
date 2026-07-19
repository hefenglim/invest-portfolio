"""Unit: smart backfill windows (2026-07-03, R4 item 2, human decision).

Prices: default floor is config-driven (``history_backfill_days``); a symbol whose
position began earlier backfills from its first acquisition date; watch-only symbols
use the default. FX: from the earliest ledger flow date when older than the floor.
The tests pin the floor via a monkeypatched ``get_settings`` so they are independent
of the shipped default (5y since owner 2026-07-08).
"""

import sqlite3
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from portfolio_dash.scheduler import jobs as jobs_mod
from portfolio_dash.scheduler.jobs import (
    backfill_history_all,
    earliest_acquisitions,
    earliest_ledger_flow,
)

_NOW = datetime(2026, 7, 3, tzinfo=UTC)
_DEFAULT_START = date(2025, 7, 3)  # now - 365d (test pins the floor to 365 below)


def _pin_floor(monkeypatch: pytest.MonkeyPatch, days: int) -> None:
    """Pin the config-driven backfill floor so the window math is deterministic."""
    monkeypatch.setattr(jobs_mod, "get_settings",
                        lambda: SimpleNamespace(history_backfill_days=days))


def _inst(conn: sqlite3.Connection, symbol: str, market: str) -> None:
    conn.execute(
        "INSERT INTO instruments (symbol, market, quote_ccy, sector, name, board) "
        "VALUES (?, ?, 'X', NULL, NULL, NULL)", (symbol, market))


def _buy(conn: sqlite3.Connection, symbol: str, d: str) -> None:
    conn.execute(
        "INSERT INTO transactions (account_id, symbol, side, quantity, price, fees, tax, "
        "trade_date) VALUES ('tw_broker', ?, 'BUY', '100', '10', '0', '0', ?)",
        (symbol, d))


def _opening(conn: sqlite3.Connection, symbol: str, d: str) -> None:
    conn.execute(
        "INSERT INTO opening_inventory (account_id, symbol, shares, original_avg_cost, "
        "original_cost_total, build_date) VALUES ('tw_broker', ?, '100', '10', '1000', ?)",
        (symbol, d))


def test_earliest_acquisitions_min_of_buy_and_opening(conn: sqlite3.Connection) -> None:
    _buy(conn, "2330", "2024-05-01")
    _buy(conn, "2330", "2026-01-01")
    _opening(conn, "2330", "2023-11-20")
    _buy(conn, "AAPL", "2026-06-01")
    conn.commit()
    acq = earliest_acquisitions(conn)
    assert acq["2330"] == date(2023, 11, 20)  # opening predates the first buy
    assert acq["AAPL"] == date(2026, 6, 1)


def test_earliest_ledger_flow_spans_all_ledgers(conn: sqlite3.Connection) -> None:
    assert earliest_ledger_flow(conn) is None  # empty ledger
    _buy(conn, "2330", "2025-02-01")
    conn.execute(
        "INSERT INTO fx_conversions (account_id, date, from_ccy, from_amount, to_ccy, "
        "to_amount) VALUES ('schwab', '2023-06-15', 'TWD', '32000', 'USD', '1000')")
    conn.commit()
    assert earliest_ledger_flow(conn) == date(2023, 6, 15)


def test_smart_backfill_windows(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    _pin_floor(monkeypatch, 365)  # pin the floor to 365d for deterministic date math
    _inst(conn, "OLD", "TW")    # position since 2024-01-10 -> backfill from there
    _inst(conn, "NEW", "TW")    # position since 2026-06-01 -> default window
    _inst(conn, "WATCH", "US")  # no position ever -> default window
    _buy(conn, "OLD", "2024-01-10")
    _buy(conn, "NEW", "2026-06-01")
    conn.commit()

    price_calls: list[tuple[list[str], date]] = []
    fx_calls: list[date] = []

    def fake_history(c, registry, instruments, start, *, now):  # type: ignore[no-untyped-def]
        price_calls.append((sorted(i.symbol for i in instruments), start))
        from portfolio_dash.pricing.results import RefreshSummary
        return RefreshSummary(ok={i.symbol: "stub" for i in instruments}, failed=[],
                              fetched_at=now)

    def fake_fx_history(c, registry, pairs, start, *, now):  # type: ignore[no-untyped-def]
        fx_calls.append(start)
        from portfolio_dash.pricing.results import RefreshSummary
        return RefreshSummary(ok={"USDTWD": "stub"}, failed=[], fetched_at=now)

    monkeypatch.setattr(jobs_mod, "default_registry", lambda conn=None: "REG")
    monkeypatch.setattr(jobs_mod, "refresh_history", fake_history)
    monkeypatch.setattr(jobs_mod, "refresh_fx_history", fake_fx_history)

    detail = backfill_history_all(conn, now=_NOW)

    # FU-D46: prices refresh per symbol (the registry routes per-ref anyway; the loop
    # moved into jobs.py so per-symbol progress is honest) — the WINDOW math is unchanged.
    windows = {tuple(syms): start for syms, start in price_calls}
    assert windows[("OLD",)] == date(2024, 1, 10)          # extended to first buy
    assert windows[("NEW",)] == _DEFAULT_START             # default 12-month window
    assert windows[("WATCH",)] == _DEFAULT_START           # watch-only -> default window
    # FX from the earliest ledger flow (OLD's first buy, older than 12mo)
    assert fx_calls == [date(2024, 1, 10)]
    assert "fx(from 2024-01-10)" in detail


def test_explicit_days_keeps_uniform_window(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    _inst(conn, "OLD", "TW")
    _buy(conn, "OLD", "2020-01-01")
    conn.commit()
    starts: list[date] = []

    def fake_history(c, registry, instruments, start, *, now):  # type: ignore[no-untyped-def]
        starts.append(start)
        from portfolio_dash.pricing.results import RefreshSummary
        return RefreshSummary(ok={}, failed=[], fetched_at=now)

    monkeypatch.setattr(jobs_mod, "default_registry", lambda conn=None: "REG")
    monkeypatch.setattr(jobs_mod, "refresh_history", fake_history)
    monkeypatch.setattr(jobs_mod, "refresh_fx_history", fake_history)
    backfill_history_all(conn, now=_NOW, days=30)
    assert all(s == date(2026, 6, 3) for s in starts)  # uniform, ignores acquisitions


def test_default_window_reads_history_backfill_days(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    """The default (days=None) floor derives from the config field, not a literal."""
    _pin_floor(monkeypatch, 1825)  # 5y floor (owner 2026-07-08)
    _inst(conn, "WATCH", "US")     # no position -> uses the config floor verbatim
    conn.commit()
    starts: list[date] = []

    def fake_history(c, registry, instruments, start, *, now):  # type: ignore[no-untyped-def]
        starts.append(start)
        from portfolio_dash.pricing.results import RefreshSummary
        return RefreshSummary(ok={}, failed=[], fetched_at=now)

    monkeypatch.setattr(jobs_mod, "default_registry", lambda conn=None: "REG")
    monkeypatch.setattr(jobs_mod, "refresh_history", fake_history)
    monkeypatch.setattr(jobs_mod, "refresh_fx_history", fake_history)
    backfill_history_all(conn, now=_NOW)  # days=None -> smart windows off the config floor
    expected = (_NOW - timedelta(days=1825)).date()  # floor came from config (1825), not 365
    assert starts and all(s == expected for s in starts)
