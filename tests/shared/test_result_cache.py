"""``shared/result_cache.py``: the latest result per name, kept until the database changes.

PERF spec 2026-10-18 §12.1. A stale hit would serve an answer computed before a write — the
one thing the cache must never do — so every way a write can reach the file is pinned to a
miss: another connection, another PROCESS, two commits inside one timer tick, a commit after
a checkpoint reused the ``-wal`` at the same size, a write window's opening and closing, the
caller's own open transaction. A commit landing between the version read and the
computation's snapshot may only make an entry NEWER than its key, never older. While another
request's write window is open, readers keep caching and sharing (post-v0.1.32 item 1): the
window's commit is a commit like any other. Stub computations only; the dashboard itself is
exercised in ``test_result_cache_dashboard.py``.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.shared import flight, result_cache
from portfolio_dash.shared.config import get_settings
from portfolio_dash.shared.db import connect_path

_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    result_cache.reset()
    yield
    result_cache.reset()
    assert flight._flights == {}, "a flight outlived its computation"
    assert flight._writes_in_progress == 0


def _make_db(path: Path, *, wal: bool = True) -> Path:
    conn = connect_path(path) if wal else sqlite3.connect(path)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()
    return path


def _point_app_at(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make *path* the application's database: the only file the cache serves."""
    monkeypatch.setenv("DB_PATH", str(path))
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _settings_cleared() -> Iterator[None]:
    yield
    get_settings.cache_clear()


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = _make_db(tmp_path / "cache.db")
    _point_app_at(path, monkeypatch)
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _own(d: dict[str, int]) -> dict[str, int]:
    return dict(d)


class Reader:
    """A caller: its own connection, a stub computation that reads ``t.x`` and counts runs."""

    def __init__(self, path: Path, conn: sqlite3.Connection | None = None) -> None:
        self.conn = conn if conn is not None else connect_path(path)
        self.runs = 0

    def compute(self) -> dict[str, int]:
        self.runs += 1
        return {"x": int(self.conn.execute("SELECT x FROM t").fetchone()[0])}

    def read(self, *, now: datetime = NOW, name: str = "probe",
             restamp: Callable[[dict[str, int]], dict[str, int]] | None = None,
             snapshot: bool = True) -> dict[str, int]:
        return result_cache.cached(
            self.conn, name=name, now=now, compute=self.compute, copy=_own,
            restamp=restamp, snapshot=snapshot)

    def close(self) -> None:
        self.conn.close()


@pytest.fixture
def reader(db: Path) -> Iterator[Reader]:
    r = Reader(db)
    yield r
    r.close()


def _commit(path: Path, sql: str = "UPDATE t SET x = x + 1") -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(sql)
        conn.commit()
    finally:
        conn.close()


# --- hits and copies -----------------------------------------------------------------------


def test_a_repeat_is_a_hit_and_every_reader_owns_its_answer(reader: Reader) -> None:
    first = reader.read()
    second = reader.read()
    assert first == second == {"x": 1}
    assert reader.runs == 1
    assert first is not second
    second["x"] = 99                      # a reader mutating its copy cannot reach the entry
    assert reader.read() == {"x": 1}
    assert result_cache.stats()["probe"].hits == 2


def test_another_connection_on_the_same_file_shares_the_entry(db: Path, reader: Reader) -> None:
    other = Reader(db)
    try:
        assert reader.read() == other.read()
        assert (reader.runs, other.runs) == (1, 0)
    finally:
        other.close()


def test_the_entry_is_the_latest_one_per_name(reader: Reader) -> None:
    reader.read()
    reader.read(now=NOW + timedelta(days=1))      # another day: a miss, replacing the entry
    reader.read()                                 # back to the first day: a miss again
    assert reader.runs == 3
    reader.read(name="other")                     # another name has its own entry
    reader.read()
    assert reader.runs == 4


def test_a_restamp_runs_on_every_answer_with_its_own_clock(reader: Reader) -> None:
    def at(minute: int) -> Callable[[dict[str, int]], dict[str, int]]:
        def stamp(d: dict[str, int]) -> dict[str, int]:
            d["minute"] = minute
            return d
        return stamp

    assert reader.read(now=NOW, restamp=at(30)) == {"x": 1, "minute": 30}
    later = NOW + timedelta(hours=9)              # same Taipei day
    assert reader.read(now=later, restamp=at(59)) == {"x": 1, "minute": 59}
    assert reader.runs == 1
    assert reader.read() == {"x": 1}              # the entry itself was never stamped


