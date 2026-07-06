"""SQLite backup, integrity check, and pre-write snapshots (spec 19.3, ops 保全).

A low-level ops utility: it imports ONLY stdlib + ``portfolio_dash.shared`` (for the
configured ``db_path``). It never imports ``portfolio``/``pricing``/``data_ingestion``/
``api``/``scheduler`` (architecture.md — ops is a leaf above shared; higher layers call
in, never the reverse).

Backups use the **sqlite3 online backup API** (``Connection.backup``), which yields a
consistent snapshot even while the source DB is open / mid-write — unlike a raw file
copy. The snapshot is written to a temp on-disk sqlite file, gzipped to the target
``.gz`` path, and the temp file removed. All side effects are confined to the given
(or default) backup/snapshot directories.
"""

import gzip
import logging
import os
import shutil
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from portfolio_dash.shared.config import get_settings

logger = logging.getLogger(__name__)

_BACKUP_GLOB = "portfolio_*.db.gz"
# The news DB backup rides along (L1 fix, decision Q4a 2026-07-07) under its own prefix.
_NEWS_BACKUP_GLOB = "news_*.db.gz"


def _resolve_db_path(db_path: Path | None) -> Path:
    """The source DB path, defaulting to the configured ``db_path`` when None."""
    return db_path if db_path is not None else get_settings().db_path


def _online_backup_to_gz(src_db: Path, dest_gz: Path) -> None:
    """Snapshot ``src_db`` via the sqlite3 online backup API and gzip it to ``dest_gz``.

    A consistent point-in-time copy is taken into a temporary on-disk sqlite file
    (so the source may be open / mid-write), that file is gzipped to ``dest_gz``
    (overwriting any same-named file), then the temp file is removed.
    """
    dest_gz.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(suffix=".db", dir=dest_gz.parent)
    os.close(fd)  # mkstemp returns an open fd we do not use; sqlite3 reopens by path
    tmp_path = Path(tmp_name)
    try:
        src_conn = sqlite3.connect(src_db)
        try:
            dst_conn = sqlite3.connect(tmp_path)
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
        finally:
            src_conn.close()
        with open(tmp_path, "rb") as raw, gzip.open(dest_gz, "wb") as gz:
            shutil.copyfileobj(raw, gz)
    finally:
        tmp_path.unlink(missing_ok=True)


def _rotate(backup_dir: Path, *, keep: int, glob: str = _BACKUP_GLOB) -> None:
    """Keep the newest ``keep`` files matching ``glob`` in ``backup_dir``.

    Files are ordered by name (which is date-prefixed, so lexical == chronological),
    newest last; everything before the trailing ``keep`` is deleted.
    """
    files = sorted(backup_dir.glob(glob))
    if keep < 0:
        keep = 0
    for stale in files[: max(0, len(files) - keep)]:
        stale.unlink(missing_ok=True)


def backup_database(
    *,
    db_path: Path | None = None,
    backup_dir: Path | None = None,
    now: datetime,
    keep: int = 30,
) -> Path:
    """Write a gzipped, consistent daily backup of the SQLite DB; rotate to ``keep`` files.

    ``db_path`` defaults to the configured ``get_settings().db_path``; ``backup_dir``
    defaults to ``db_path.parent / "backups"`` (created if missing). The backup is named
    ``portfolio_{now:%Y-%m-%d}.db.gz`` and OVERWRITES a same-day re-run. After writing,
    only the newest ``keep`` (default 30) ``portfolio_*.db.gz`` files are retained.

    The news DB rides along (L1 fix, decision Q4a): when ``news.db`` exists next to the
    ledger DB it is backed up too, as ``news_{date}.db.gz`` with the same rotation;
    absent → silently skipped. The path is derived locally as ``db_path.parent /
    "news.db"`` — the same convention as ``news.store.news_db_path()`` — because ops/
    stays news-agnostic (it imports only stdlib + ``shared``; see the module docstring).
    Returns the ledger backup's ``.gz`` path.
    """
    src_db = _resolve_db_path(db_path)
    target_dir = backup_dir if backup_dir is not None else src_db.parent / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    dest_gz = target_dir / f"portfolio_{now:%Y-%m-%d}.db.gz"
    _online_backup_to_gz(src_db, dest_gz)
    _rotate(target_dir, keep=keep)
    news_db = src_db.parent / "news.db"  # mirrors news.store.news_db_path() (no import)
    if news_db.exists():
        news_gz = target_dir / f"news_{now:%Y-%m-%d}.db.gz"
        _online_backup_to_gz(news_db, news_gz)
        _rotate(target_dir, keep=keep, glob=_NEWS_BACKUP_GLOB)
        logger.info("news sqlite backup written: %s (keep=%d)", news_gz, keep)
    logger.info("sqlite backup written: %s (keep=%d)", dest_gz, keep)
    return dest_gz


def check_integrity(db_path: Path | None = None) -> tuple[bool, str]:
    """Run ``PRAGMA integrity_check`` on the source DB; return ``(ok, detail)``.

    ``ok`` is True iff the pragma's single-row result is exactly ``"ok"``. On any
    sqlite error (e.g. an unreadable / corrupt file) returns ``(False, <error>)`` so
    callers never crash on a damaged DB.
    """
    src_db = _resolve_db_path(db_path)
    try:
        conn = sqlite3.connect(src_db)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        return False, f"integrity_check raised: {exc}"
    detail = str(row[0]) if row is not None else ""
    return detail == "ok", detail


def pre_write_snapshot(
    *,
    prefix: str,
    db_path: Path | None = None,
    backup_dir: Path | None = None,
    now: datetime,
) -> Path:
    """Take a one-off, prefixed snapshot before a risky write (CSV/AI commit, migration).

    Same online-backup + gzip mechanics as :func:`backup_database`, but written under
    ``backup_dir`` (default ``db_path.parent / "snapshots"``) and named
    ``f"{prefix}{now:%Y-%m-%dT%H%M%S}.db.gz"`` (e.g. ``pre_import_`` / ``pre_migrate_``).
    No rotation — these are intentional, named restore points. Returns the ``.gz`` path.
    """
    src_db = _resolve_db_path(db_path)
    target_dir = backup_dir if backup_dir is not None else src_db.parent / "snapshots"
    target_dir.mkdir(parents=True, exist_ok=True)
    dest_gz = target_dir / f"{prefix}{now:%Y-%m-%dT%H%M%S}.db.gz"
    _online_backup_to_gz(src_db, dest_gz)
    logger.info("pre-write snapshot written: %s", dest_gz)
    return dest_gz


def latest_backup_at(
    backup_dir: Path | None = None, *, db_path: Path | None = None
) -> str | None:
    """Return the newest daily backup's mtime as a UTC ISO-8601 string, or None.

    Scans ``backup_dir`` (default ``db_path.parent / "backups"`` — the same default as
    :func:`backup_database`) for ``portfolio_*.db.gz`` files and returns the modification
    time of the most recent one as a timezone-aware ISO-8601 string in UTC. Returns
    ``None`` when the directory is missing or holds no matching backup (read-only and
    side-effect free — never creates the directory). The router calls this to surface
    backup freshness on the dashboard; ``build_dashboard`` stays pure and never reads it.
    """
    target_dir = (
        backup_dir if backup_dir is not None else _resolve_db_path(db_path).parent / "backups"
    )
    if not target_dir.is_dir():
        return None
    files = sorted(target_dir.glob(_BACKUP_GLOB), key=lambda p: p.stat().st_mtime)
    if not files:
        return None
    newest = files[-1]
    return datetime.fromtimestamp(newest.stat().st_mtime, tz=UTC).isoformat()
