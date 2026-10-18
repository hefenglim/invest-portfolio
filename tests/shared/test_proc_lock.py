"""``shared/proc_lock.py``: cross-process exclusive locks (PERF plan W0.7, spec §9.4).

The holder-process tests run a real child (``tests/shared/child_fixtures/lock_holder.py``) —
liveness by OS lock is the whole point, and only another process can show it. Windows is what
runs locally; the POSIX branch (``fcntl.flock``) is exercised by the same tests on Linux.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from portfolio_dash.shared import proc_lock
from portfolio_dash.shared.child_runner import package_root
from portfolio_dash.shared.config import get_settings

_FIXTURES = "tests.shared.child_fixtures"


def _wait_for(predicate: Callable[[], bool], timeout: float = 5.0, step: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return bool(predicate())


@pytest.fixture
def lock_path(tmp_path: Path) -> Path:
    return tmp_path / "x.lock"


@pytest.fixture
def holder(lock_path: Path, tmp_path: Path) -> Iterator[subprocess.Popen[bytes]]:
    """Another process holding *lock_path* until the test kills it."""
    ready = tmp_path / "ready.json"
    proc = subprocess.Popen(
        [sys.executable, "-m", f"{_FIXTURES}.lock_holder", str(lock_path), str(ready)],
        cwd=str(package_root()), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        if not _wait_for(ready.exists, timeout=15.0):
            proc.kill()
            _, err = proc.communicate(timeout=5)
            pytest.fail(f"lock holder never got ready: {err.decode(errors='replace')}")
        assert json.loads(ready.read_text(encoding="utf-8"))["held"] is True
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
        if proc.stderr is not None:
            proc.stderr.close()


# -- one process ---------------------------------------------------------------------------
def test_try_lock_creates_an_empty_file_and_holds_it(lock_path: Path) -> None:
    assert not lock_path.exists()
    handle = proc_lock.try_lock(lock_path)
    assert handle is not None and handle.held and handle.path == lock_path
    try:
        assert lock_path.exists() and lock_path.stat().st_size == 0   # a lock holds no data
        assert proc_lock.is_held(lock_path)
        # A second handle in the SAME process is refused, exactly as another process is
        # (flock: per open file description; msvcrt: per handle). A lockf lock would let it in.
        assert proc_lock.try_lock(lock_path) is None
    finally:
        handle.release()
    assert not handle.held
    assert not proc_lock.is_held(lock_path)
    handle.release()                                  # idempotent
    again = proc_lock.try_lock(lock_path)
    assert again is not None
    again.release()


def test_the_context_manager_releases(lock_path: Path) -> None:
    with proc_lock.lock(lock_path, timeout=1.0) as handle:
        assert handle.held and proc_lock.is_held(lock_path)
    assert not handle.held
    assert not proc_lock.is_held(lock_path)


def test_is_held_on_a_missing_file_is_free_and_creates_nothing(lock_path: Path) -> None:
    assert proc_lock.is_held(lock_path) is False
    assert not lock_path.exists()


def test_a_probe_leaves_a_free_lock_free(lock_path: Path) -> None:
    first = proc_lock.try_lock(lock_path)
    assert first is not None
    first.release()
    for _ in range(3):
        assert proc_lock.is_held(lock_path) is False
    handle = proc_lock.try_lock(lock_path)
    assert handle is not None
    handle.release()


def test_lock_times_out_with_a_clear_error(lock_path: Path) -> None:
    with proc_lock.lock(lock_path, timeout=0):
        started = time.monotonic()
        with pytest.raises(TimeoutError) as caught:
            proc_lock.lock(lock_path, timeout=0.3)
        elapsed = time.monotonic() - started
    assert 0.25 <= elapsed < 2.0                      # Windows clock: ~16 ms steps
    assert str(lock_path) in str(caught.value)
    assert not proc_lock.is_held(lock_path)          # the failed attempt left nothing behind


def test_lock_retries_until_the_holder_lets_go(lock_path: Path) -> None:
    """``lock`` is a retry loop (Windows ``LK_LOCK`` would give up after ~9 s on its own)."""
    first = proc_lock.try_lock(lock_path)
    assert first is not None
    timer = threading.Timer(0.3, first.release)
    timer.start()
    try:
        started = time.monotonic()
        with proc_lock.lock(lock_path, timeout=5.0) as second:
            assert second.held
            assert time.monotonic() - started >= 0.2
    finally:
        timer.cancel()
        first.release()


def test_timeout_must_not_be_negative(lock_path: Path) -> None:
    with pytest.raises(ValueError):
        proc_lock.lock(lock_path, timeout=-1)


def test_closing_another_descriptor_of_the_file_keeps_the_lock(lock_path: Path) -> None:
    """Why ``flock`` and never ``lockf``: a POSIX record lock dies with ANY close of the file
    in the holding process, so a stray open/close would silently free the settlement."""
    with proc_lock.lock(lock_path, timeout=1.0):
        fd = os.open(lock_path, os.O_RDONLY)
        os.close(fd)
        with open(lock_path, "rb"):
            pass
        assert proc_lock.is_held(lock_path)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX branch: fcntl")
def test_posix_never_uses_lockf(lock_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import fcntl

    def _refuse(*_: object) -> None:
        raise AssertionError("proc_lock must use flock, never lockf")

    monkeypatch.setattr(fcntl, "lockf", _refuse)
    with proc_lock.lock(lock_path, timeout=1.0):
        assert proc_lock.is_held(lock_path)
    assert not proc_lock.is_held(lock_path)


# -- another process -----------------------------------------------------------------------
def test_a_lock_held_by_another_process(lock_path: Path,
                                        holder: subprocess.Popen[bytes]) -> None:
    assert proc_lock.is_held(lock_path)
    assert proc_lock.try_lock(lock_path) is None
    with pytest.raises(TimeoutError):
        proc_lock.lock(lock_path, timeout=0.2)
    assert holder.poll() is None


def test_killing_the_holder_frees_the_lock(lock_path: Path,
                                           holder: subprocess.Popen[bytes]) -> None:
    """Liveness is the OS lock: however the holder dies, the OS releases it (measured on
    Windows: free within 3–10 ms of the kill)."""
    assert proc_lock.is_held(lock_path)
    holder.kill()
    holder.wait(timeout=10)
    assert _wait_for(lambda: not proc_lock.is_held(lock_path), timeout=5.0)
    handle = proc_lock.try_lock(lock_path)
    assert handle is not None
    handle.release()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows locks are mandatory")
def test_windows_another_process_cannot_read_the_locked_byte(
        lock_path: Path, holder: subprocess.Popen[bytes]) -> None:
    """Measured: the read fails even though the file is empty, which is why a lock file
    holds no data."""
    with open(lock_path, "rb") as fh, pytest.raises(PermissionError):
        fh.read(1)


# -- the program's lock files --------------------------------------------------------------
def test_the_lock_files_live_in_the_data_folder() -> None:
    data = get_settings().db_path.resolve().parent
    assert proc_lock.data_dir() == data
    assert proc_lock.settlement_lock_path() == data / "settlement.lock"
    assert proc_lock.heavy_child_lock_path() == data / "heavy-child.lock"
    assert proc_lock.job_lock_path(412) == data / "job-412.lock"
    other = Path("elsewhere")
    assert proc_lock.settlement_lock_path(other) == other / "settlement.lock"
    assert proc_lock.job_lock_path(7, other) == other / "job-7.lock"