def test_an_error_is_raised_and_nothing_is_kept(reader: Reader) -> None:
    def boom() -> dict[str, int]:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        result_cache.cached(reader.conn, name="probe", now=NOW, compute=boom, copy=_own)
    assert reader.read() == {"x": 1}
    assert reader.runs == 1
    assert not reader.conn.in_transaction


# --- every write misses --------------------------------------------------------------------


def test_a_commit_on_another_connection_misses(db: Path, reader: Reader) -> None:
    assert reader.read() == {"x": 1}
    _commit(db)
    assert reader.read() == {"x": 2}
    assert reader.runs == 2


def test_a_commit_on_the_callers_own_connection_misses(reader: Reader) -> None:
    # data_version never moves on a connection's OWN commit — which is why the version is
    # read on a dedicated connection, never on the caller's.
    assert reader.read() == {"x": 1}
    reader.conn.execute("UPDATE t SET x = 5")
    reader.conn.commit()
    assert reader.read() == {"x": 5}


def test_a_commit_from_another_process_misses(db: Path, reader: Reader) -> None:
    assert reader.read() == {"x": 1}
    env = dict(os.environ, PYTHONPATH=str(_ROOT))
    subprocess.run(
        [sys.executable, "-c",
         "import sqlite3, sys\n"
         "c = sqlite3.connect(sys.argv[1])\n"
         "c.execute('UPDATE t SET x = x + 10')\n"
         "c.commit()\n"
         "c.close()\n",
         str(db)],
        check=True, cwd=_ROOT, env=env, timeout=60,
    )
    assert reader.read() == {"x": 11}


def test_two_commits_inside_one_timer_tick_each_miss(db: Path, reader: Reader) -> None:
    # A quote refresh commits prices, FX and the derived MYR/TWD rate milliseconds apart
    # (spec §12.1): a reader between two of them must miss on the second, although the file's
    # mtime may not have moved and the same-size page rewrite leaves its size unchanged.
    writer = sqlite3.connect(db)
    try:
        seen = [reader.read()["x"]]
        for _ in range(3):
            writer.execute("UPDATE t SET x = x + 1")
            writer.commit()
            seen.append(reader.read()["x"])
    finally:
        writer.close()
    assert seen == [1, 2, 3, 4]
    assert reader.runs == 4


def test_a_commit_after_a_checkpoint_reused_the_wal_misses(db: Path, reader: Reader) -> None:
    writer = sqlite3.connect(db)
    try:
        writer.execute("UPDATE t SET x = 2")
        writer.commit()
        assert reader.read() == {"x": 2}
        wal = db.with_name(db.name + "-wal")
        size = wal.stat().st_size
        writer.execute("PRAGMA wal_checkpoint(RESTART)")
        assert reader.read() == {"x": 2}
        writer.execute("UPDATE t SET x = 3")       # rewrites the -wal from its start
        writer.commit()
        assert wal.stat().st_size == size          # a (size) signature could not tell
        assert reader.read() == {"x": 3}
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        writer.execute("UPDATE t SET x = 4")
        writer.commit()
        assert reader.read() == {"x": 4}
    finally:
        writer.close()


def test_a_rollback_keeps_the_entry(db: Path, reader: Reader) -> None:
    reader.read()
    writer = sqlite3.connect(db)
    try:
        writer.execute("UPDATE t SET x = 100")
        writer.rollback()
    finally:
        writer.close()
    assert reader.read() == {"x": 1}
    assert reader.runs == 1


