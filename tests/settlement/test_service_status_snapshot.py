"""One settlement answer describes ONE database state (stress audit phase 1, 2026-10-22).

The stress harness read ``GET /api/settlement/status`` at the end of run #4 and got
generation 3, the run's claimed day as a stale range (待結算) and NO active run — a combination
the database never holds: the swap clears ``settle_meta.run_*`` and marks the report
``published`` in ONE transaction. ``status()`` issued its SELECTs in autocommit, each on its own
snapshot, and the child's swap committed between ``store_state`` (before it) and
``active_or_unfinished_runs`` (after it). Every settlement read seam now reads one snapshot:

- ``status()`` — the swap committed between two of its reads changes nothing it answers; the
  settle lock is observed BEFORE the snapshot opens as well as after its reads, so a child that
  swaps and exits during the read is not reported 已中斷 for a run the snapshot shows running;
- the panel's history (``runs_page``), one run's report (``run_report``), its diff
  (``diff_rows``) and the change trail (``changes_page``) — a commit between their COUNT and
  their rows, or between a report's JSON and its columns, is not mixed into the answer.

Each interleaved write is committed on ANOTHER connection between two reads of the read under
test; the answer must equal the answer read before that write, and the write must be visible
to the same read afterwards (so the test would see a mixed answer).
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from portfolio_dash.api import settlement_service as service
from portfolio_dash.portfolio import settlement_store as st
from portfolio_dash.portfolio.settlement_models import DiffRow, SettlementStatus
from portfolio_dash.shared import proc_lock
from portfolio_dash.shared.config import get_settings
from portfolio_dash.shared.enums import Currency
from tests.reference.corpora import open_db
from tests.settlement import service_support as sup
from tests.settlement import stub_settle_worker as stub
from tests.settlement.conftest import CorpusFactory

#: A back-dated trade before the live window: once a run claims it, the run's claimed day is
#: the store's dirty day and the days before the window are its labelled stale range.
_BACK = date(2026, 2, 2)
_LOCK_TAKER = re.compile(r"^\s*BEGIN\s+(IMMEDIATE|EXCLUSIVE)\b", re.I)
_WRITE = re.compile(r"^\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP)\b", re.I)


@pytest.fixture
def path(corpus_db: CorpusFactory, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    yield sup.prepare(corpus_db("golden").path, monkeypatch)
    service.reset_for_tests()
    get_settings.cache_clear()


def _through() -> date:
    return service._now().date() - timedelta(days=1)


def _claimed_run(path: Path) -> tuple[int, st.Claim]:
    """Generation 1 published, a back-dated change, and a run that claimed it: a worker
    between its claim and its swap."""
    service.start_run(scope="full", reason="button", started_by="user:guest")
    sup.back_dated_trade(path, _BACK)
    run_id = sup.fake_preparing_run(path, reason="prompt", started_by="user:guest",
                                    through=_through())
    conn = open_db(path)
    try:
        claim = st.claim(conn, run_id, scope="dirty", run_through=_through())
        conn.commit()
    finally:
        conn.close()
    return run_id, claim


def _publish(path: Path, run_id: int, claim: st.Claim) -> None:
    """The worker's swap, committed on its own connection."""
    conn = open_db(path)
    try:
        stub._publish(conn, run_id, claim)
        conn.commit()
    finally:
        conn.close()


def _status_on(conn: sqlite3.Connection, now: datetime) -> SettlementStatus:
    return service.status(conn, viewer="user:guest", now=now, reporting=Currency.TWD)


def _status(path: Path, now: datetime) -> SettlementStatus:
    conn = open_db(path)
    try:
        return _status_on(conn, now)
    finally:
        conn.close()


def _no_mix(answer: SettlementStatus) -> None:
    """The combination the stress audit saw: no run active, yet a stale range that only an
    active run's claim produces (with no run, the stale range starts at ``dirty_from``)."""
    if answer.run is None:
        assert answer.stale_from in (None, answer.dirty_from), (
            f"no active run but a stale range from a claim: {answer.state} "
            f"gen {answer.generation} dirty_from {answer.dirty_from} "
            f"stale {answer.stale_from}..{answer.stale_to}")


# --- status() ------------------------------------------------------------------------------


