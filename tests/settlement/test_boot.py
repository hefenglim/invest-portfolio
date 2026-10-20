"""Boot and the settlement store (plan W0.6): the lifespan creates the store once, and a second
boot moves nothing — no ``schema_version`` change, no row change (spec §7's integrity check
depends on an ordinary boot leaving ``schema_version`` alone)."""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pytest_socket import disable_socket, enable_socket

from portfolio_dash.api import alert_inputs
from portfolio_dash.api import app as app_module
from portfolio_dash.api.app import checkpoint_on_stop, create_app
from portfolio_dash.portfolio.settlement_store import SETTLE_TABLES
from portfolio_dash.shared import result_cache
from portfolio_dash.shared.config import get_settings
from portfolio_dash.shared.db import connect_path, session


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


def _boot() -> None:
    with TestClient(create_app()) as client:
        assert client.get("/api/health").status_code == 200


def _state(path: Path) -> tuple[int, list[tuple[str, str]], int]:
    conn = sqlite3.connect(path)
    try:
        version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
        schema = [(str(r[0]), str(r[1])) for r in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name")]
        rows = sum(
            int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]) for t in SETTLE_TABLES
        )
        return version, schema, rows
    finally:
        conn.close()


def test_boot_creates_the_store_and_a_second_boot_changes_nothing(boot_db: Path) -> None:
    _boot()
    first = _state(boot_db)
    names = {name for name, _sql in first[1]}
    assert set(SETTLE_TABLES) <= names
    assert first[2] == 2  # the two singleton rows
    conn = sqlite3.connect(boot_db)
    try:
        assert "results" in {str(r[1]) for r in conn.execute("PRAGMA table_info(job_runs)")}
    finally:
        conn.close()
    _boot()
    assert _state(boot_db) == first


