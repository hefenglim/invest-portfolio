"""One way to run a Python child process (PERF plan P6, W0.7; spec §9.2, §9.4, §16).

Used by the settlement service (the settle child) and by the job dispatcher (job workers):

- :func:`spawn` runs ``sys.executable -m <module> <args>`` and returns a :class:`ChildHandle`
  at once. A waiter thread per child waits for it and then calls ``on_exit`` with a
  :class:`ChildResult` — on a clean exit, an exception, an external kill, a cancel while
  queued, and when the process could not even be started. ``on_exit`` runs exactly once.
- **The heavy gate.** ``spawn(..., heavy=True)`` queues: at most one heavy child runs at a
  time, in FIFO order (an in-process ticket queue), and while it lives the gate holds
  ``<data>/heavy-child.lock`` (``shared/proc_lock.py``), so two processes sharing a data
  folder never run two heavy children at once either. A non-heavy spawn never waits.
  The gate is released before ``on_exit`` runs, so a slow callback — or one that spawns the
  chained run — never holds up the queue.
- ``on_start`` (optional) runs on the waiter thread once the child's turn has come — after
  the heavy gate granted it, for a heavy child — and before its process starts, unless the
  child was cancelled by then (in the queue, or as its turn came). A cancel that lands WHILE
  ``on_start`` runs, or right after it, still keeps the process from starting: ``on_start``
  ran and the process did not. So pair it with ``on_exit``, which fires either way — a
  bracket opened in ``on_start`` is then always closed. The job dispatcher marks the
  settlement's change trail there, so a mark taken before a long gate wait does not count
  the owner's changes made during it as the job's (code review 2026-10-20). Keep it short:
  the gate is held while it runs. An exception it raises is logged and the child still
  starts.
- :meth:`ChildHandle.terminate_and_wait` terminates, waits, kills if still alive and waits
  again; for a child still in the queue it cancels it instead. It returns once the process
  is gone AND ``on_exit`` has returned.
- :func:`stop_all` is the app's stop (``api/app.py``'s lifespan, before its WAL checkpoint):
  every child this runner spawned that is still alive is cancelled and terminated at once —
  none that is queued starts when the gate passes the turn on — and waited for, within ONE
  bound, until its process is gone and its ``on_exit`` has returned. Each result says so
  (:attr:`ChildResult.stopped`), so its owner records an interruption, not a cancel or a
  crash. A child left running would keep the database open (a TRUNCATE checkpoint cannot
  delete ``-wal`` / ``-shm`` under it) and outlive the process: where the service manager
  does not kill the whole process group (a dev server's Ctrl+C), it went on running.

**The child runs this checkout's code.** ``cwd`` and the front of ``PYTHONPATH`` are the root
the parent's own ``portfolio_dash`` was imported from (``Path(portfolio_dash.__file__)
.resolve().parents[1]``). The dev venv's editable install maps ``portfolio_dash`` to the MAIN
checkout through a meta-path finder appended AFTER the normal path finder, so a child started
from a git worktree with this ``cwd`` imports the worktree, as its parent does. Never ``-I``:
isolated mode drops both ``PYTHONPATH`` and the ``cwd`` entry of ``sys.path``.

**Environment.** ``env`` is layered over the parent's ``os.environ`` (never a replacement:
Windows needs ``SYSTEMROOT`` and friends). On top of it the runner sets ``PYTHONPATH`` (above),
``DB_PATH`` to the parent's database as an ABSOLUTE path (a relative one would resolve against
the child's different ``cwd`` and open another file), and ``PYTHONIOENCODING=utf-8`` unless
given (the stderr tail is decoded as UTF-8; a zh-TW Windows console encoding would garble a
Chinese traceback).

**Process group.** ``start_new_session=True`` on POSIX only. Windows ignores the flag, so it is
not passed there.

**Windows behaviour (measured on the dev machine, 2026-10-18):**
- A venv's ``Scripts\\python.exe`` is a launcher that runs the base interpreter as ITS child, so
  :attr:`ChildHandle.pid` is the launcher's pid, not the ``os.getpid()`` the child sees. Killing
  the launcher kills the interpreter (a kill-on-close job object; its file locks were free
  within 3–10 ms), and killing the interpreter by its own pid makes the launcher exit with the
  same code. A child that must be found by pid later (the settle child, spec §9.4) records its
  own ``os.getpid()``.
- ``Popen.terminate()`` / ``kill()`` are both ``TerminateProcess(handle, 1)``: exit code 1, the
  same code an unhandled exception gives. So on Windows "killed" is known only for a kill this
  runner made (:attr:`ChildResult.terminated`); an external ``taskkill`` reads as a non-zero
  exit with whatever code it chose. On POSIX a negative return code names the signal.

**Inside the child** (its ``main()``, not the runner): :func:`lower_own_priority` (``nice 19``
and, on Linux, ``SCHED_IDLE``; a no-op on Windows) and :func:`apply_test_stubs_from_env`
(``PD_TEST_STUBS`` names a test-only module to import before binding anything; refused unless
``PD_TEST_MODE=1``).

``shared/`` imports nothing above itself: the runner knows nothing about settlement or jobs.
"""

