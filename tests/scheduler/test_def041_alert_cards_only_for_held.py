"""DEF-041 (functional test G-09 / OBS-2, owner ruling 2026-09-24): 持倉提點 is for HOLDINGS.

Measured on the demo (R1/R2): 系統設定 › 排程中心 › alert_scan › 立即執行 → the alert list held
``drawdown_from_peak`` for 1234 / 1235 / 2308 / 2323 / 2331 / 2608 / SPCX — every one a
watchlist-only symbol — and each became a 「持倉提點」 card: an LLM charge, and a card titled
as advice on a position the owner does not have.

Ruling: the alert-triggered card is dispatched only for a symbol currently HELD; a
watchlist symbol's alert stays in the alert list (and the bell, and the push) but produces
no card. "Held" is the COMPUTED book's answer — a position with shares != 0 in any account —
not a second SQL re-derivation. The filter sits at the single dispatch seam
(``alerts_bridge.dispatch_alert_events_ex``), bound by the scheduler from the app's
registration (``insight_service.held_symbols_for_alerts``), and the run detail says what it
withheld, the way R2 made the non-symbol-scope skips visible.
"""

import inspect
import sqlite3
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.llm_insight import alerts_bridge as ab
from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.scheduler import jobs
from portfolio_dash.strategy.alerts import Alert

NOW = datetime(2026, 9, 24, 15, 0, tzinfo=ZoneInfo("Asia/Taipei"))


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    jobs.create_scheduler_tables(c)
    cs.ensure_seeded(c)
    ab.ensure_tables(c)
    cs.create_insight_type(c, name="持倉提點", scope="on_alert", alert_rules="all",
                           enabled=True, now=NOW)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _clear() -> Iterator[None]:
    jobs.register_insight_runner(None)
    jobs.register_alert_held_fn(None)
    yield
    jobs.register_insight_runner(None)
    jobs.register_alert_held_fn(None)


class _Recorder:
    def __init__(self) -> None:
        self.symbols: list[str | None] = []

    def __call__(self, c: sqlite3.Connection, insight_type_id: int, *, now: datetime,
                 fired_rule: str, fired_symbol: str | None, trigger: Any) -> None:
        self.symbols.append(fired_symbol)


def _dd(symbol: str) -> Alert:
    return Alert(id=f"drawdown_from_peak:{symbol}", sev="risk", rule="drawdown_from_peak",
                 title=f"{symbol} 自高點回撤", detail="回撤 22.0%＞門檻 20.0%",
                 href=f"/symbol/{symbol}", scope="symbol", subject=symbol)


_PORTFOLIO = Alert(id="portfolio_drawdown", sev="risk", rule="portfolio_drawdown",
                   title="組合自高點回撤", detail="回撤 12%", scope="portfolio")


def _scan(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
          alerts: list[Alert]) -> tuple[_Recorder, str]:
    monkeypatch.setattr(jobs, "_compute_alerts_for_scan", lambda c, *, now: alerts)
    rec = _Recorder()
    jobs.register_insight_runner(rec)
    return rec, jobs.alert_scan(conn, now=NOW).detail  # DEF-067: a JobOutcome


