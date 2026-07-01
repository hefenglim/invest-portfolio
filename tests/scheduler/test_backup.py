"""Tests for spec-19.3 ops 保全: daily SQLite backup + integrity check + snapshots.

Pure ops + the ``backup_daily`` scheduler job. All side effects are confined to a
temp dir (injectable params or ``DB_PATH``); nothing touches the real ``data/``.
No APScheduler, no network.
"""

import gzip
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from portfolio_dash.ops import backup as backup_ops
from portfolio_dash.scheduler.jobs import JOBS, backup_daily, run_job
from portfolio_dash.shared.config import get_settings

_NOW = datetime(2026, 6, 16, 1, 30, tzinfo=UTC)


def _make_db(path: Path) -> None:
    """Create a trivial, healthy sqlite DB with one seeded table."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE widget (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO widget (name) VALUES ('alpha'), ('beta')")
        conn.commit()
    finally:
        conn.close()


def _read_gz_db(gz_path: Path, tmp_path: Path) -> sqlite3.Connection:
    """Decompress a ``.db.gz`` to a temp file and open it as a sqlite DB."""
    raw = tmp_path / "restored.db"
    with gzip.open(gz_path, "rb") as gz:
        raw.write_bytes(gz.read())
    conn = sqlite3.connect(raw)
    conn.row_factory = sqlite3.Row
    return conn


# --- backup_database ----------------------------------------------------------


def test_backup_database_writes_valid_gzipped_sqlite(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    _make_db(db)
    backup_dir = tmp_path / "backups"

    gz = backup_ops.backup_database(db_path=db, backup_dir=backup_dir, now=_NOW)

    assert gz.name == "portfolio_2026-06-16.db.gz"
    assert gz.exists()
    # Valid gzip containing a valid sqlite DB with the seeded table readable back.
    restored = _read_gz_db(gz, tmp_path)
    try:
        names = {r["name"] for r in restored.execute("SELECT name FROM widget")}
    finally:
        restored.close()
    assert names == {"alpha", "beta"}


def test_backup_database_same_day_overwrites(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    _make_db(db)
    backup_dir = tmp_path / "backups"

    first = backup_ops.backup_database(db_path=db, backup_dir=backup_dir, now=_NOW)
    second = backup_ops.backup_database(db_path=db, backup_dir=backup_dir, now=_NOW)

    assert first == second
    # Only one file for the day (overwrite, not duplicate).
    assert len(list(backup_dir.glob("portfolio_*.db.gz"))) == 1


def test_backup_database_default_dir_is_db_parent_backups(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    _make_db(db)

    gz = backup_ops.backup_database(db_path=db, now=_NOW)

    assert gz.parent == db.parent / "backups"
    assert gz.exists()


def test_rotation_keeps_only_newest(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    _make_db(db)
    backup_dir = tmp_path / "backups"
    # Five distinct calendar days; keep=3 must retain the 3 newest.
    days = [datetime(2026, 6, d, 1, 30, tzinfo=UTC) for d in (10, 11, 12, 13, 14)]
    for day in days:
        backup_ops.backup_database(db_path=db, backup_dir=backup_dir, now=day, keep=3)

    remaining = sorted(p.name for p in backup_dir.glob("portfolio_*.db.gz"))
    assert remaining == [
        "portfolio_2026-06-12.db.gz",
        "portfolio_2026-06-13.db.gz",
        "portfolio_2026-06-14.db.gz",
    ]


def test_rotation_default_keep_is_30(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    _make_db(db)
    backup_dir = tmp_path / "backups"
    # Synthesize 35 dated backup files, then one real backup (default keep=30).
    for d in range(1, 36):
        (backup_dir).mkdir(parents=True, exist_ok=True)
        (backup_dir / f"portfolio_2026-05-{d:02d}.db.gz").write_bytes(b"stale")
    backup_ops.backup_database(db_path=db, backup_dir=backup_dir, now=_NOW)

    assert len(list(backup_dir.glob("portfolio_*.db.gz"))) == 30
    # The freshly written (lexically newest) backup survives.
    assert (backup_dir / "portfolio_2026-06-16.db.gz").exists()


# --- check_integrity ----------------------------------------------------------


def test_check_integrity_ok_on_healthy_db(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    _make_db(db)

    ok, detail = backup_ops.check_integrity(db_path=db)

    assert ok is True
    assert detail == "ok"


def test_check_integrity_false_on_corrupt_file(tmp_path: Path) -> None:
    # A file that is not a valid sqlite DB → check fails, never raises out.
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"this is not a sqlite database file at all")

    ok, detail = backup_ops.check_integrity(db_path=corrupt)

    assert ok is False
    assert detail  # carries a reason


# --- pre_write_snapshot -------------------------------------------------------


def test_pre_write_snapshot_writes_prefixed_gz(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    _make_db(db)
    snap_dir = tmp_path / "snapshots"

    gz = backup_ops.pre_write_snapshot(
        prefix="pre_import_", db_path=db, backup_dir=snap_dir, now=_NOW
    )

    assert gz.name == "pre_import_2026-06-16T013000.db.gz"
    assert gz.parent == snap_dir
    restored = _read_gz_db(gz, tmp_path)
    try:
        names = {r["name"] for r in restored.execute("SELECT name FROM widget")}
    finally:
        restored.close()
    assert names == {"alpha", "beta"}


def test_pre_write_snapshot_default_dir_is_db_parent_snapshots(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    _make_db(db)

    gz = backup_ops.pre_write_snapshot(prefix="pre_migrate_", db_path=db, now=_NOW)

    assert gz.parent == db.parent / "snapshots"
    assert gz.exists()


# --- backup_daily scheduler job ----------------------------------------------


def test_backup_daily_registered() -> None:
    spec = next(j for j in JOBS if j.id == "backup_daily")
    assert spec.default_cron == "30 1 * * *"
    assert spec.default_timezone == "Asia/Taipei"
    assert spec.default_enabled is True


@pytest.fixture
def real_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """Point ``DB_PATH`` at a healthy temp DB so backup_daily resolves it from settings."""
    db = tmp_path / "portfolio.db"
    _make_db(db)
    monkeypatch.setenv("DB_PATH", str(db))
    get_settings.cache_clear()
    yield db
    get_settings.cache_clear()


def test_backup_daily_ok_run_writes_backup(
    conn: sqlite3.Connection, real_db: Path
) -> None:
    rid = run_job(conn, "backup_daily", now=_NOW)
    row = conn.execute("SELECT status, detail FROM job_runs WHERE id=?", (rid,)).fetchone()
    assert row["status"] == "ok"
    assert row["detail"].startswith("backup ok -> portfolio_2026-06-16")
    # The gz landed in <db_parent>/backups (default dir).
    assert (real_db.parent / "backups" / "portfolio_2026-06-16.db.gz").exists()


def test_backup_daily_integrity_failure_records_error_run(
    monkeypatch: pytest.MonkeyPatch,
    conn: sqlite3.Connection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    monkeypatch.setattr(
        backup_ops, "check_integrity", lambda *a, **k: (False, "malformed db page 3")
    )
    # On failure the func RAISES → run_job records an error row; backup_database is not called.
    called = {"backup": False}
    def _fake_backup(**k: object) -> Path:
        called["backup"] = True
        return Path("nope")

    monkeypatch.setattr(backup_ops, "backup_database", _fake_backup)
    caplog.set_level(logging.WARNING)

    rid = run_job(conn, "backup_daily", now=_NOW)

    row = conn.execute("SELECT status, detail FROM job_runs WHERE id=?", (rid,)).fetchone()
    assert row["status"] == "error"
    assert "integrity_check failed" in row["detail"]
    assert "malformed db page 3" in row["detail"]
    assert called["backup"] is False
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


def test_backup_daily_warns_after_failure_streak_recovers(
    monkeypatch: pytest.MonkeyPatch,
    conn: sqlite3.Connection,
    real_db: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    # Three consecutive integrity failures → three error runs.
    monkeypatch.setattr(
        backup_ops, "check_integrity", lambda *a, **k: (False, "boom")
    )
    for _ in range(3):
        run_job(conn, "backup_daily", now=_NOW)
    assert backup_daily.__name__  # sanity

    # Recovery run: integrity ok again → success + a best-effort streak-recovery warning.
    monkeypatch.setattr(backup_ops, "check_integrity", lambda *a, **k: (True, "ok"))
    caplog.set_level(logging.WARNING)
    rid = run_job(conn, "backup_daily", now=_NOW)

    row = conn.execute("SELECT status FROM job_runs WHERE id=?", (rid,)).fetchone()
    assert row["status"] == "ok"
    assert any(
        "consecutive failed run" in r.getMessage() for r in caplog.records
    )