from __future__ import annotations

import contextlib
import importlib
import logging
import os
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Literal

from . import proc_lock

log = logging.getLogger(__name__)

#: Bytes of the child's stderr kept for the report (the END of the stream).
DEFAULT_STDERR_TAIL_BYTES = 16 * 1024

#: How often a heavy child waiting for the cross-process lock retries (and re-checks cancel).
_GATE_POLL_S = 0.05

#: How long :func:`stop_all` lets a terminated child exit before it kills it. The children
#: install no SIGTERM handler (a terminate ends them at once; on Windows it IS the kill): this
#: only bounds a child stuck where a signal cannot reach it yet.
STOP_KILL_AFTER_S = 5.0

ChildState = Literal["queued", "running", "exited"]


def package_root() -> Path:
    """The folder that holds the ``portfolio_dash`` package this process imported.

    ``Path(portfolio_dash.__file__).resolve().parents[1]``, computed from this module's own
    file (``<root>/portfolio_dash/shared/child_runner.py``) so ``shared/`` imports nothing
    above itself; a test pins the two as equal.
    """
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ChildResult:
    """What happened to one child. Handed to ``on_exit`` and kept on the handle."""

    module: str
    args: tuple[str, ...]
    heavy: bool
    #: False when the child never ran: cancelled in the queue, or ``Popen`` itself failed.
    started: bool
    #: ``Popen.pid`` (on a Windows venv, the launcher's pid — see the module docstring).
    pid: int | None
    #: The exit code; negative on POSIX when a signal ended it; None when it never ran.
    returncode: int | None
    #: POSIX: the signal that ended the child (``-returncode``), else None.
    signal: int | None
    #: This runner ended it: :meth:`ChildHandle.terminate_and_wait` terminated or killed the
    #: process, or cancelled it while it was still queued.
    terminated: bool
    #: The last bytes of the child's stderr, decoded as UTF-8 (replacement on bad bytes).
    stderr_tail: str
    #: Wall seconds from the process start to its exit (0.0 when it never ran).
    duration_s: float
    #: Wall seconds between :func:`spawn` and the process start (the heavy gate's queue).
    queued_s: float
    #: Why the process could not be started (``Popen`` raised), else None.
    error: str | None = None
    #: The app's stop ended it (:func:`stop_all`): its process was terminated, or it never
    #: started — cancelled in the queue or as its turn came. ``terminated`` is then true too.
    #: Its owner records the run as interrupted by the stop, never as a cancel or a crash.
    stopped: bool = False

    @property
    def ok(self) -> bool:
        """Exited with code 0."""
        return self.returncode == 0

    @property
    def killed(self) -> bool:
        """Ended by a signal (POSIX) or by this runner's terminate / kill / cancel."""
        return self.signal is not None or self.terminated