def test_a_file_replaced_under_the_same_name_reopens_the_version_connection(
    db: Path, reader: Reader, monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader.read()
    real = result_cache._identity
    monkeypatch.setattr(result_cache, "_identity",
                        lambda path: None if real(path) is None else (-1, -1))
    reader.read()                               # another inode: another epoch, a miss
    assert reader.runs == 2
    reader.read()
    assert reader.runs == 2


def test_version_connections_are_bounded_and_an_evicted_file_misses(
    tmp_path: Path, reader: Reader,
) -> None:
    reader.read()
    for i in range(result_cache.MAX_VERSION_CONNECTIONS):   # push the reader's file out
        path = tmp_path / f"other-{i}.db"
        sqlite3.connect(path).close()
        assert result_cache.data_version(str(path)) is not None
    assert len(result_cache._versions) == result_cache.MAX_VERSION_CONNECTIONS
    reader.read()                               # reopened under a new epoch: a miss
    assert reader.runs == 2
    reader.read()
    assert reader.runs == 2


# --- when nothing is cached ----------------------------------------------------------------


def test_another_requests_write_window_still_caches_and_its_commit_misses(
    db: Path, reader: Reader,
) -> None:
    """Post-v0.1.32 item 1: a write window open elsewhere in the process no longer makes every
    reader compute alone (before: two runs inside the window, both uncached). The window's
    commit moves ``data_version`` at the commit itself, and its closing moves the generation,
    so nothing read before the commit is served after it, nor anything filed inside the window
    after the window."""
    reader.read()
    flight.begin_write()
    try:
        assert reader.read() == {"x": 1}         # the window's opening moved the key
        assert reader.read() == {"x": 1}         # ... and the window does not stop a hit
        assert reader.runs == 2
        _commit(db)                              # the window's own commit
        assert reader.read() == {"x": 2}
        assert reader.runs == 3
        assert reader.read() == {"x": 2}
        assert reader.runs == 3
    finally:
        flight.end_write()
    assert reader.read() == {"x": 2}             # the window's closing moved the key again
    assert reader.runs == 4
    stats = result_cache.stats()["probe"]
    assert (stats.hits, stats.misses, stats.uncached) == (2, 4, 0)


def test_a_write_window_closing_moves_the_key(reader: Reader) -> None:
    reader.read()
    flight.begin_write()
    flight.end_write()
    reader.read()
    assert reader.runs == 2


def test_a_connection_in_a_transaction_computes_alone(reader: Reader) -> None:
    reader.read()
    reader.conn.execute("UPDATE t SET x = 7")   # its own uncommitted change is its answer
    assert reader.conn.in_transaction
    assert reader.read() == {"x": 7}
    reader.conn.rollback()
    assert reader.read() == {"x": 1}
    assert reader.runs == 2


def test_an_in_memory_database_is_never_cached() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        assert result_cache.key_for(conn, name="probe", now=NOW) is None
    finally:
        conn.close()


# --- only the application's database; the version connection never writes -----------------


def _readonly_like_a_script(path: Path) -> sqlite3.Connection:
    """``scripts/merge_reconcile.py::_open_readonly``'s shape: mode=ro + a write-deny
    authorizer (the snapshot must leave the file byte-identical)."""
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True,
                           check_same_thread=False)
    writes = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
              sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_INDEX,
              sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_DROP_TABLE,
              sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_ATTACH}

    def deny_writes(action: int, *_args: object) -> int:
        return sqlite3.SQLITE_DENY if action in writes else sqlite3.SQLITE_OK

    conn.set_authorizer(deny_writes)
    return conn


def test_a_file_that_is_not_the_apps_database_is_never_cached_nor_held_open(
    tmp_path: Path, db: Path,
) -> None:
    """A script's copy (here a rollback-journal file, opened like ``merge_reconcile``'s
    read-only snapshot) computes directly, opens no version connection, is left byte for byte
    as it was — and can be deleted the moment its own connection closes (on Windows an open
    file cannot be)."""
    copy = _make_db(tmp_path / "copy.db", wal=False)
    before = _sha(copy)
    other = Reader(copy, _readonly_like_a_script(copy))
    try:
        assert result_cache.key_for(other.conn, name="probe", now=NOW) is None
        assert other.read() == other.read() == {"x": 1}
        assert other.runs == 2
        assert result_cache.stats()["probe"].uncached == 2
        assert result_cache._versions == {}, "a version connection was opened on the copy"
    finally:
        other.close()
    assert _sha(copy) == before
    copy.unlink()                                  # nothing else holds it open
    assert not copy.exists()


