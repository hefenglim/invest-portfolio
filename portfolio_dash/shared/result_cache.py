"""Keep the latest computed result per name until the database changes (PERF, spec §12.1).

The generalisation of :mod:`portfolio_dash.shared.flight`: ``flight`` shares a computation
while it RUNS; this module also KEEPS its result, so the next identical caller is answered
with a copy instead of a fresh build. In-flight sharing still goes through
:func:`flight.coalesce`, so the two layers never disagree about what "identical" means.

**The key** — :class:`CacheKey` ``(name, db_path, write_generation, data_version, day,
reporting, extra)``:

* ``write_generation`` is :func:`flight.write_generation`: it moves on every write window
  (``api/app.py``'s mutating requests, every ``shared.db.session`` that changed a row);
* ``data_version`` is ``PRAGMA data_version`` read on ONE dedicated, never-writing connection
  per database file, held by this module. SQLite changes that value whenever ANY OTHER
  connection commits — another request, a job or settlement child process, a script, the
  stress-audit harness writing SQLite directly — and on a TRUNCATE checkpoint; never on a
  rollback (measured, W0.1). A file signature ``(mtime, size)`` cannot see two commits inside
  one timer tick, nor a commit after a checkpoint reused the ``-wal`` at the same size;
* ``day`` is ``now.date()``: an entry never outlives the day it was computed for. A field
  that depends on ``now`` beyond its date is set per request by the caller's ``restamp``.

**The cache serves the web process's OWN database only** — the file ``get_settings().db_path``
names. A connection to any other file (a script's read-only snapshot, the perf harness's
scratch copy, an ad-hoc database, a test corpus) computes directly, uncached, and opens no
version connection: this module never holds a file open that its owner may want to delete
(Windows refuses to delete an open file) or must leave byte-identical. The comparison is on
resolved paths, falling back to ``os.path.samefile``.

**What the dedicated connection obeys** (all measured in W0.1): it is opened READ-ONLY by
:func:`shared.db.connect_path` (``read_only=True``: a ``mode=ro`` URI and no pragma — it can
neither write nor convert the file; ``data_version`` moves on a read-only connection exactly
as on a writable one, measured 2026-10-19), kept in autocommit (``isolation_level=None``)
because ``data_version`` FREEZES while that connection holds a read transaction, executes
nothing but ``PRAGMA data_version``, and is used under a lock.
The file is the caller's own (``PRAGMA database_list``). The value is paired with the
connection's epoch — a counter that moves whenever a version connection is (re)opened — so a
reopened connection, whose counter restarts, can never reproduce an older key. The file's
identity ``(st_dev, st_ino)`` is checked on every read: a file replaced under the same name
(a restore by a script) reopens the connection instead of watching the old inode forever.

**Ordering — the version is read BEFORE the computation's read snapshot opens.** A commit
between the two can only make an entry NEWER than its key (the next caller reads the moved
version and misses); read the other way round, an entry built from the old data would sit
under the new version and be served after the commit. A commit DURING the computation makes
the entry unreachable for the same reason: the key is the one taken at compute START.

**No caching** (the key is ``None`` and the computation runs alone, exactly as before) while
a write window is open anywhere in the process, when the caller's connection is in a
transaction (its own uncommitted changes must be in its answer), for a database with no
file (``:memory:``, a temp database), or for a file that is not the application's database.

**Entries:** the latest one per ``name``. Every reader receives its own deep copy (``copy``),
made from a stored value nobody mutates; then the caller's ``restamp`` sets the sub-day
fields of THAT copy (the dashboard's ``as_of = now``).

**Read snapshot (PERF-S12):** on a miss, :func:`cached` runs the computation inside ONE
explicit read transaction opened after the key is taken (``snapshot=True``), so a build
reads one consistent state instead of a series of autocommit SELECTs. A caller whose
computation itself reads another cached result passes ``snapshot=False`` and opens
:func:`read_snapshot` around its own reads, AFTER the inner cached read: a key taken inside a
transaction is ``None``, so nesting the inner read inside the outer snapshot would always
compute it afresh.

**Pre-warm:** :func:`schedule_prewarm` runs a computation on a background thread after a
quiet delay (``PD_PREWARM=1``), debounced, so the next page load is a hit.

Imports nothing above ``shared/``.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from collections.abc import Callable, Hashable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import NamedTuple, cast

from portfolio_dash.shared import flags, flight
from portfolio_dash.shared.config import get_settings
from portfolio_dash.shared.db import connect_path

logger = logging.getLogger(__name__)


class CacheKey(NamedTuple):
    """What makes two requests identical (the module docstring explains each part)."""

    name: str
    db_path: str
    write_generation: int
    data_version: tuple[int, int]  # (version-connection epoch, PRAGMA data_version)
    day: date
    reporting: Hashable
    extra: Hashable


@dataclass
class _VersionConn:
    conn: sqlite3.Connection
    epoch: int
    identity: tuple[int, int]


@dataclass
class _Entry:
    key: CacheKey
    value: object


@dataclass
class Stats:
    """Per-name counters since the last :func:`reset` (W4 / W5 measure the hit rate)."""

    hits: int = 0
    misses: int = 0
    uncached: int = 0


#: Version connections held at once; the least recently used is closed beyond it (its file
#: reopens under a new epoch, so its old entries simply miss). A process serves one or two
#: database files; the bound only matters to a test session that opens hundreds.
MAX_VERSION_CONNECTIONS = 4

_lock = threading.Lock()
_versions: dict[str, _VersionConn] = {}
_epoch = 0
_entries: dict[str, _Entry] = {}
_stats: dict[str, Stats] = {}
_prewarm_timer: threading.Timer | None = None

_MISS = object()


# --- the key -------------------------------------------------------------------------------


def db_file(conn: sqlite3.Connection) -> str | None:
    """The main database file of *conn*, or ``None`` for a database with no file."""
    row = conn.execute("PRAGMA database_list").fetchone()
    path = str(row[2]) if row is not None and row[2] else ""
    return path or None


def is_app_database(path: str) -> bool:
    """``True`` when *path* is the application's own database (``get_settings().db_path``) —
    the only file this cache keeps results for and holds a version connection on."""
    configured = get_settings().db_path
    if os.path.normcase(os.path.abspath(path)) == os.path.normcase(
            os.path.abspath(configured)):
        return True
    try:
        return os.path.samefile(path, configured)
    except OSError:
        return False


def _identity(path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _open_version_conn(path: str, identity: tuple[int, int]) -> _VersionConn:
    global _epoch
    # Read-only and pragma-free: the version connection never writes and never converts the
    # file (``journal_mode = WAL`` on a rollback-journal file is a write).
    conn = connect_path(Path(path), read_only=True)
    conn.isolation_level = None  # autocommit: data_version freezes inside a read transaction
    _epoch += 1
    return _VersionConn(conn=conn, epoch=_epoch, identity=identity)


def data_version(path: str) -> tuple[int, int] | None:
    """``(epoch, PRAGMA data_version)`` of the file at *path*, read on the dedicated
    connection; ``None`` when the file cannot be read (no caching)."""
    identity = _identity(path)
    if identity is None:
        return None
    with _lock:
        try:
            held = _versions.pop(path, None)  # re-inserted below: most recently used last
            if held is not None and held.identity != identity:
                held.conn.close()
                held = None
            if held is None:
                held = _open_version_conn(path, identity)
                while len(_versions) >= MAX_VERSION_CONNECTIONS:
                    _versions.pop(next(iter(_versions))).conn.close()
            _versions[path] = held
            row = held.conn.execute("PRAGMA data_version").fetchone()
        except sqlite3.Error:
            logger.warning("result cache: cannot read data_version of %s", path,
                           exc_info=True)
            stale = _versions.pop(path, None)
            if stale is not None:
                stale.conn.close()
            return None
        return (held.epoch, int(row[0]))


def key_for(
    conn: sqlite3.Connection, *, name: str, now: datetime,
    reporting: Hashable = None, extra: Hashable = (),
) -> CacheKey | None:
    """The cache key of this request, or ``None`` when it must compute alone.

    *extra* must determine the result together with the rest of the key; pass a ``Decimal``
    as its ``str`` (equal Decimals with different exponents print differently). A file that
    is not the application's database (:func:`is_app_database`) is never cached.
    """
    if conn.in_transaction:
        return None
    generation = flight.write_generation()
    if generation is None:
        return None
    path = db_file(conn)
    if path is None or not is_app_database(path):
        return None
    version = data_version(path)
    if version is None:
        return None
    return CacheKey(name, path, generation, version, now.date(), reporting, extra)


# --- the read snapshot ---------------------------------------------------------------------


@contextmanager
def read_snapshot(conn: sqlite3.Connection) -> Iterator[None]:
    """Run the block inside ONE explicit read transaction on *conn* (PERF-S12).

    A connection already in a transaction keeps it (its reads are already one snapshot). The
    transaction ends with ``COMMIT`` on success and ``ROLLBACK`` on an error — the same
    outcome ``shared.db.session`` gives a block that wrote nothing.
    """
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN")
    try:
        yield
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    if conn.in_transaction:
        conn.execute("COMMIT")


# --- entries -------------------------------------------------------------------------------


def _count(name: str, field: str) -> None:
    stats = _stats.setdefault(name, Stats())
    setattr(stats, field, getattr(stats, field) + 1)


def note_uncached(name: str) -> None:
    """Count one answer of *name* that a caller computed for itself, outside the cache (e.g.
    alerts evaluated from a build of another state), so :func:`stats` stays complete."""
    with _lock:
        _count(name, "uncached")


def _lookup(key: CacheKey) -> object:
    with _lock:
        entry = _entries.get(key.name)
        if entry is None or entry.key != key:
            return _MISS
        return entry.value


def _store(key: CacheKey, value: object) -> None:
    with _lock:
        _entries[key.name] = _Entry(key, value)


def cached[T](
    conn: sqlite3.Connection,
    *,
    name: str,
    now: datetime,
    compute: Callable[[], T],
    copy: Callable[[T], T],
    reporting: Hashable = None,
    extra: Hashable = (),
    restamp: Callable[[T], T] | None = None,
    snapshot: bool = True,
) -> T:
    """``compute()``'s result for this request — from the cache, from an identical
    computation already running, or computed now (and kept as the latest entry of *name*).

    * *copy* gives every reader its own object (``model_copy(deep=True)``,
      ``copy.deepcopy``); the stored value is a separate copy nobody mutates.
    * *restamp* sets the fields that depend on *now* beyond its date on the reader's copy
      (e.g. ``as_of``), and returns it. It runs on every answer — a hit, a joiner's copy
      and the leader's own result alike.
    * *snapshot* runs a miss inside :func:`read_snapshot` (see the module docstring for when
      to pass ``False``).

    An uncacheable request (:func:`key_for` is ``None``) runs ``compute()`` directly, as the
    code did before the cache — no snapshot, no sharing. :func:`cached_keyed` also returns
    the key the answer was served under.
    """
    value, _key = cached_keyed(
        conn, name=name, now=now, compute=compute, copy=copy, reporting=reporting,
        extra=extra, restamp=restamp, snapshot=snapshot)
    return value


def cached_keyed[T](
    conn: sqlite3.Connection,
    *,
    name: str,
    now: datetime,
    compute: Callable[[], T],
    copy: Callable[[T], T],
    reporting: Hashable = None,
    extra: Hashable = (),
    restamp: Callable[[T], T] | None = None,
    snapshot: bool = True,
) -> tuple[T, CacheKey | None]:
    """:func:`cached`, plus the :class:`CacheKey` the answer was served under (``None`` when
    it was computed uncached).

    The key tells a caller WHICH state the answer belongs to: the value was computed from a
    snapshot taken after that key's version was read, so it is never older than the key. A
    second cached result derived from this one may be filed under its own key only when the
    two keys agree on the state (:func:`same_state`) — otherwise it would sit under a
    version newer than the data it was computed from.
    """
    key = key_for(conn, name=name, now=now, reporting=reporting, extra=extra)
    return serve(conn, key, name=name, compute=compute, copy=copy, restamp=restamp,
                 snapshot=snapshot), key


def serve[T](
    conn: sqlite3.Connection,
    key: CacheKey | None,
    *,
    name: str,
    compute: Callable[[], T],
    copy: Callable[[T], T],
    restamp: Callable[[T], T] | None = None,
    snapshot: bool = True,
) -> T:
    """Serve *compute*'s result under a key the caller already took (:func:`key_for`) —
    for a caller that must inspect the key BEFORE deciding what to compute. A key taken
    earlier is the conservative choice: a commit since then only makes the entry
    unreachable. ``None`` computes directly, uncached."""
    if key is None:
        with _lock:
            _count(name, "uncached")
        return compute()
    hit = _lookup(key)
    if hit is not _MISS:
        with _lock:
            _count(name, "hits")
        return _finish(copy(cast(T, hit)), restamp)

    def lead() -> T:
        again = _lookup(key)
        if again is not _MISS:  # stored by a leader that finished after our lookup
            with _lock:
                _count(name, "hits")
            return copy(cast(T, again))
        with _lock:
            _count(name, "misses")
        if snapshot:
            with read_snapshot(conn):
                value = compute()
        else:
            value = compute()
        _store(key, copy(value))
        return value

    return _finish(flight.coalesce(key, lead, copy=copy), restamp)


def same_state(a: CacheKey | None, b: CacheKey | None) -> bool:
    """``True`` when two keys describe the same database state: the same file, write
    generation, ``data_version``, day and reporting currency (their names and extras may
    differ). ``None`` — an uncached answer — matches nothing."""
    if a is None or b is None:
        return False
    return (a.db_path, a.write_generation, a.data_version, a.day, a.reporting) == (
        b.db_path, b.write_generation, b.data_version, b.day, b.reporting)


def _finish[T](value: T, restamp: Callable[[T], T] | None) -> T:
    return value if restamp is None else restamp(value)


def invalidate(name: str | None = None) -> None:
    """Drop the entry of *name* (every entry for ``None``). Never needed for correctness —
    the key moves on every commit — but frees memory and is explicit after a settlement
    swap."""
    with _lock:
        if name is None:
            _entries.clear()
        else:
            _entries.pop(name, None)


def stats() -> dict[str, Stats]:
    """A snapshot of the per-name counters."""
    with _lock:
        return {k: Stats(v.hits, v.misses, v.uncached) for k, v in _stats.items()}


def reset() -> None:
    """Forget everything: entries, counters, a pending pre-warm, and close every version
    connection (tests; a process that is about to replace or delete a database file)."""
    global _prewarm_timer
    with _lock:
        _entries.clear()
        _stats.clear()
        for held in _versions.values():
            try:
                held.conn.close()
            except sqlite3.Error:  # pragma: no cover - closing an idle connection
                pass
        _versions.clear()
        if _prewarm_timer is not None:
            _prewarm_timer.cancel()
            _prewarm_timer = None


# --- pre-warm ------------------------------------------------------------------------------


def schedule_prewarm(fn: Callable[[], object], delay: float) -> bool:
    """Run ``fn()`` on a daemon thread *delay* seconds from now, debounced: a later call
    replaces a pending one, so a burst of writes pre-warms once, after the last.

    Off unless ``PD_PREWARM=1`` (returns ``False``; the unit tests' default). At fire time a
    write window still open skips the run — its end schedules the next one. A failing
    ``fn`` is logged, never raised: a pre-warm only saves the next reader a build.
    """
    global _prewarm_timer
    if not flags.prewarm_enabled():
        return False
    timer = threading.Timer(delay, _run_prewarm, args=(fn,))
    timer.daemon = True
    timer.name = "result-cache-prewarm"
    with _lock:
        if _prewarm_timer is not None:
            _prewarm_timer.cancel()
        _prewarm_timer = timer
    timer.start()
    return True


def _run_prewarm(fn: Callable[[], object]) -> None:
    global _prewarm_timer
    with _lock:
        if _prewarm_timer is threading.current_thread():
            _prewarm_timer = None
    if flight.write_generation() is None:
        return
    try:
        fn()
    except Exception:  # noqa: BLE001 - see schedule_prewarm
        logger.warning("result cache: pre-warm failed", exc_info=True)


__all__ = [
    "CacheKey",
    "Stats",
    "cached",
    "cached_keyed",
    "data_version",
    "db_file",
    "invalidate",
    "is_app_database",
    "key_for",
    "read_snapshot",
    "reset",
    "same_state",
    "schedule_prewarm",
    "serve",
    "stats",
]