class _TailBuffer:
    """The last *limit* bytes written to it."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._buf = bytearray()
        self.total = 0

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        self._buf += chunk
        if len(self._buf) > self._limit:
            del self._buf[: len(self._buf) - self._limit]

    def text(self) -> str:
        data = bytes(self._buf)
        if self.total > len(data):
            # The cut may have split a UTF-8 sequence: drop the partial lead bytes.
            while data and (data[0] & 0xC0) == 0x80:
                data = data[1:]
        return data.decode("utf-8", errors="replace")


def _drain(stream: IO[bytes], tail: _TailBuffer) -> None:
    fd = stream.fileno()
    try:
        while chunk := os.read(fd, 65536):
            tail.feed(chunk)
    except (OSError, ValueError):
        pass  # the pipe closed under us (the child was killed): keep what was read
    finally:
        try:
            stream.close()
        except OSError:
            pass


class _HeavyGate:
    """FIFO admission for heavy children: one at a time, in spawn order."""

    def __init__(self, lock_path: Path | None) -> None:
        self._lock_path = lock_path
        self._cond = threading.Condition()
        self._queue: deque[ChildHandle] = deque()
        self._active: ChildHandle | None = None

    def lock_path(self) -> Path:
        return self._lock_path if self._lock_path is not None else proc_lock.heavy_child_lock_path()

    def enqueue(self, handle: ChildHandle) -> None:
        with self._cond:
            self._queue.append(handle)

    def wake(self) -> None:
        with self._cond:
            self._cond.notify_all()

    def queued(self) -> int:
        with self._cond:
            return len(self._queue)

    def discard(self, handle: ChildHandle) -> None:
        """Take *handle* out of the gate wherever it is — its ticket in the queue, or the
        active turn — and wake the next one. A no-op for a handle the gate does not hold.

        The one exit for a ticket whose waiter never ran (:meth:`ChildRunner.spawn` when the
        thread could not start) or broke while waiting (:meth:`enter`): a ticket left at the
        head of the FIFO would wedge every later heavy child — every job and every
        settlement run — for the life of the process (package H review, 2026-10-20)."""
        with self._cond:
            if handle in self._queue:
                self._queue.remove(handle)
            if self._active is handle:
                self._active = None
            self._cond.notify_all()

    def enter(self, handle: ChildHandle) -> proc_lock.LockHandle | None:
        """Wait for *handle*'s turn, then for the cross-process lock.

        Returns the held lock, or ``None`` when the handle was cancelled while waiting (it
        has then left the queue and the turn has passed on). Anything raised while waiting
        for the turn takes the ticket out of the queue first (:meth:`discard`).
        """
        try:
            with self._cond:
                while True:
                    if handle.cancel_requested:
                        self._queue.remove(handle)
                        self._cond.notify_all()
                        return None
                    if self._active is None and self._queue[0] is handle:
                        self._queue.popleft()
                        self._active = handle
                        break
                    self._cond.wait()
        except BaseException:
            self.discard(handle)
            raise
        # In-process turn taken; now the cross-process half. Polled, so a cancel still
        # lands while another process holds the data folder's heavy lock.
        path = self.lock_path()
        try:
            while True:
                held = proc_lock.try_lock(path)
                if held is not None:
                    return held
                if handle.cancel_requested:
                    self.leave(handle)
                    return None
                time.sleep(_GATE_POLL_S)
        except BaseException:
            self.leave(handle)
            raise

    def leave(self, handle: ChildHandle) -> None:
        with self._cond:
            if self._active is handle:
                self._active = None
            self._cond.notify_all()


class ChildHandle:
    """A spawned (or queued) child. Thread-safe."""

    def __init__(self, runner: ChildRunner, module: str, args: tuple[str, ...], heavy: bool,
                 env: dict[str, str], on_exit: Callable[[ChildResult], None] | None,
                 stderr_tail_bytes: int,
                 on_start: Callable[[], None] | None = None) -> None:
        self.module = module
        self.args = args
        self.heavy = heavy
        self._runner = runner
        self._env = env
        self._on_exit = on_exit
        self._on_start = on_start
        self._tail_bytes = stderr_tail_bytes
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._cancel = False
        self._terminated = False
        self._stopped = False              # set only by the app's stop (:func:`stop_all`)
        self._result: ChildResult | None = None
        self._exited = threading.Event()   # the process is gone (or never ran)
        self._done = threading.Event()     # ... and on_exit has returned
        self._spawned_at = time.monotonic()
        self._thread = threading.Thread(
            target=self._run, name=f"child-waiter:{module}", daemon=True)

    # -- state ------------------------------------------------------------------------------
    @property
    def pid(self) -> int | None:
        """``Popen.pid`` once started (a Windows venv: the launcher's pid)."""
        with self._lock:
            return self._proc.pid if self._proc is not None else None

    @property
    def state(self) -> ChildState:
        if self._exited.is_set():
            return "exited"
        with self._lock:
            return "running" if self._proc is not None else "queued"

    @property
    def cancel_requested(self) -> bool:
        with self._lock:
            return self._cancel

    @property
    def result(self) -> ChildResult | None:
        """The result once the process is gone, else None."""
        return self._result

    def wait(self, timeout: float | None = None) -> ChildResult | None:
        """Wait until the process is gone and ``on_exit`` has returned; None on timeout."""
        if threading.current_thread() is self._thread:
            # Called from inside on_exit: the process is gone, the callback is the caller.
            return self._result
        if not self._done.wait(timeout):
            return None
        return self._result

    # -- control ----------------------------------------------------------------------------
    def terminate_and_wait(self, timeout: float) -> ChildResult:
        """Terminate, wait *timeout*, kill if still alive, wait again; cancel if queued.

        Returns once the process is gone and ``on_exit`` has returned. Raises
        ``TimeoutError`` only if the process survives even the kill (a process stuck in the
        kernel). Calling it on an exited child just returns its result.
        """
        with self._lock:
            self._cancel = True
            proc = self._proc
        if proc is None:
            self._runner.gate.wake()          # a queued child leaves the queue
        else:
            self._stop(proc, timeout)
        if threading.current_thread() is self._thread:
            # on_exit itself asked: the process is gone; do not wait for ourselves.
            if not self._exited.wait(max(timeout, 1.0)):
                raise TimeoutError(f"child {self.module} still running after terminate")
            assert self._result is not None
            return self._result
        self._done.wait()
        assert self._result is not None
        return self._result

    def _stop(self, proc: subprocess.Popen[bytes], timeout: float) -> None:
        if proc.poll() is not None:
            return
        with self._lock:
            self._terminated = True
        try:
            proc.terminate()
        except OSError:
            pass  # it exited between poll() and terminate()
        try:
            proc.wait(timeout)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(max(timeout, 5.0))
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                f"child {self.module} (pid {proc.pid}) survived kill for {max(timeout, 5.0)} s"
            ) from exc

    def _request_stop(self) -> subprocess.Popen[bytes] | None:
        """:func:`stop_all`'s first step for this child: cancel it — a queued child, or one
        whose turn is coming, never starts its process — and return the process to terminate
        when one is running. A child that has started and already exited keeps its own result
        (not stopped); one that never started is recorded stopped."""
        with self._lock:
            self._cancel = True
            proc = self._proc
            if proc is None:
                self._stopped = True
                return None
            if proc.poll() is not None:
                return None
            self._stopped = True
            self._terminated = True
            return proc

    # -- the waiter thread ------------------------------------------------------------------
    def _start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        gate_lock: proc_lock.LockHandle | None = None
        entered = False
        try:
            if self.heavy:
                gate_lock = self._runner.gate.enter(self)
                if gate_lock is None:
                    self._finish(self._never_ran(error=None, terminated=True))
                    return
                entered = True
            if self.cancel_requested:  # cancelled as its turn came: no on_start, no process
                self._finish(self._never_ran(error=None, terminated=True))
                return
            self._call_on_start()
            self._run_process()
        except BaseException as exc:   # on_exit must still fire (e.g. the lock folder is gone)
            log.exception("child runner: waiter for %s failed", self.module)
            if self._result is None:
                self._finish(self._never_ran(error=f"{type(exc).__name__}: {exc}",
                                             terminated=False))
        finally:
            if gate_lock is not None:
                gate_lock.release()
            if entered:
                self._runner.gate.leave(self)
            self._exited.set()
            self._call_on_exit()

    def _call_on_start(self) -> None:
        """The turn has come (the gate is held for a heavy child) and no cancel was recorded;
        the process is not started yet — and a cancel landing from here on still keeps it
        from starting (:meth:`_run_process` checks again). Never raises: an observer's
        failure does not stop the child."""
        if self._on_start is None:
            return
        try:
            self._on_start()
        except Exception:
            log.warning("child runner: on_start for %s raised", self.module, exc_info=True)

    def _run_process(self) -> None:
        cmd = [sys.executable, "-m", self.module, *self.args]
        queued_s = time.monotonic() - self._spawned_at
        with self._lock:
            if self._cancel:
                self._result = self._never_ran(error=None, terminated=True)
                return
            try:
                proc = _popen(cmd, self._env)
            except OSError as exc:
                self._result = self._never_ran(error=f"{type(exc).__name__}: {exc}",
                                               terminated=False)
                return
            self._proc = proc
        started = time.monotonic()
        tail = _TailBuffer(self._tail_bytes)
        assert proc.stderr is not None
        reader = threading.Thread(target=_drain, args=(proc.stderr, tail),
                                  name=f"child-stderr:{self.module}", daemon=True)
        reader.start()
        returncode = proc.wait()
        duration = time.monotonic() - started
        reader.join(timeout=5.0)
        with self._lock:
            terminated = self._terminated
            stopped = self._stopped
        self._result = ChildResult(
            module=self.module, args=self.args, heavy=self.heavy, started=True,
            pid=proc.pid, returncode=returncode,
            signal=-returncode if returncode < 0 else None,
            terminated=terminated, stderr_tail=tail.text(), duration_s=duration,
            queued_s=queued_s, stopped=stopped,
        )
        if returncode != 0 and not terminated:
            log.warning("child %s (pid %s) exited with %s; stderr tail:\n%s",
                        self.module, proc.pid, returncode, self._result.stderr_tail[-2000:])

    def _never_ran(self, *, error: str | None, terminated: bool) -> ChildResult:
        # Read without the lock (some callers hold it): a stop sets ``_stopped`` together with
        # ``_cancel``, so by the time a cancel is seen here the flag is too.
        return ChildResult(
            module=self.module, args=self.args, heavy=self.heavy, started=False, pid=None,
            returncode=None, signal=None, terminated=terminated, stderr_tail="",
            duration_s=0.0, queued_s=time.monotonic() - self._spawned_at, error=error,
            stopped=terminated and self._stopped,
        )

    def _finish(self, result: ChildResult) -> None:
        if self._result is None:
            self._result = result

    def _call_on_exit(self) -> None:
        try:
            if self._on_exit is not None and self._result is not None:
                self._on_exit(self._result)
        except Exception:
            log.exception("child runner: on_exit for %s raised", self.module)
        finally:
            self._done.set()
            self._runner._forget(self)   # after the event: stop_all never misses a callback

    def __repr__(self) -> str:
        return f"ChildHandle({self.module!r}, heavy={self.heavy}, state={self.state!r})"