def test_a_watchlist_symbol_alert_produces_no_card_and_the_run_says_so(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads: list[int] = []

    def held(c: sqlite3.Connection, *, now: datetime) -> set[str]:
        reads.append(1)
        return {"2884"}

    jobs.register_alert_held_fn(held)
    rec, detail = _scan(conn, monkeypatch,
                        [_dd("1234"), _dd("2884"), _dd("SPCX"), _PORTFOLIO])
    assert rec.symbols == ["2884", None]  # the held symbol + the portfolio card, nothing else
    # DEF-062: the rule by its name, never its id
    assert "略過 2 條觀察標的預警（未持有，不產卡）：高點回撤 1234、高點回撤 SPCX" in detail
    assert "drawdown_from_peak" not in detail
    assert reads == [1]  # the book is read ONCE per pass, not once per event
    # the watchlist alerts are still in the alert list — consumed, never deleted
    rows = conn.execute("SELECT symbol, consumed FROM alert_events ORDER BY id").fetchall()
    assert [(r["symbol"], r["consumed"]) for r in rows] == [
        ("1234", 1), ("2884", 1), ("SPCX", 1), (None, 1),
    ]


def test_the_book_is_not_read_when_no_symbol_alert_needs_it(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads: list[int] = []

    def held(c: sqlite3.Connection, *, now: datetime) -> set[str]:
        reads.append(1)
        return set()

    jobs.register_alert_held_fn(held)
    rec, detail = _scan(conn, monkeypatch, [_PORTFOLIO])
    assert rec.symbols == [None] and reads == []
    assert "觀察標的" not in detail


def test_when_holdings_cannot_be_read_symbol_alerts_wait_for_the_next_scan(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not wired (or failing): never a card for a watchlist symbol, never a held symbol's card
    dropped for good — the symbol events stay unconsumed and the run detail says so."""
    rec, detail = _scan(conn, monkeypatch, [_dd("2884"), _PORTFOLIO])
    assert rec.symbols == [None]
    assert "1 條個股預警暫不派發（無法判定是否持有，下次掃描重試）" in detail
    pending = ab.unconsumed_events(conn)
    assert [e.symbol for e in pending] == ["2884"]
    # the next scan, with the book readable, decides it
    jobs.register_alert_held_fn(lambda c, *, now: {"2884"})
    rec2, _ = _scan(conn, monkeypatch, [])
    assert rec2.symbols == ["2884"] and ab.unconsumed_events(conn) == []


def test_a_failing_book_read_defers_too(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(c: sqlite3.Connection, *, now: datetime) -> set[str]:
        raise RuntimeError("dashboard build failed")

    jobs.register_alert_held_fn(boom)
    rec, detail = _scan(conn, monkeypatch, [_dd("2884")])
    assert rec.symbols == [] and "暫不派發" in detail
    assert len(ab.unconsumed_events(conn)) == 1


def test_the_seam_parameter_is_required() -> None:
    """architecture.md injection obligation (1): a forgotten binding is a TypeError, not a
    silent return to carding the watchlist."""
    for fn in (ab.dispatch_alert_events, ab.dispatch_alert_events_ex):
        p = inspect.signature(fn).parameters["held_symbols"]
        assert p.kind is inspect.Parameter.KEYWORD_ONLY
        assert p.default is inspect.Parameter.empty


# --- "held" is the computed book --------------------------------------------------------


def test_held_is_read_from_the_computed_book(golden_db: sqlite3.Connection) -> None:
    from portfolio_dash.api.insight_service import held_symbols_for_alerts
    from portfolio_dash.portfolio.dashboard import build_dashboard
    from portfolio_dash.shared.enums import Currency

    data = build_dashboard(golden_db, now=NOW, reporting=Currency.TWD)
    book = {h.symbol for h in data.holdings if h.shares != 0}
    assert book, "the golden ledger holds positions"
    assert held_symbols_for_alerts(golden_db, now=NOW) == book
    registered = {r["symbol"] for r in golden_db.execute("SELECT symbol FROM instruments")}
    # the watchlist (registered, not held) is exactly what the dispatcher keeps off the cards
    assert not (registered - book) & held_symbols_for_alerts(golden_db, now=NOW)


# --- the app binds it ---------------------------------------------------------------


@pytest.mark.enable_socket
def test_the_app_registers_the_book_reader_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "boot.db"))
    monkeypatch.setenv("PD_DISABLE_SCHEDULER", "1")
    from portfolio_dash.shared.config import get_settings

    get_settings.cache_clear()
    from portfolio_dash.api import insight_service
    from portfolio_dash.api.app import create_app

    try:
        with TestClient(create_app()):
            assert jobs._ALERT_HELD_FN is insight_service.held_symbols_for_alerts
    finally:
        get_settings.cache_clear()