def test_a_bad_flag_stops_the_boot(boot_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every PD_* flag is validated once at boot (shared/flags.py reads them per call): a typo
    fails the start with the variable's name, never as a 500 on each request later."""
    monkeypatch.setenv("PD_STATIC_IMMUTABLE", "yes")
    with pytest.raises(ValueError, match="PD_STATIC_IMMUTABLE"), TestClient(create_app()):
        pass


def test_a_clean_stop_leaves_the_database_in_one_file(boot_db: Path) -> None:
    """Shutdown closes the result cache's read-only version connections and checkpoints, so
    no `-wal` / `-shm` outlives a clean stop (a file-copy restore would replay a stale WAL)."""
    app = create_app()
    with TestClient(app) as client:
        assert client.get("/api/dashboard").status_code == 200
        assert client.get("/api/dashboard").status_code == 200  # a cache hit: the version
    assert not boot_db.with_name(boot_db.name + "-wal").exists()  # connection was open
    assert app.state.stop_checkpoint[0] == 0


def test_a_clean_stop_waits_for_a_running_prewarm_and_still_leaves_one_file(
    boot_db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-warm blocked mid-build — its read snapshot open — when the stop begins
    (review 2026-10-20): the lifespan waits for it (``result_cache.shutdown``), the pre-warm
    reopens no version connection afterwards, and the TRUNCATE checkpoint completes (busy 0)
    with no ``-wal`` / ``-shm`` left. ``reset()`` alone closed the connections under the
    running pre-warm, which reopened one, and the checkpoint met its reader."""
    started, release = threading.Event(), threading.Event()
    alerts_keys: list[result_cache.CacheKey | None] = []
    opened_after_stop: list[str] = []
    real_open = result_cache._open_version_conn

    def spy(path: str, identity: tuple[int, int]) -> object:
        if result_cache._closed:
            opened_after_stop.append(path)
        return real_open(path, identity)

    def prewarm_today() -> None:  # alert_inputs.prewarm_today's shape: its own session
        now = datetime.now(UTC)
        with session() as conn:
            def build() -> int:
                n = int(conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0])
                started.set()
                assert release.wait(30), "never released"
                return n

            result_cache.cached(conn, name="dashboard", now=now, compute=build, copy=int)
            alerts_keys.append(result_cache.key_for(conn, name="alerts", now=now))

    def release_once_the_stop_began() -> None:
        deadline = time.monotonic() + 30
        while not result_cache._closed and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.3)  # the stop is now waiting on the pre-warm
        release.set()

    monkeypatch.setenv("PD_PREWARM", "1")
    monkeypatch.setattr(app_module, "WRITE_PREWARM_DELAY_S", 0.0)
    monkeypatch.setattr(alert_inputs, "prewarm_today", prewarm_today)
    monkeypatch.setattr(result_cache, "_open_version_conn", spy)
    app = create_app()
    releaser = threading.Thread(target=release_once_the_stop_began, daemon=True)
    with TestClient(app) as client:
        # any non-read request closes a write window, which schedules the pre-warm
        assert client.post("/api/stop-probe-no-such-route").status_code in (404, 405)
        assert started.wait(10)
        # a commit after the pre-warm's snapshot began: the WAL now holds frames that the
        # snapshot predates, which a TRUNCATE checkpoint cannot fold back under it
        other = sqlite3.connect(boot_db)
        try:
            other.execute("CREATE TABLE stop_probe (x INTEGER)")
            other.execute("INSERT INTO stop_probe VALUES (1)")
            other.commit()
        finally:
            other.close()
        releaser.start()
    releaser.join(30)
    assert alerts_keys == [None]          # the pre-warm finished after the stop began ...
    assert opened_after_stop == []        # ... and opened no version connection
    assert app.state.stop_checkpoint[0] == 0
    assert [s for s in ("-wal", "-shm")
            if boot_db.with_name(boot_db.name + s).exists()] == []


def test_a_checkpoint_a_reader_blocks_is_logged_and_the_stop_goes_on(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """What a stop does when a reader outlived ``result_cache.shutdown``'s wait (or a child
    still holds the file): the checkpoint answers busy within the busy timeout, never hangs,
    and the lifespan logs that ``-wal`` / ``-shm`` may remain."""
    path = tmp_path / "held.db"
    writer = connect_path(path)
    writer.execute("CREATE TABLE t (x INTEGER)")
    writer.execute("INSERT INTO t VALUES (1)")
    writer.commit()
    reader = connect_path(path)
    reader.execute("BEGIN")
    reader.execute("SELECT x FROM t").fetchone()        # a read snapshot holds the WAL
    stopper = sqlite3.connect(path, timeout=0.2)
    try:
        started = time.monotonic()
        busy, _log, _done = checkpoint_on_stop(stopper)
        assert time.monotonic() - started < 5
    finally:
        stopper.close()
        reader.rollback()
        reader.close()
        writer.close()
    assert busy == 1
    assert any("checkpoint could not complete" in r.getMessage() and "-wal" in r.getMessage()
               for r in caplog.records if r.levelname == "WARNING")


#: Tables the app does NOT create in its own database at boot, each for a stated reason.
_NOT_AT_BOOT = {
    "portfolio_snapshots": "retired at W4: an existing database keeps it; no app path creates it",
    "news_mentions": "lives in the news library's own file (news/store.py)",
    "organized_news": "lives in the news library's own file (news/store.py)",
}


def test_every_table_the_package_creates_exists_after_boot(boot_db: Path) -> None:
    """A table created by its first WRITE moves ``PRAGMA schema_version`` after boot recorded
    it, and the settlement store is unusable until the next run's claim — every dashboard and
    月度成績 then pays a full replay (final senior review, 2026-10-20: ``fee_rule_overrides``
    on the first fee-rule save). So every ``CREATE TABLE`` in the package runs at boot."""
    import re

    package = Path(__file__).resolve().parents[2] / "portfolio_dash"
    names: set[str] = set()
    for source in package.rglob("*.py"):
        names |= set(re.findall(r"CREATE TABLE IF NOT EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)",
                                source.read_text(encoding="utf-8")))
    _boot()
    conn = sqlite3.connect(boot_db)
    try:
        present = {str(r[0]) for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()
    missing = sorted(names - present - set(_NOT_AT_BOOT))
    assert missing == [], f"created only by a later write: {missing}"
    assert set(_NOT_AT_BOOT) <= names, "a listed exception no longer exists in the package"

