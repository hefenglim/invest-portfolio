"""Cross-process exclusive locks on files in the data folder (PERF plan W0.7, spec §9.4).

**What they are for — liveness, never data.** A lock is held by a live process and released by
the OS the moment that process dies, however it dies. That is why the settlement uses a lock,
not a heartbeat, to tell a slow child from a dead one (spec §9.4 "Liveness is the OS lock").

**The lock files the program uses** (``<data>`` = the folder of the configured database,
``get_settings().db_path``, resolved — see :func:`data_dir`):

- ``<data>/settlement.lock`` — held by the settle child for its whole life; probed by status
  reads and by boot (spec §9.4).
- ``<data>/heavy-child.lock`` — held by ``child_runner``'s gate while a heavy child lives, so
  one data folder never runs two heavy children, whichever process spawned them.
- ``<data>/job-<run_id>.lock`` — held by a job child for its whole life, so boot can finalise
  an orphan ``running`` job row whose lock is free.

A lock file holds no data and is never deleted: on Windows the lock is mandatory (another
process cannot even read the locked byte) and an open file cannot be removed; on POSIX,
deleting a lock file while someone holds it lets a second process lock a new file of the same
name. Creating the parent folder is the caller's job; creating the file is the lock's.

**POSIX: ``fcntl.flock``, never ``lockf``.** A ``lockf`` / ``fcntl(F_SETLK)`` record lock belongs
to the PROCESS, so closing *any* descriptor of the file anywhere in that process drops it —
one stray ``open().close()`` of a lock file would silently free the settlement. ``flock``
belongs to the open file description, so only this handle's ``close`` releases it, and two
handles in one process conflict exactly as two processes do.

**Windows: ``msvcrt.locking`` on byte 0** after ``os.lseek(fd, 0, 0)`` (``locking`` locks from
the current position). ``LK_NBLCK`` is the probe and raises ``PermissionError`` (EACCES) when
the byte is held. ``LK_LOCK`` is NOT a blocking lock: it retries ten times, one second apart,
and then raises ``OSError`` errno 36 (EDEADLOCK) — measured 9.1 s on the dev machine — so
:func:`lock` is a retry loop over ``LK_NBLCK``. Locks are per handle here too: a second handle
in the same process is refused. Locking byte 0 of the empty file works (a lock may lie past EOF).

**A probe is a brief acquisition.** :func:`is_held` takes the lock and releases it at once, so
a holder that must not lose a race against a concurrent probe (a child taking its role lock
while a status read probes it) takes the lock with :func:`lock` and a timeout of a few seconds,
never with :func:`try_lock`.
"""

from __future__ import annotations

import errno
import os
import sys
import time
from pathlib import Path
from types import TracebackType

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

SETTLEMENT_LOCK = "settlement.lock"
HEAVY_CHILD_LOCK = "heavy-child.lock"

#: errno values that mean "another handle holds the lock" (as opposed to a real I/O error).
_HELD_ERRNOS: frozenset[int] = frozenset(
    {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK}
)

#: How long :func:`lock` sleeps between two non-blocking attempts.
DEFAULT_POLL_S = 0.05

PathLike = str | os.PathLike[str]


class LockHandle:
    """One acquired lock. :meth:`release` (or leaving the ``with`` block) frees it."""

    def __init__(self, path: Path, fd: int) -> None:
        self._path = path
        self._fd: int | None = fd

    @property
    def path(self) -> Path:
        return self._path

    @property
    def held(self) -> bool:
        """True until :meth:`release` runs."""
        return self._fd is not None

    def release(self) -> None:
        """Unlock and close. Idempotent: a second call does nothing."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock(fd)
        finally:
            os.close(fd)

    def __enter__(self) -> LockHandle:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        self.release()

    def __repr__(self) -> str:
        return f"LockHandle({str(self._path)!r}, held={self.held})"


def _is_held_error(exc: OSError) -> bool:
    return isinstance(exc, PermissionError | BlockingIOError) or exc.errno in _HELD_ERRNOS


def _try_acquire(fd: int) -> bool:
    """One non-blocking attempt on an open descriptor; False when another handle holds it."""
    try:
        if sys.platform == "win32":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if _is_held_error(exc):
            return False
        raise
    return True


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _open_for_lock(path: Path) -> int:
    # O_RDWR | O_CREAT: the lock creates its own file. Descriptors are non-inheritable by
    # default (PEP 446), so a spawned child never carries this lock with it.
    return os.open(path, os.O_RDWR | os.O_CREAT, 0o644)


def try_lock(path: PathLike) -> LockHandle | None:
    """Take the lock if it is free right now; ``None`` if another handle holds it."""
    p = Path(path)
    fd = _open_for_lock(p)
    try:
        acquired = _try_acquire(fd)
    except BaseException:
        os.close(fd)
        raise
    if not acquired:
        os.close(fd)
        return None
    return LockHandle(p, fd)


def lock(path: PathLike, timeout: float, *, poll_s: float = DEFAULT_POLL_S) -> LockHandle:
    """Take the lock, retrying every *poll_s* seconds for up to *timeout* seconds.

    Raises ``TimeoutError`` naming the file when it is still held at the deadline. A
    *timeout* of 0 makes exactly one attempt (``try_lock`` that raises instead of returning
    ``None``).
    """
    if timeout < 0:
        raise ValueError(f"timeout must be >= 0, got {timeout}")
    p = Path(path)
    deadline = time.monotonic() + timeout
    fd = _open_for_lock(p)
    try:
        while True:
            if _try_acquire(fd):
                return LockHandle(p, fd)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"lock {p} is still held after {timeout:.1f} s")
            time.sleep(min(poll_s, remaining))
    except BaseException:
        os.close(fd)
        raise


def is_held(path: PathLike) -> bool:
    """Whether another handle holds the lock right now. Never blocks, never creates the file.

    A missing file means free (nobody ever took it). The probe takes the lock and releases
    it at once (see the module docstring); ``PermissionError`` / EACCES / EAGAIN — on the
    open or on the attempt — mean held.
    """
    p = Path(path)
    try:
        # Read-only is enough for both flock and msvcrt.locking, and cannot fail on a lock
        # file the probing user may read but not write.
        fd = os.open(p, os.O_RDONLY)
    except FileNotFoundError:
        return False
    except PermissionError:
        return True
    try:
        if not _try_acquire(fd):
            return True
        _unlock(fd)
        return False
    finally:
        os.close(fd)


def data_dir() -> Path:
    """The data folder: the configured database's parent, as an absolute path."""
    # Imported here, not at module level: ``config`` pulls in pydantic-settings (measured
    # 2.1 s cold on the dev machine), and the lock primitive must stay stdlib-cheap for a
    # probe or a small child that never needs the settings.
    from .config import get_settings

    return get_settings().db_path.resolve().parent


def settlement_lock_path(data: Path | None = None) -> Path:
    """``<data>/settlement.lock`` — held by the settle child for its whole life."""
    return (data if data is not None else data_dir()) / SETTLEMENT_LOCK


def heavy_child_lock_path(data: Path | None = None) -> Path:
    """``<data>/heavy-child.lock`` — held by the runner's gate while a heavy child lives."""
    return (data if data is not None else data_dir()) / HEAVY_CHILD_LOCK


def job_lock_path(run_id: int, data: Path | None = None) -> Path:
    """``<data>/job-<run_id>.lock`` — held by a job child for its whole life."""
    return (data if data is not None else data_dir()) / f"job-{run_id}.lock"