class ChildRunner:
    """A runner with its own heavy gate. The app uses the process-wide one (:func:`spawn`);
    tests build their own with a private *heavy_lock_path*."""

    def __init__(self, *, heavy_lock_path: Path | None = None,
                 stderr_tail_bytes: int = DEFAULT_STDERR_TAIL_BYTES) -> None:
        self.gate = _HeavyGate(heavy_lock_path)
        self._tail_bytes = stderr_tail_bytes
        #: Every child spawned and not yet done (its ``on_exit`` has not returned) — what
        #: :meth:`stop_all` stops. The gate alone does not know a child whose process has
        #: exited while its ``on_exit`` still writes, nor a non-heavy one.
        self._live: set[ChildHandle] = set()
        self._live_lock = threading.Lock()

    def spawn(self, module: str, args: Sequence[str] = (), *,
              env: Mapping[str, str] | None = None,
              on_exit: Callable[[ChildResult], None] | None = None,
              heavy: bool = False,
              on_start: Callable[[], None] | None = None) -> ChildHandle:
        """Start ``python -m <module> <args>`` (queued behind the gate when *heavy*)."""
        handle = ChildHandle(self, module, tuple(args), heavy, child_env(env), on_exit,
                             self._tail_bytes, on_start)
        with self._live_lock:
            self._live.add(handle)
        if heavy:
            self.gate.enqueue(handle)       # the ticket is taken HERE, so FIFO = spawn order
        try:
            handle._start()
        except BaseException:
            # The waiter never ran (``RuntimeError: can't start new thread`` under memory
            # pressure): nobody will ever take this ticket's turn, so it leaves the gate
            # before the caller hears of the failure.
            if heavy:
                self.gate.discard(handle)
            self._forget(handle)
            raise
        return handle

    def _forget(self, handle: ChildHandle) -> None:
        with self._live_lock:
            self._live.discard(handle)

    def stop_all(self, timeout: float) -> list[ChildHandle]:
        """Stop every child of this runner that is still alive — the app is stopping — and
        wait for each until its process is gone and its ``on_exit`` has returned, *timeout*
        seconds at most in all. Returns the children not done by then (logged).

        1. Every child is cancelled FIRST, all of them, so none that is queued starts when the
           gate passes the turn on, nor one whose turn is coming (its ``on_start`` may still
           run; its process never starts).
        2. Every running process is terminated, and killed if it is still alive
           :data:`STOP_KILL_AFTER_S` later.
        3. Each child's ``on_exit`` receives a result with ``stopped`` (and ``terminated``)
           set — except a child whose process had already exited on its own, whose result
           stands.

        A child spawned after the call began is not stopped (the lifespan calls this after
        the scheduler and the settlement service stopped starting new work). Never raises."""
        deadline = time.monotonic() + timeout
        with self._live_lock:
            handles = list(self._live)
        if not handles:
            return []
        procs = [proc for proc in (h._request_stop() for h in handles) if proc is not None]
        self.gate.wake()                    # a queued child sees its cancel and leaves
        log.info("child runner: stopping %d child(ren), %d running", len(handles), len(procs))
        for proc in procs:
            with contextlib.suppress(OSError):
                proc.terminate()            # it may have exited since the poll
        kill_at = min(deadline, time.monotonic() + STOP_KILL_AFTER_S)
        for proc in procs:
            try:
                proc.wait(max(kill_at - time.monotonic(), 0.0))
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    proc.kill()
        left = [h for h in handles
                if not h._done.wait(max(deadline - time.monotonic(), 0.0))]
        if left:
            log.warning("child runner: %d child(ren) not done %.0f s into the stop: %s",
                        len(left), timeout, ", ".join(repr(h) for h in left))
        return left


