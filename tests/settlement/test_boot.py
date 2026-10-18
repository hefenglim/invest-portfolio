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
