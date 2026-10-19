"""The boot sequence of the settlement store (plan W0.6 lifespan order, W1-B "Install and verify"
tests): after a FRESH boot and after an UPGRADE boot (a database created without the settle
tables and without ``job_runs.results``), the store is usable and ``trigger_schema_version``
equals ``PRAGMA schema_version``; a second boot recreates nothing and moves nothing.

``_boot`` runs the REAL ``api/app.py`` lifespan, which since the wave-1 merge does the whole
order on its ``system:boot`` session: the tables and the ``job_runs`` migration, the trigger
set, the boot hook, and ``record_schema_version`` LAST; it returns what the trigger check found
(``app.state.settle_trigger_check``).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pytest_socket import disable_socket, enable_socket

from portfolio_dash.api.app import create_app
from portfolio_dash.portfolio import settlement_store as st
from portfolio_dash.portfolio.settlement_models import ACTOR_BOOT, PeriodKpi, RunRequest
from portfolio_dash.shared.config import get_settings
from portfolio_dash.shared.enums import Currency
from tests.conftest import _seed_golden

TWD = Currency.TWD
THROUGH = date(2026, 6, 10)


@pytest.fixture
def boot_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "boot.db"
    monkeypatch.setenv("DB_PATH", str(path))
    get_settings.cache_clear()
    enable_socket()  # the TestClient portal's event loop uses a socket pair on Windows
    try:
        yield path
    finally:
        disable_socket(allow_unix_socket=True)
        get_settings.cache_clear()


def _boot() -> st.TriggerCheck:
    app = create_app()
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
    check: st.TriggerCheck = app.state.settle_trigger_check
    return check


def _versions(path: Path) -> tuple[int, int | None]:
    conn = sqlite3.connect(path)
    try:
        schema = int(conn.execute("PRAGMA schema_version").fetchone()[0])
        row = conn.execute("SELECT trigger_schema_version FROM settle_meta").fetchone()
        return schema, None if row[0] is None else int(row[0])
    finally:
        conn.close()


def _publish_one(path: Path) -> st.StoreState:
    """A run end to end on the booted file: the store accepts it and serves it."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        run_id = 1 + int(conn.execute("SELECT COALESCE(MAX(run_id), 0) FROM settle_run"
                                      ).fetchone()[0])
        st.create_run(conn, RunRequest(scope="full", reason="boot", through=THROUGH),
                      run_id=run_id, trigger_text="系統啟動時自動執行",
                      started_at=datetime(2026, 6, 11, tzinfo=UTC))
        c = st.claim(conn, run_id, scope="full", run_through=THROUGH)
        assert c.run_from is not None and c.triggers_verified
        new = st.next_generation(conn)
        days = [c.run_from + timedelta(days=i) for i in range(c.days)]
        st.write_days(conn, new, [st.SettledDay(
            day=d, total_value=Decimal("1"), net_invested=Decimal("1"), net_worth=None,
            cash_total=None, cash_incomplete=False, incomplete=False, options_open=False)
            for d in days])
        st.write_periods(conn, new, [st.SettledPeriod(
            kind=k, period=p, end_day=e, kpi=PeriodKpi(reporting_ccy=TWD))
            for k, p, e in st.period_ends(c.run_from, c.run_through)])
        # `claim` clamps the range up to what is already settled — the boot's own run since
        # the wave-2 merge — and `swap` publishes exactly the claimed range.
        st.swap(conn, run_id, calc_version=st.calc_version(TWD), bench_state=None,
                through=c.run_through, first_day=c.run_from, reporting=TWD)
        return st.store_state(conn, reporting=TWD)
    finally:
        conn.close()


def _seed(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        _seed_golden(conn)
    finally:
        conn.close()


def _trail(path: Path) -> list[tuple[str, str | None]]:
    conn = sqlite3.connect(path)
    try:
        return [(str(r[0]), r[1]) for r in conn.execute(
            "SELECT table_name, actor FROM settle_change_log ORDER BY id")]
    finally:
        conn.close()


def test_a_fresh_boot_installs_a_verified_trigger_set_and_a_second_boot_moves_nothing(
        boot_db: Path) -> None:
    first = _boot()
    assert first.first_install and first.recreated and not first.marked_dirty
    schema, recorded = _versions(boot_db)
    assert recorded == schema
    _seed(boot_db)
    assert _publish_one(boot_db).usable
    again = _boot()
    assert not again.recreated
    assert _versions(boot_db) == (schema, schema)
    conn = sqlite3.connect(boot_db)
    try:
        assert st.triggers_verified(conn) and st.verify_triggers(conn)
        assert st.store_state(conn, reporting=TWD).usable
    finally:
        conn.close()


def test_an_upgrade_boot_migrates_installs_and_serves(boot_db: Path) -> None:
    _boot()
    _seed(boot_db)
    conn = sqlite3.connect(boot_db)
    try:  # back to a database an older version left: no store, no job_runs.results
        st.drop_store(conn)
        conn.execute("ALTER TABLE job_runs DROP COLUMN results")
        conn.commit()
    finally:
        conn.close()
    check = _boot()
    assert check.first_install and not check.marked_dirty
    conn = sqlite3.connect(boot_db)
    try:
        assert "results" in {str(r[1]) for r in conn.execute("PRAGMA table_info(job_runs)")}
        assert set(st.SETTLE_TABLES) <= {str(r[0]) for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()
    schema, recorded = _versions(boot_db)
    assert recorded == schema
    assert _publish_one(boot_db).usable


def test_a_boot_that_recreates_a_lost_trigger_marks_the_store_and_signs_it_as_boot(
        boot_db: Path) -> None:
    _boot()
    _seed(boot_db)
    _publish_one(boot_db)
    conn = sqlite3.connect(boot_db)
    try:
        conn.execute(f"DROP TRIGGER {st.TRIGGER_PREFIX}fx_rates_upd")
        conn.commit()
        assert not st.triggers_verified(conn)
    finally:
        conn.close()
    check = _boot()
    assert check.created == (f"{st.TRIGGER_PREFIX}fx_rates_upd",) and check.marked_dirty
    assert _trail(boot_db) == [("fx_rates", ACTOR_BOOT)]
    conn = sqlite3.connect(boot_db)
    try:
        state = st.store_state(conn, reporting=TWD)
        assert state.usable and state.dirty_from == state.first_day
    finally:
        conn.close()