def test_a_swap_between_two_status_reads_is_not_mixed_into_the_answer(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stress audit's failure, made deterministic: the swap commits after ``store_state``
    and before ``active_or_unfinished_runs``. The answer is the pre-swap picture entire (the
    run active, its claim still the stale range, generation 1) — never generation 1 with the
    stale range and no run."""
    run_id, claim = _claimed_run(path)
    now = service._now()
    swapped: list[int] = []
    real = st.active_or_unfinished_runs

    def swap_first(conn: sqlite3.Connection) -> list[Any]:
        if not swapped:
            _publish(path, run_id, claim)  # the child's swap commits HERE
            swapped.append(run_id)
        return real(conn)

    with proc_lock.lock(proc_lock.settlement_lock_path(), timeout=2):  # the child, alive
        before = _status(path, now)
        monkeypatch.setattr(st, "active_or_unfinished_runs", swap_first)
        answer = _status(path, now)
        monkeypatch.setattr(st, "active_or_unfinished_runs", real)
    assert swapped == [run_id]
    _no_mix(answer)
    assert answer.state == "running" and answer.run is not None
    assert answer.run.run_id == run_id and answer.generation == 1
    assert answer.dirty_from is None and answer.stale_from == _BACK
    assert answer == before
    after = _status(path, now)
    assert after.state == "clean" and after.run is None and after.generation == 2
    assert after.stale_from is None and after.settled_through is not None
    assert after.live_from == after.settled_through + timedelta(days=1)


def test_a_child_that_swaps_and_exits_during_the_read_reads_running_not_interrupted(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock probe is not a database read, so it cannot be inside the snapshot: it is
    taken BEFORE the snapshot opens and again after its reads. A child alive when the read
    began, that swaps and exits while it runs, is still running in the snapshot's state —
    reading 已中斷 there would pair the pre-swap database with the post-exit process table."""
    run_id, claim = _claimed_run(path)
    now = service._now()
    child = proc_lock.lock(proc_lock.settlement_lock_path(), timeout=2)
    swapped: list[int] = []
    real = st.active_or_unfinished_runs

    def swap_and_exit(conn: sqlite3.Connection) -> list[Any]:
        if not swapped:
            _publish(path, run_id, claim)
            child.release()  # the child exits right after its swap
            swapped.append(run_id)
        return real(conn)

    try:
        monkeypatch.setattr(st, "active_or_unfinished_runs", swap_and_exit)
        answer = _status(path, now)
        monkeypatch.setattr(st, "active_or_unfinished_runs", real)
    finally:
        child.release()
    assert swapped == [run_id]
    _no_mix(answer)
    assert answer.state == "running" and answer.run is not None
    assert answer.run.run_id == run_id and answer.generation == 1
    after = _status(path, now)
    assert after.state == "clean" and after.generation == 2 and after.run is None


def test_a_child_that_starts_during_the_read_is_not_read_interrupted(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other edge of the same probe: the lock is free when the read begins, and a child
    takes it and claims before the snapshot opens. The probe after the reads sees it; the run
    reads as starting or running, never 已中斷."""
    service.start_run(scope="full", reason="button")
    sup.back_dated_trade(path, _BACK)
    run_id = sup.fake_preparing_run(path, through=_through())
    now = service._now()
    held: list[proc_lock.LockHandle] = []
    real = st.store_state

    def start_first(conn: sqlite3.Connection, **kwargs: Any) -> st.StoreState:
        if not held:
            held.append(proc_lock.lock(proc_lock.settlement_lock_path(), timeout=2))
            other = open_db(path)
            try:
                st.claim(other, run_id, scope="dirty", run_through=_through())
                other.commit()
            finally:
                other.close()
        return real(conn, **kwargs)

    try:
        monkeypatch.setattr(st, "store_state", start_first)
        answer = _status(path, now)
        monkeypatch.setattr(st, "store_state", real)
    finally:
        for handle in held:
            handle.release()
    assert held
    assert answer.state in ("preparing", "running")
    assert answer.run is not None and answer.run.run_id == run_id


def test_status_reads_one_deferred_snapshot_and_answers_under_a_held_write_lock(
    path: Path,
) -> None:
    """DEF-065 still holds: the snapshot is a READ transaction (a deferred ``BEGIN``, never
    ``BEGIN IMMEDIATE``), it writes nothing, it ends before ``status()`` returns, and a WAL
    reader is not blocked by another connection's write lock (busy timeout 0.2 s here: a lock
    taker would fail fast)."""
    run_id, _claim = _claimed_run(path)
    now = service._now()
    holder = sqlite3.connect(str(path), isolation_level=None)
    conn = sqlite3.connect(str(path), timeout=0.2, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("UPDATE settle_dirty SET seq = seq WHERE id = 1")
        with proc_lock.lock(proc_lock.settlement_lock_path(), timeout=2):
            answer = _status_on(conn, now)
        assert not conn.in_transaction and conn.total_changes == 0
    finally:
        holder.execute("ROLLBACK")
        holder.close()
        conn.close()
    assert answer.state == "running" and answer.run is not None and answer.run.run_id == run_id
    begins = [s for s in statements if s.strip().upper().startswith("BEGIN")]
    assert begins == ["BEGIN"], begins
    assert not [s for s in statements if _LOCK_TAKER.match(s) or _WRITE.match(s)]
    assert statements[-1].strip().upper() in ("COMMIT", "ROLLBACK")


# --- the other settlement read seams --------------------------------------------------------


class _Interleave:
    """Commit *write* on another connection the moment *conn* starts the first statement that
    *when* matches — before that statement takes its read snapshot (SQLite traces a statement
    when it begins running, ahead of its read lock)."""

    def __init__(self, conn: sqlite3.Connection, when: Callable[[str], bool],
                 write: Callable[[], object]) -> None:
        self.fired = False
        self.errors: list[BaseException] = []
        self._when = when
        self._write = write
        conn.set_trace_callback(self._trace)

    def _trace(self, sql: str) -> None:
        if self.fired or not self._when(sql):
            return
        self.fired = True
        try:
            self._write()
        except BaseException as exc:  # noqa: BLE001 — a trace callback swallows exceptions
            self.errors.append(exc)

    def check(self) -> None:
        assert not self.errors, self.errors
        assert self.fired, "the interleaved write never ran"


def _interleaved(path: Path, read: Callable[[sqlite3.Connection], Any],
                 when: Callable[[str], bool], write: Callable[[], object]) -> None:
    """``read`` with *write* committed between two of its statements answers exactly what it
    answered before the write; afterwards it sees the write (else the test proves nothing)."""

    def fresh() -> Any:
        conn = open_db(path)
        try:
            return read(conn)
        finally:
            conn.close()

    before = fresh()
    conn = open_db(path)
    try:
        hook = _Interleave(conn, when, write)
        answer = read(conn)
        conn.set_trace_callback(None)
        assert not conn.in_transaction
    finally:
        conn.close()
    hook.check()
    after = fresh()
    assert after != before, "the interleaved write is invisible to this read"
    assert answer == before, "the answer mixes two database states"


def _sql(*parts: str, without: str | None = None) -> Callable[[str], bool]:
    def match(sql: str) -> bool:
        return all(p in sql for p in parts) and (without is None or without not in sql)

    return match


def test_the_run_history_page_counts_the_rows_it_lists(path: Path) -> None:
    """``GET /api/settlement/runs``: a run created between the COUNT and the page's rows."""
    service.start_run(scope="full", reason="button")
    _interleaved(
        path,
        lambda c: service.runs_page(c, viewer="user:guest", limit=20, offset=0,
                                    only_changes=False),
        _sql("FROM settle_run r WHERE", "ORDER BY r.run_id", without="COUNT(*)"),
        lambda: sup.fake_preparing_run(path))


def test_one_run_report_is_never_half_published(path: Path) -> None:
    """``GET /api/settlement/runs/{id}``: the swap commits between the report's JSON and its
    columns — never a ``published`` summary over the running report's body."""
    run_id, claim = _claimed_run(path)
    _interleaved(
        path, lambda c: service.run_report(c, run_id, viewer="user:guest"),
        _sql("SELECT run_id, status, resumed"),
        lambda: _publish(path, run_id, claim))


def test_a_run_diff_page_counts_the_rows_it_lists(path: Path) -> None:
    """``GET /api/settlement/runs/{id}/diff``: diff rows written between the COUNT and the
    rows."""
    run_id, _claim = _claimed_run(path)

    def write() -> None:
        conn = open_db(path)
        try:
            st.write_diff(conn, run_id, [
                DiffRow(run_id=run_id, day=_BACK + timedelta(days=i), field="total_value",
                        kind="changed", before=Decimal("1"), after=Decimal("2"))
                for i in range(3)])
            conn.commit()
        finally:
            conn.close()

    _interleaved(
        path,
        lambda c: service.diff_rows(c, run_id, field_name=None, kind=None, limit=50,
                                    offset=0),
        _sql("FROM settle_run_diff WHERE", "ORDER BY day", without="COUNT(*)"),
        write)


def test_the_pending_trail_counts_the_records_it_lists(path: Path) -> None:
    """``GET /api/settlement/changes?state=pending``: a change recorded between the COUNT and
    the records."""
    service.start_run(scope="full", reason="button")
    sup.back_dated_trade(path, _BACK)
    _interleaved(
        path,
        lambda c: service.changes_page(c, state="pending", viewer="user:guest", limit=50,
                                       offset=0),
        _sql("FROM settle_change_log WHERE", "ORDER BY id", without="COUNT(*)"),
        lambda: sup.back_dated_trade(path, _BACK + timedelta(days=1)))