def test_the_version_connection_is_read_only_and_never_converts_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The app's database in rollback-journal mode, read through a mode=ro caller: a cached
    read leaves the file byte-identical and its journal mode unchanged (``journal_mode = WAL``
    on it would be a write), and the version connection itself cannot write."""
    path = _make_db(tmp_path / "rj.db", wal=False)
    _point_app_at(path, monkeypatch)
    before = _sha(path)
    reader = Reader(path, _readonly_like_a_script(path))
    try:
        assert reader.read() == reader.read() == {"x": 1}
        assert reader.runs == 1
        assert len(result_cache._versions) == 1
        held = next(iter(result_cache._versions.values())).conn
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            held.execute("CREATE TABLE z (x)")
    finally:
        reader.close()
    assert _sha(path) == before, "a cached read changed the file"
    _commit(path)                                  # a commit still misses
    reader = Reader(path, _readonly_like_a_script(path))
    try:
        assert reader.read() == {"x": 2}
    finally:
        reader.close()
    result_cache.reset()
    probe = sqlite3.connect(path)
    try:
        assert probe.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        probe.close()


def test_a_read_only_version_connection_reads_a_wal_file_with_live_side_files(
    db: Path, reader: Reader,
) -> None:
    """The app's WAL database with its -wal/-shm present (a writer holds it open): the
    read-only version connection opens, and every commit — another connection's, another
    process's — and a checkpoint move what it reads."""
    writer = sqlite3.connect(db)
    try:
        writer.execute("UPDATE t SET x = 2")
        writer.commit()
        assert db.with_name(db.name + "-wal").exists()
        assert db.with_name(db.name + "-shm").exists()
        assert reader.read() == {"x": 2}
        first = result_cache.key_for(reader.conn, name="probe", now=NOW)
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        second = result_cache.key_for(reader.conn, name="probe", now=NOW)
        assert first is not None and second is not None
        assert first.data_version != second.data_version
    finally:
        writer.close()


def test_the_apps_database_is_matched_by_file_not_by_spelling(
    db: Path, reader: Reader, monkeypatch: pytest.MonkeyPatch,
) -> None:
    relative = Path(os.path.relpath(db, Path.cwd())) if db.drive == Path.cwd().drive else db
    monkeypatch.setenv("DB_PATH", str(relative))
    get_settings.cache_clear()
    assert result_cache.is_app_database(str(db))
    assert result_cache.key_for(reader.conn, name="probe", now=NOW) is not None
    assert not result_cache.is_app_database(str(db.with_name("other.db")))


def test_the_key_names_the_callers_own_file(db: Path, reader: Reader) -> None:
    key = result_cache.key_for(reader.conn, name="probe", now=NOW, reporting="TWD",
                               extra=("1.0",))
    assert key is not None
    assert Path(key.db_path).resolve() == db.resolve()
    assert key.day == NOW.date()
    assert (key.reporting, key.extra) == ("TWD", ("1.0",))
    assert result_cache.key_for(reader.conn, name="probe", now=NOW, reporting="TWD",
                                extra=("1.00",)) != key


# --- ordering: the version is read before the snapshot -------------------------------------