def _popen(cmd: list[str], env: dict[str, str]) -> subprocess.Popen[bytes]:
    common_cwd = str(package_root())
    if sys.platform == "win32":
        return subprocess.Popen(cmd, cwd=common_cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return subprocess.Popen(cmd, cwd=common_cwd, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            start_new_session=True)


def child_env(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment a child gets: ``os.environ`` + *overrides* + the runner's own keys."""
    env = dict(os.environ)
    if overrides:
        env.update(overrides)
    root = str(package_root())
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = root if not existing else root + os.pathsep + existing
    if env.get("DB_PATH"):
        db_path = Path(env["DB_PATH"])
    else:
        # Lazy, like proc_lock.data_dir: a child that only calls lower_own_priority or
        # apply_test_stubs_from_env must not pay for pydantic-settings.
        from .config import get_settings

        db_path = get_settings().db_path
    env["DB_PATH"] = str(db_path.resolve())
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


_default_runner = ChildRunner()


def default_runner() -> ChildRunner:
    """The process-wide runner, whose gate every heavy child of this process shares."""
    return _default_runner


def spawn(module: str, args: Sequence[str] = (), *, env: Mapping[str, str] | None = None,
          on_exit: Callable[[ChildResult], None] | None = None,
          heavy: bool = False,
          on_start: Callable[[], None] | None = None) -> ChildHandle:
    """Start ``python -m <module> <args>`` on the process-wide runner (see the module doc)."""
    return _default_runner.spawn(module, args, env=env, on_exit=on_exit, heavy=heavy,
                                 on_start=on_start)


def stop_all(timeout: float) -> list[ChildHandle]:
    """:meth:`ChildRunner.stop_all` on the process-wide runner: the app's stop."""
    return _default_runner.stop_all(timeout)


# -- called inside a child's main() ----------------------------------------------------------
def lower_own_priority() -> tuple[str, ...]:
    """Lower this process's CPU priority: ``nice 19``, and ``SCHED_IDLE`` where it exists.

    For a child's own ``main()`` (spec §9.2) — the runner never renices. A no-op on Windows,
    where ``os.nice`` does not exist. Returns what it applied, for the run's log.
    """
    if sys.platform == "win32":
        return ()
    applied: list[str] = []
    try:
        os.nice(19)        # an increment; the kernel clamps the result at 19
        applied.append("nice")
    except OSError:
        pass
    if sys.platform == "linux" and hasattr(os, "SCHED_IDLE"):
        try:
            os.sched_setscheduler(0, os.SCHED_IDLE, os.sched_param(0))
            applied.append("SCHED_IDLE")
        except OSError:
            pass
    return tuple(applied)


def apply_test_stubs_from_env() -> str | None:
    """Import the test-stub module named by ``PD_TEST_STUBS`` (it patches on import).

    For a child's ``main()``, called before it binds anything. Returns the module name, or
    None when the variable is unset. Raises ``RuntimeError`` unless ``PD_TEST_MODE=1`` is
    also set, so a stray variable in a production unit can never load test code.
    """
    name = os.environ.get("PD_TEST_STUBS")
    if not name:
        return None
    if os.environ.get("PD_TEST_MODE") != "1":
        raise RuntimeError(
            f"PD_TEST_STUBS={name!r} is set but PD_TEST_MODE is not '1'; "
            "refusing to load test stubs outside a test run"
        )
    importlib.import_module(name)
    return name
