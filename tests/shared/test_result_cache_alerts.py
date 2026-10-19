"""The ``"alerts"`` entry is filed only with the dashboard state it was computed from.

PERF spec 2026-10-18 §12.1; wave-1 review 2026-10-19. ``compute_alerts_full`` takes its key
when it is called — AFTER the dashboard route built ``data``. A commit landing between the
two (a quote refresh on another connection) used to file alerts computed from the PRE-commit
dashboard under the POST-commit key: the next ``GET /api/alerts`` and the scheduler's
``alert_scan`` served 「AAPL 報價過期」 while every freshness block said nothing was stale.
Now the caller passes the key ``data`` was served under, and the build feeds the cached path
only when both keys describe one state (``result_cache.same_state``).

Also pinned: ``calib_gap`` is part of the key (a different gap is never served the old
alerts), and the alert inputs are read inside ONE read transaction (PERF-S12), traced.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from portfolio_dash.api import alert_inputs
from portfolio_dash.portfolio.dashboard import build_dashboard, build_dashboard_keyed
from portfolio_dash.portfolio.dashboard_models import DashboardData
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared import result_cache
from portfolio_dash.shared.config import get_settings
from portfolio_dash.shared.db import connect_path
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.strategy.alerts import Alert
from tests.conftest import GOLDEN_NOW, _seed_golden, init_golden_base

#: Ten days after golden's clock: its stored quotes (2026-06-09) are stale (> 4 days).
NOW = GOLDEN_NOW + timedelta(days=10)
TWD = Currency.TWD


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    result_cache.reset()
    yield
    result_cache.reset()


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Golden in a file that is the application's database (the only file cached)."""
    path = tmp_path / "ledger.db"
    conn = connect_path(path)
    init_golden_base(conn)
    _seed_golden(conn)
    conn.commit()
    conn.close()
    monkeypatch.setenv("DB_PATH", str(path))
    get_settings.cache_clear()
    try:
        yield path
    finally:
        get_settings.cache_clear()


def _refresh_quote(path: Path, symbol: str, market: Market, close: str) -> None:
    """A quote refresh on ANOTHER connection: commits a fresh close dated today."""
    w = connect_path(path)
    try:
        upsert_prices(w, [PriceRow(instrument=symbol, market=market, as_of=NOW.date(),
                                   close=Decimal(close), source="test")], fetched_at=NOW)
        w.commit()
    finally:
        w.close()


def _stale_alerts(alerts: list[Alert]) -> set[str | None]:
    return {a.subject for a in alerts if a.rule == "stale_price"}


def _stale_in(data: DashboardData) -> set[str | None]:
    return {p.symbol for p in data.freshness.prices if p.stale}


def _truth(path: Path, calib_gap: Decimal | None = None) -> list[Alert]:
    """The alerts of the database as it is now, computed with nothing cached: a connection
    inside its own transaction is never served from the cache (its key is ``None``)."""
    conn = connect_path(path)
    try:
        conn.execute("BEGIN")
        return alert_inputs.compute_alerts_full(conn, now=NOW, reporting=TWD,
                                                calib_gap=calib_gap)
    finally:
        conn.rollback()
        conn.close()


def _ids(alerts: list[Alert]) -> list[str]:
    return [a.id for a in alerts]


def test_a_commit_between_the_build_and_the_alerts_never_files_a_mixed_entry(
    db: Path,
) -> None:
    route = connect_path(db)
    try:
        data, data_key = build_dashboard_keyed(route, now=NOW, reporting=TWD)
        assert data_key is not None
        assert _stale_in(data) == {"2330", "AAPL"}
        _refresh_quote(db, "2330", Market.TW, "610")        # lands mid-request
        embedded = alert_inputs.compute_alerts_full(
            route, now=NOW, reporting=TWD, data=data, data_key=data_key)
    finally:
        route.close()
    # This response is self-consistent: its alerts are its own dashboard's ...
    assert _stale_alerts(embedded) == _stale_in(data) == {"2330", "AAPL"}
    # ... and nothing was filed under the post-commit key.
    assert "alerts" not in result_cache._entries
    # The next GET /api/alerts (or alert_scan) reads the database as it now is.
    api = connect_path(db)
    try:
        served = alert_inputs.compute_alerts_full(api, now=NOW, reporting=TWD)
        dash_now = build_dashboard(api, now=NOW, reporting=TWD)
    finally:
        api.close()
    assert _stale_alerts(served) == _stale_in(dash_now) == {"AAPL"}
    assert _ids(served) == _ids(_truth(db))


def test_the_next_dashboard_request_agrees_with_its_own_embedded_alerts(db: Path) -> None:
    first = connect_path(db)
    try:
        d0, k0 = build_dashboard_keyed(first, now=NOW, reporting=TWD)
        _refresh_quote(db, "AAPL", Market.US, "125")
        alert_inputs.compute_alerts_full(first, now=NOW, reporting=TWD, data=d0, data_key=k0)
    finally:
        first.close()
    _refresh_quote(db, "2330", Market.TW, "610")
    second = connect_path(db)
    try:
        d1, k1 = build_dashboard_keyed(second, now=NOW, reporting=TWD)
        emb = alert_inputs.compute_alerts_full(second, now=NOW, reporting=TWD, data=d1,
                                               data_key=k1)
    finally:
        second.close()
    assert _stale_in(d1) == set()
    assert _stale_alerts(emb) == set()


