"""Boot and the settlement store (plan W0.6): the lifespan creates the store once, and a second
boot moves nothing — no ``schema_version`` change, no row change (spec §7's integrity check
depends on an ordinary boot leaving ``schema_version`` alone)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pytest_socket import disable_socket, enable_socket

from portfolio_dash.api.app import create_app
from portfolio_dash.portfolio.settlement_store import SETTLE_TABLES
from portfolio_dash.shared.config import get_settings


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