def test_a_commit_between_the_version_read_and_the_snapshot_never_yields_an_older_entry(
    db: Path, reader: Reader, monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = result_cache.data_version
    fired = [False]

    def version_then_commit(path: str) -> tuple[int, int] | None:
        version = real(path)
        if not fired[0]:
            fired[0] = True
            _commit(db)                          # lands after the key, before the snapshot
        return version

    monkeypatch.setattr(result_cache, "data_version", version_then_commit)
    assert reader.read() == {"x": 2}             # the snapshot already sees the commit
    assert reader.read() == {"x": 2}             # the entry sits under the OLDER version: miss
    assert reader.runs == 2
    assert reader.read() == {"x": 2}
    assert reader.runs == 2


def test_a_commit_during_the_computation_makes_the_entry_unreachable(
    db: Path, reader: Reader,
) -> None:
    def compute() -> dict[str, int]:
        before = int(reader.conn.execute("SELECT x FROM t").fetchone()[0])
        _commit(db)                              # another writer, mid-build
        after = int(reader.conn.execute("SELECT x FROM t").fetchone()[0])
        assert before == after, "the build read two states"
        reader.runs += 1
        return {"x": after}

    first = result_cache.cached(reader.conn, name="probe", now=NOW, compute=compute, copy=_own)
    assert first == {"x": 1}                     # one consistent snapshot (PERF-S12)
    assert not reader.conn.in_transaction
    assert reader.read() == {"x": 2}             # the commit made the entry unreachable


def test_a_miss_runs_inside_one_read_transaction_and_ends_it(reader: Reader) -> None:
    seen: list[bool] = []

    def compute() -> dict[str, int]:
        seen.append(reader.conn.in_transaction)
        return reader.compute()

    result_cache.cached(reader.conn, name="probe", now=NOW, compute=compute, copy=_own)
    result_cache.cached(reader.conn, name="other", now=NOW, compute=compute, copy=_own,
                        snapshot=False)
    assert seen == [True, False]
    assert not reader.conn.in_transaction


def test_a_key_taken_inside_a_snapshot_is_none(reader: Reader) -> None:
    with result_cache.read_snapshot(reader.conn):
        reader.conn.execute("SELECT 1").fetchone()
        assert result_cache.key_for(reader.conn, name="probe", now=NOW) is None
    assert result_cache.key_for(reader.conn, name="probe", now=NOW) is not None


# --- in-flight sharing ---------------------------------------------------------------------


@pytest.mark.parametrize("window", [False, True], ids=["no-window", "another-write-window"])
def test_two_concurrent_identical_requests_compute_once(db: Path, window: bool) -> None:
    """Also while another request's write window is open (post-v0.1.32 item 1; before, each
    computed alone)."""
    if window:
        flight.begin_write()
    try:
        _two_concurrent_identical_requests_compute_once(db)
    finally:
        if window:
            flight.end_write()


def _two_concurrent_identical_requests_compute_once(db: Path) -> None:
    release = threading.Event()
    started = threading.Event()
    runs = [0]
    out: dict[str, dict[str, int]] = {}

    def request(name: str) -> None:
        conn = connect_path(db)

        def compute() -> dict[str, int]:
            runs[0] += 1
            started.set()
            assert release.wait(10)
            return {"x": int(conn.execute("SELECT x FROM t").fetchone()[0])}

        try:
            out[name] = result_cache.cached(conn, name="probe", now=NOW, compute=compute,
                                            copy=_own)
        finally:
            conn.close()

    first = threading.Thread(target=request, args=("first",))
    first.start()
    assert started.wait(10)
    second = threading.Thread(target=request, args=("second",))
    second.start()
    deadline = time.monotonic() + 10
    while not any(f.joiners for f in flight._flights.values()):
        assert time.monotonic() < deadline, "the second request never joined"
        time.sleep(0.005)
    release.set()
    first.join(30)
    second.join(30)
    assert runs[0] == 1
    assert out["first"] == out["second"] == {"x": 1}
    assert out["first"] is not out["second"]


# --- pre-warm ------------------------------------------------------------------------------


def _wait_for(pred: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 5
    while not pred():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def test_prewarm_is_off_unless_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PD_PREWARM", "0")
    ran: list[int] = []
    assert result_cache.schedule_prewarm(lambda: ran.append(1), 0.0) is False
    time.sleep(0.05)
    assert ran == []


def test_prewarm_is_debounced_to_the_last_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PD_PREWARM", "1")
    ran: list[str] = []
    assert result_cache.schedule_prewarm(lambda: ran.append("first"), 0.2)
    assert result_cache.schedule_prewarm(lambda: ran.append("second"), 0.05)
    _wait_for(lambda: bool(ran))
    time.sleep(0.3)
    assert ran == ["second"]


def test_prewarm_skips_while_a_write_window_is_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PD_PREWARM", "1")
    ran: list[int] = []
    done = threading.Event()
    real = result_cache._run_prewarm

    def run(fn: Callable[[], object]) -> None:
        real(fn)
        done.set()

    monkeypatch.setattr(result_cache, "_run_prewarm", run)
    flight.begin_write()
    try:
        result_cache.schedule_prewarm(lambda: ran.append(1), 0.0)
        assert done.wait(5)
    finally:
        flight.end_write()
    assert ran == []


def test_a_failing_prewarm_is_logged_not_raised(monkeypatch: pytest.MonkeyPatch,
                                                caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setenv("PD_PREWARM", "1")
    done = threading.Event()

    def boom() -> None:
        done.set()
        raise RuntimeError("prewarm broke")

    result_cache.schedule_prewarm(boom, 0.0)
    assert done.wait(5)
    _wait_for(lambda: any("pre-warm failed" in r.getMessage() for r in caplog.records))


# --- the stop: shutdown() ------------------------------------------------------------------


class BlockedPrewarm:
    """A pre-warm shaped like ``api/alert_inputs.prewarm_today``: its own connection, a cached
    "dashboard" build that reads (so its read snapshot holds the WAL) and then blocks until
    released, then the "alerts" key. It records that second key (``None`` = computed
    uncached, no version connection touched)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.alerts_keys: list[result_cache.CacheKey | None] = []

    def __call__(self) -> None:
        conn = connect_path(self.path)
        try:
            def build() -> dict[str, int]:
                # read FIRST: the snapshot's read lock is held from the first read on
                value = {"x": int(conn.execute("SELECT x FROM t").fetchone()[0])}
                self.started.set()
                assert self.release.wait(30), "never released"
                return value

            result_cache.cached(conn, name="dashboard", now=NOW, compute=build, copy=_own)
            self.alerts_keys.append(result_cache.key_for(conn, name="alerts", now=NOW))
        finally:
            conn.close()
            self.finished.set()


@pytest.fixture
def prewarm(db: Path) -> Iterator[BlockedPrewarm]:
    """A :class:`BlockedPrewarm` on the app's database, always released at teardown (before
    the autouse fixture checks that no flight outlived its computation)."""
    blocked = BlockedPrewarm(db)
    yield blocked
    blocked.release.set()
    if blocked.started.is_set():
        assert blocked.finished.wait(30), "the blocked pre-warm never finished"


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every version connection the cache opens, by path."""
    seen: list[str] = []
    real = result_cache._open_version_conn

    def spy(path: str, identity: tuple[int, int]) -> object:
        seen.append(path)
        return real(path, identity)

    monkeypatch.setattr(result_cache, "_open_version_conn", spy)
    return seen


def _side_files(path: Path) -> list[str]:
    return [s for s in ("-wal", "-shm") if path.with_name(path.name + s).exists()]


def test_shutdown_waits_for_a_running_prewarm_then_the_checkpoint_truncates(
    db: Path, prewarm: BlockedPrewarm, opened: list[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-warm blocked mid-build when the stop begins: shutdown waits for it, the pre-warm
    opens no version connection after the stop began (its alerts read computes uncached),
    and the lifespan's TRUNCATE checkpoint completes — ONE file left (docs/DEPLOYMENT.md)."""
    monkeypatch.setenv("PD_PREWARM", "1")
    assert result_cache.schedule_prewarm(prewarm, 0.0)
    assert prewarm.started.wait(5)
    _commit(db)  # WAL frames the pre-warm's snapshot predates: a checkpoint must wait for it
    assert opened == [str(db)]                       # the dashboard key, before the stop
    returned: list[bool] = []
    stopper = threading.Thread(target=lambda: returned.append(result_cache.shutdown(10.0)))
    stopper.start()
    time.sleep(0.3)
    assert stopper.is_alive(), "shutdown returned while a pre-warm still ran"
    assert not result_cache.schedule_prewarm(lambda: None, 0.0), "a pre-warm after the stop"
    prewarm.release.set()
    stopper.join(10)
    assert returned == [True]
    assert prewarm.finished.is_set() and prewarm.alerts_keys == [None]
    assert opened == [str(db)], "a version connection was reopened after the stop began"
    assert result_cache._versions == {}
    conn = connect_path(db)
    try:
        busy, _log, _done = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        conn.close()
    assert busy == 0
    assert _side_files(db) == []


def test_a_prewarm_that_outlives_the_wait_is_logged_and_reopens_nothing(
    db: Path, prewarm: BlockedPrewarm, opened: list[str], monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The bounded wait runs out: shutdown says so (``False`` and a warning naming the
    consequence), closes the version connections anyway, and never hangs. The late pre-warm
    then finishes uncached — no version connection, no entry kept."""
    monkeypatch.setenv("PD_PREWARM", "1")
    assert result_cache.schedule_prewarm(prewarm, 0.0)
    assert prewarm.started.wait(5)
    started = time.monotonic()
    assert result_cache.shutdown(0.2) is False
    assert time.monotonic() - started < 5
    assert result_cache._versions == {}
    warned = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("pre-warm" in m and "-wal" in m for m in warned), warned
    prewarm.release.set()
    assert prewarm.finished.wait(10)
    assert prewarm.alerts_keys == [None]
    assert opened == [str(db)] and result_cache._versions == {}
    assert result_cache._entries == {}, "the late pre-warm kept an entry after the stop"


def test_shutdown_cancels_a_pending_prewarm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PD_PREWARM", "1")
    ran: list[int] = []
    assert result_cache.schedule_prewarm(lambda: ran.append(1), 0.2)
    assert result_cache.shutdown(1.0) is True
    time.sleep(0.4)
    assert ran == []


def test_reset_opens_the_cache_again_after_a_shutdown(reader: Reader) -> None:
    """``shutdown`` is final for a stopping process; ``reset`` gives a working cache back —
    the conftest calls it around every test, and the lifespan at its start (a second
    lifespan in one process: ``tests/settlement/test_boot.py``)."""
    assert result_cache.shutdown(1.0) is True
    assert reader.read() == reader.read() == {"x": 1}
    assert reader.runs == 2                          # closed: every read computes
    result_cache.reset()
    assert reader.read() == reader.read() == {"x": 1}
    assert reader.runs == 3                          # open again: the second read is a hit