def test_a_build_of_the_same_state_feeds_the_entry_and_the_next_read_hits(
    db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    builds = [0]
    real = alert_inputs.assemble

    def counted(*args: object, **kwargs: object) -> alert_inputs.AlertInputs:
        builds[0] += 1
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(alert_inputs, "assemble", counted)
    conn = connect_path(db)
    try:
        data, data_key = build_dashboard_keyed(conn, now=NOW, reporting=TWD)
        embedded = alert_inputs.compute_alerts_full(conn, now=NOW, reporting=TWD, data=data,
                                                    data_key=data_key)
        assert result_cache.stats()["alerts"].misses == 1
        again = alert_inputs.compute_alerts_full(conn, now=NOW, reporting=TWD)
    finally:
        conn.close()
    assert builds[0] == 1, "the same state assembled its inputs twice"
    assert _ids(again) == _ids(embedded) == _ids(_truth(db))


def test_a_build_served_uncached_never_feeds_the_entry(db: Path) -> None:
    conn = connect_path(db)
    try:
        data = build_dashboard(conn, now=NOW, reporting=TWD)
        alert_inputs.compute_alerts_full(conn, now=NOW, reporting=TWD, data=data,
                                         data_key=None)
    finally:
        conn.close()
    assert "alerts" not in result_cache._entries


def test_another_calib_gap_is_never_served_the_old_alerts(db: Path) -> None:
    conn = connect_path(db)
    try:
        none_gap = alert_inputs.compute_alerts_full(conn, now=NOW, reporting=TWD)
        wide = alert_inputs.compute_alerts_full(conn, now=NOW, reporting=TWD,
                                                calib_gap=Decimal("99"))
        assert result_cache.stats()["alerts"].misses == 2, "the second gap was a hit"
    finally:
        conn.close()
    assert _ids(wide) == _ids(_truth(db, Decimal("99")))
    assert _ids(none_gap) == _ids(_truth(db))
    assert "calib_gap" in _ids(wide) and "calib_gap" not in _ids(none_gap)


def test_the_alert_inputs_are_read_inside_one_read_transaction(db: Path) -> None:
    """PERF-S12: every statement the alerts issue themselves — the 400-day price reads, the
    consensus snapshots, the rules, the quota — runs between ONE ``BEGIN`` and its
    ``COMMIT`` (the dashboard came from the cache before that transaction opened)."""
    conn = connect_path(db)
    try:
        data, data_key = build_dashboard_keyed(conn, now=NOW, reporting=TWD)
        trace: list[str] = []
        conn.set_trace_callback(trace.append)
        alert_inputs.compute_alerts_full(conn, now=NOW, reporting=TWD, data=data,
                                         data_key=data_key)
        conn.set_trace_callback(None)
    finally:
        conn.close()
    statements = [s.strip() for s in trace]
    begins = [i for i, s in enumerate(statements) if s.upper() == "BEGIN"]
    commits = [i for i, s in enumerate(statements) if s.upper() == "COMMIT"]
    assert len(begins) == 1 and len(commits) == 1, statements[:5]
    inner = statements[begins[0] + 1:commits[0]]
    reads = [s for s in statements if s.upper().startswith("SELECT")]
    assert any("FROM prices" in s for s in inner), "the price history ran outside"
    assert any("llm_usage" in s for s in inner), "the quota ran outside"
    outside = [s for s in reads if s not in inner and "PRAGMA" not in s.upper()]
    assert not outside, f"alert reads outside the read transaction: {outside[:3]}"


def test_the_dashboard_key_is_the_key_its_value_was_served_under(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``build_dashboard_keyed`` returns the key its value was SERVED under, never one
    re-taken afterwards (fold verification N8): with a commit landing right after the serve,
    a re-taken key would match the alerts key, file the pre-commit dashboard's alerts under
    the post-commit state, and the next ``GET /api/alerts`` would serve them."""
    real = result_cache.cached_keyed

    def served_then_commit(*a: object, **kw: object) -> object:
        out: object = real(*a, **kw)  # type: ignore[arg-type]
        _refresh_quote(db, "AAPL", Market.US, "125")
        return out

    monkeypatch.setattr(result_cache, "cached_keyed", served_then_commit)
    route = connect_path(db)
    try:
        data, data_key = build_dashboard_keyed(route, now=NOW, reporting=TWD)
    finally:
        route.close()
    monkeypatch.setattr(result_cache, "cached_keyed", real)
    conn = connect_path(db)
    try:
        alert_inputs.compute_alerts_full(conn, now=NOW, reporting=TWD, calib_gap=None,
                                         data=data, data_key=data_key)
        served = alert_inputs.compute_alerts_full(conn, now=NOW, reporting=TWD, calib_gap=None)
    finally:
        conn.close()
    result_cache.reset()
    fresh = connect_path(db)
    try:
        truth = alert_inputs.compute_alerts_full(fresh, now=NOW, reporting=TWD, calib_gap=None)
    finally:
        fresh.close()
    assert _stale_alerts(served) == _stale_alerts(truth)
    assert "AAPL" not in (_stale_alerts(truth) - {None})


def test_an_alerts_evaluation_for_one_caller_is_counted_uncached(db: Path) -> None:
    """The mismatched-state path (alerts from the caller's own build, never stored) shows in
    ``result_cache.stats()``, so a hit rate measured later is not overstated."""
    conn = connect_path(db)
    try:
        data, data_key = build_dashboard_keyed(conn, now=NOW, reporting=TWD)
        _refresh_quote(db, "AAPL", Market.US, "125")
        before = result_cache.stats().get("alerts", result_cache.Stats()).uncached
        alert_inputs.compute_alerts_full(conn, now=NOW, reporting=TWD, calib_gap=None,
                                         data=data, data_key=data_key)
    finally:
        conn.close()
    assert result_cache.stats()["alerts"].uncached == before + 1
