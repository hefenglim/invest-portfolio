"""``shared/child_runner.py`` (PERF plan P6, W0.7): spawn, the FIFO heavy gate, exit callbacks,
terminate-and-wait, the worktree case and the test-stub door.

Every child is a real ``python -m tests.shared.child_fixtures.<name>`` process. Each test
builds its own :class:`ChildRunner` with a private heavy lock, so no test shares a gate with
another or with the process-wide runner.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

import portfolio_dash
from portfolio_dash.shared import child_runner, proc_lock
from portfolio_dash.shared.child_runner import ChildHandle, ChildResult, ChildRunner
from portfolio_dash.shared.config import get_settings
from tests.shared.child_fixtures import stub_target
from tests.shared.child_fixtures.noisy import END_MARKER

_FX = "tests.shared.child_fixtures"
_WAIT = 20.0          # generous upper bound for one child's life on a loaded machine


class Recorder:
    """An ``on_exit`` that records every call (and the thread it ran on)."""

    def __init__(self) -> None:
        self.calls: list[ChildResult] = []
        self.threads: list[str] = []
        self.fired = threading.Event()

    def __call__(self, result: ChildResult) -> None:
        self.calls.append(result)
        self.threads.append(threading.current_thread().name)
        self.fired.set()


def _wait_for(predicate: Callable[[], bool], timeout: float = _WAIT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _read_json(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


@pytest.fixture
def runner(tmp_path: Path) -> Iterator[ChildRunner]:
    handles: list[ChildHandle] = []
    r = ChildRunner(heavy_lock_path=tmp_path / "heavy-child.lock")
    real_spawn = r.spawn

    def _tracked(*a: Any, **k: Any) -> ChildHandle:
        h = real_spawn(*a, **k)
        handles.append(h)
        return h

    r.spawn = _tracked  # type: ignore[method-assign]
    yield r
    for h in handles:                       # never leave a child behind
        if h.state != "exited":
            h.terminate_and_wait(5.0)


def _done(handle: ChildHandle) -> ChildResult:
    result = handle.wait(_WAIT)
    assert result is not None, f"{handle!r} did not finish in {_WAIT} s"
    return result


# -- exits ---------------------------------------------------------------------------------
def test_a_child_that_exits_0(runner: ChildRunner) -> None:
    rec = Recorder()
    handle = runner.spawn(f"{_FX}.exit_code", ["0"], on_exit=rec)
    result = _done(handle)
    assert rec.calls == [result] and handle.result is result
    assert result.ok and result.started and result.returncode == 0
    assert result.signal is None and not result.terminated and not result.killed
    assert result.error is None and isinstance(result.pid, int)
    assert result.module == f"{_FX}.exit_code" and result.args == ("0",)
    assert result.duration_s > 0 and result.queued_s >= 0
    assert "bye with 0" in result.stderr_tail
    assert handle.state == "exited"
    assert rec.threads[0].startswith("child-waiter:")     # on the waiter thread, not ours


def test_a_non_zero_exit_code_is_reported(runner: ChildRunner) -> None:
    result = _done(runner.spawn(f"{_FX}.exit_code", ["7"]))
    assert result.returncode == 7 and not result.ok and not result.killed


def test_a_child_that_raises(runner: ChildRunner) -> None:
    rec = Recorder()
    result = _done(runner.spawn(f"{_FX}.raises", ["boom-marker 失敗"], on_exit=rec))
    assert rec.calls == [result]
    assert result.started and result.returncode == 1 and not result.killed
    assert "Traceback" in result.stderr_tail
    assert "RuntimeError: boom-marker 失敗" in result.stderr_tail   # UTF-8 across the pipe


def test_a_child_killed_from_outside(runner: ChildRunner, tmp_path: Path) -> None:
    """The waiter sees an external kill like any other exit, and the callback fires."""
    ready = tmp_path / "ready.json"
    rec = Recorder()
    handle = runner.spawn(f"{_FX}.sleeper", [str(ready)], on_exit=rec)
    assert _wait_for(ready.exists)
    assert handle.state == "running"
    assert handle.pid is not None
    os.kill(handle.pid, signal.SIGTERM)
    result = _done(handle)
    assert rec.calls == [result]
    assert result.started and not result.terminated      # not this runner's doing
    if sys.platform == "win32":
        # os.kill is TerminateProcess(handle, 15): just an exit code, no signal.
        assert result.returncode == signal.SIGTERM and result.signal is None
    else:
        assert result.returncode == -signal.SIGTERM and result.signal == signal.SIGTERM
        assert result.killed


def test_terminate_and_wait_on_a_running_child(runner: ChildRunner, tmp_path: Path) -> None:
    ready = tmp_path / "ready.json"
    rec = Recorder()
    handle = runner.spawn(f"{_FX}.sleeper", [str(ready)], on_exit=rec)
    assert _wait_for(ready.exists)
    result = handle.terminate_and_wait(5.0)
    # It returns only after the process is gone AND on_exit has run.
    assert rec.calls == [result] and handle.state == "exited"
    assert result.started and result.terminated and result.killed and not result.ok
    assert handle.terminate_and_wait(5.0) is result      # again: just the result
    assert rec.calls == [result]                         # on_exit ran exactly once


def test_terminate_escalates_to_kill(runner: ChildRunner, tmp_path: Path,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """terminate, wait, kill if still alive, wait: a child that ignores the terminate."""
    ready = tmp_path / "ready.json"
    handle = runner.spawn(f"{_FX}.sleeper", [str(ready)])
    assert _wait_for(ready.exists)
    kills: list[int] = []
    real_kill = subprocess.Popen.kill

    def _kill(self: subprocess.Popen[Any]) -> None:
        kills.append(self.pid)
        real_kill(self)

    monkeypatch.setattr(subprocess.Popen, "terminate", lambda self: None)
    monkeypatch.setattr(subprocess.Popen, "kill", _kill)
    started = time.monotonic()
    result = handle.terminate_and_wait(0.3)
    assert kills == [handle.pid]
    assert time.monotonic() - started >= 0.25   # waited the timeout (Windows clock ~16 ms)
    assert result.terminated and result.started


def test_popen_failure_still_fires_on_exit(runner: ChildRunner,
                                           monkeypatch: pytest.MonkeyPatch,
                                           tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "executable", str(tmp_path / "no-such-python.exe"))
    rec = Recorder()
    result = _done(runner.spawn(f"{_FX}.exit_code", ["0"], on_exit=rec))
    assert rec.calls == [result]
    assert not result.started and result.returncode is None and result.pid is None
    assert result.error is not None and "FileNotFoundError" in result.error


def test_the_stderr_tail_is_bounded(tmp_path: Path) -> None:
    runner = ChildRunner(heavy_lock_path=tmp_path / "h.lock", stderr_tail_bytes=1024)
    result = _done(runner.spawn(f"{_FX}.noisy", [str(200_000)]))
    assert result.returncode == 3
    assert len(result.stderr_tail.encode("utf-8")) <= 1024
    assert result.stderr_tail.rstrip().endswith(END_MARKER)


def test_a_callback_that_raises_is_logged_and_does_not_jam_the_gate(
        runner: ChildRunner, caplog: pytest.LogCaptureFixture) -> None:
    def _bad(_: ChildResult) -> None:
        raise RuntimeError("callback bug")

    caplog.set_level(logging.ERROR, logger="portfolio_dash.shared.child_runner")
    first = runner.spawn(f"{_FX}.exit_code", ["0"], on_exit=_bad, heavy=True)
    second = runner.spawn(f"{_FX}.exit_code", ["0"], heavy=True)
    assert _done(first).ok
    assert _done(second).ok
    assert any("on_exit" in r.getMessage() for r in caplog.records)


# -- the heavy gate --------------------------------------------------------------------------
def test_heavy_children_run_one_at_a_time_in_fifo_order(runner: ChildRunner,
                                                         tmp_path: Path) -> None:
    lock = tmp_path / "heavy-child.lock"
    outs = [tmp_path / f"probe{i}.json" for i in range(3)]
    handles = [runner.spawn(f"{_FX}.heavy_probe", [str(out), str(lock), "0.25"], heavy=True)
               for out in outs]
    assert runner.gate.queued() >= 1                      # the later ones wait their turn
    results = [_done(h) for h in handles]
    assert all(r.ok for r in results), [r.stderr_tail for r in results]
    spans = [_read_json(out) for out in outs]
    for span in spans:
        assert span["held_at_start"] and span["held_at_end"]   # gate lock held while it lived
    for earlier, later in zip(spans, spans[1:], strict=False):
        assert earlier["end_ns"] <= later["start_ns"]           # never overlapping, in order
    assert results[2].queued_s > results[0].queued_s
    assert not proc_lock.is_held(lock)                          # released after the last
    assert runner.gate.queued() == 0


def test_a_non_heavy_child_never_waits_for_the_gate(runner: ChildRunner,
                                                    tmp_path: Path) -> None:
    ready = tmp_path / "ready.json"
    heavy = runner.spawn(f"{_FX}.sleeper", [str(ready)], heavy=True)
    assert _wait_for(ready.exists)
    light = runner.spawn(f"{_FX}.exit_code", ["0"])
    assert _done(light).ok
    assert heavy.state == "running"


def test_a_heavy_child_waits_for_the_cross_process_lock(runner: ChildRunner,
                                                        tmp_path: Path) -> None:
    """The file half of the gate: another holder of heavy-child.lock (here a second handle,
    which conflicts exactly as another process does) keeps the queue waiting."""
    other = proc_lock.try_lock(tmp_path / "heavy-child.lock")
    assert other is not None
    try:
        handle = runner.spawn(f"{_FX}.exit_code", ["0"], heavy=True)
        time.sleep(0.4)
        assert handle.state == "queued" and handle.result is None and handle.pid is None
    finally:
        other.release()
    result = _done(handle)
    assert result.ok and result.queued_s >= 0.35


def test_cancelling_a_queued_heavy_child(runner: ChildRunner, tmp_path: Path) -> None:
    ready = tmp_path / "ready.json"
    first = runner.spawn(f"{_FX}.sleeper", [str(ready)], heavy=True)
    assert _wait_for(ready.exists)
    rec = Recorder()
    second = runner.spawn(f"{_FX}.exit_code", ["0"], heavy=True, on_exit=rec)
    third = runner.spawn(f"{_FX}.exit_code", ["0"], heavy=True)
    assert second.state == "queued"
    result = second.terminate_and_wait(5.0)
    assert rec.calls == [result]
    assert not result.started and result.terminated and result.pid is None
    assert runner.gate.queued() == 1                       # only the third is left
    first.terminate_and_wait(5.0)
    assert _done(third).ok                                 # the queue moved on past it


class StartRecorder:
    """An ``on_start`` that records, at the moment it runs, whether the heavy gate's lock was
    held and whether the child's process existed yet."""

    def __init__(self, lock: Path) -> None:
        self.lock = lock
        self.handle: ChildHandle | None = None
        self.calls: list[tuple[bool, int | None, str]] = []

    def __call__(self) -> None:
        assert self.handle is not None
        self.calls.append((proc_lock.is_held(self.lock), self.handle.pid,
                           threading.current_thread().name))


def test_on_start_runs_once_the_gate_grants_the_turn_and_before_the_process(
        runner: ChildRunner, tmp_path: Path) -> None:
    """A heavy child's ``on_start`` waits with it: not while another holder keeps the gate,
    then once its turn comes — the gate held for it, no process started yet (the job
    dispatcher marks the change trail there, code review 2026-10-20)."""
    lock = tmp_path / "heavy-child.lock"
    other = proc_lock.try_lock(lock)
    assert other is not None
    rec = StartRecorder(lock)
    try:
        rec.handle = runner.spawn(f"{_FX}.exit_code", ["0"], heavy=True, on_start=rec)
        time.sleep(0.4)
        assert rec.handle.state == "queued" and rec.calls == []
    finally:
        other.release()
    assert _done(rec.handle).ok
    assert len(rec.calls) == 1
    held, pid, thread = rec.calls[0]
    assert held and pid is None and thread.startswith("child-waiter:")


def test_on_start_is_never_called_for_a_child_cancelled_in_the_queue(
        runner: ChildRunner, tmp_path: Path) -> None:
    ready = tmp_path / "ready.json"
    first = runner.spawn(f"{_FX}.sleeper", [str(ready)], heavy=True)
    assert _wait_for(ready.exists)
    rec = StartRecorder(tmp_path / "heavy-child.lock")
    rec.handle = runner.spawn(f"{_FX}.exit_code", ["0"], heavy=True, on_start=rec)
    result = rec.handle.terminate_and_wait(5.0)
    first.terminate_and_wait(5.0)
    assert not result.started and rec.calls == []


def _spawn_behind_the_gate(runner: ChildRunner, tmp_path: Path, rec: StartRecorder,
                           **kw: Any) -> ChildHandle:
    """A heavy ``exit_code 0`` spawned while another holder keeps the gate's lock, released
    only once *rec* holds the handle — so it is set before the child's turn can come."""
    other = proc_lock.try_lock(tmp_path / "heavy-child.lock")
    assert other is not None
    try:
        rec.handle = runner.spawn(f"{_FX}.exit_code", ["0"], heavy=True, **kw)
        return rec.handle
    finally:
        other.release()


def _cancel_from_another_thread(handle: ChildHandle) -> None:
    """``terminate_and_wait`` as another thread would call it, returning once the cancel is
    recorded (the call itself keeps waiting for the child's end on its own thread)."""
    threading.Thread(target=handle.terminate_and_wait, args=(5.0,), daemon=True).start()
    assert _wait_for(lambda: handle.cancel_requested)


def test_a_cancel_as_the_turn_comes_skips_on_start(runner: ChildRunner, tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """A cancel recorded after the gate granted the turn but before ``on_start``: neither
    ``on_start`` nor the process runs (the turn passes on), and ``on_exit`` still fires."""
    real_enter = runner.gate.enter

    def enter_then_cancel(handle: ChildHandle) -> proc_lock.LockHandle | None:
        held = real_enter(handle)
        _cancel_from_another_thread(handle)
        return held

    monkeypatch.setattr(runner.gate, "enter", enter_then_cancel)
    rec, exits = StartRecorder(tmp_path / "heavy-child.lock"), Recorder()
    result = _done(_spawn_behind_the_gate(runner, tmp_path, rec, on_start=rec, on_exit=exits))
    assert not result.started and result.terminated
    assert rec.calls == [] and exits.calls == [result]
    monkeypatch.setattr(runner.gate, "enter", real_enter)
    assert _done(runner.spawn(f"{_FX}.exit_code", ["0"], heavy=True)).ok   # the gate is free


def test_a_cancel_landing_during_on_start_still_stops_the_process(
        runner: ChildRunner, tmp_path: Path) -> None:
    """``on_start`` ran, the process did not: a caller pairs ``on_start`` with ``on_exit``,
    which fires either way, so a bracket opened in ``on_start`` is always closed."""
    rec, exits = StartRecorder(tmp_path / "heavy-child.lock"), Recorder()

    def on_start() -> None:
        rec()
        assert rec.handle is not None
        _cancel_from_another_thread(rec.handle)

    result = _done(_spawn_behind_the_gate(runner, tmp_path, rec, on_start=on_start,
                                          on_exit=exits))
    assert len(rec.calls) == 1 and exits.calls == [result]
    assert not result.started and result.terminated and result.pid is None


def test_an_on_start_that_raises_is_logged_and_the_child_still_runs(
        runner: ChildRunner, caplog: pytest.LogCaptureFixture) -> None:
    def _bad() -> None:
        raise RuntimeError("on_start bug")

    caplog.set_level(logging.WARNING, logger="portfolio_dash.shared.child_runner")
    assert _done(runner.spawn(f"{_FX}.exit_code", ["0"], heavy=True, on_start=_bad)).ok
    assert any("on_start" in r.getMessage() for r in caplog.records)


def test_a_broken_gate_lock_still_reports(tmp_path: Path) -> None:
    """The lock folder is the caller's job; if it is missing, on_exit still fires."""
    runner = ChildRunner(heavy_lock_path=tmp_path / "missing-dir" / "heavy-child.lock")
    rec = Recorder()
    result = _done(runner.spawn(f"{_FX}.exit_code", ["0"], heavy=True, on_exit=rec))
    assert rec.calls == [result]
    assert not result.started and result.error is not None
    assert _done(runner.spawn(f"{_FX}.exit_code", ["0"], heavy=True)).error is not None


# -- which code, which environment -----------------------------------------------------------
def test_the_package_root_is_the_imported_package(tmp_path: Path) -> None:
    root = Path(portfolio_dash.__file__).resolve().parents[1]
    assert child_runner.package_root() == root


def test_a_child_runs_this_checkouts_code(runner: ChildRunner, tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """The worktree case: the editable install maps portfolio_dash to the main checkout, yet
    a child spawned from a worktree must import the worktree, exactly as its parent did."""
    monkeypatch.setenv("PD_PARENT_ONLY", "from-parent")
    out = tmp_path / "who.json"
    result = _done(runner.spawn(f"{_FX}.whoami", [str(out), "PD_PARENT_ONLY", "PD_EXTRA"],
                                env={"PD_EXTRA": "from-override"}))
    assert result.ok, result.stderr_tail
    who = _read_json(out)
    parent_file = Path(portfolio_dash.__file__).resolve()
    assert Path(who["package_file"]).resolve() == parent_file
    root = child_runner.package_root()
    assert Path(who["cwd"]).resolve() == root
    assert who["flags"]["isolated"] == 0                       # never -I
    env = who["env"]
    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(root)
    assert Path(env["DB_PATH"]).is_absolute()
    assert Path(env["DB_PATH"]) == get_settings().db_path.resolve()
    # utf-8 unless the parent already chose an encoding (then the parent's choice stands).
    assert env["PYTHONIOENCODING"] == os.environ.get("PYTHONIOENCODING", "utf-8")
    assert env["PD_PARENT_ONLY"] == "from-parent"              # layered over os.environ
    assert env["PD_EXTRA"] == "from-override"


def test_a_relative_db_path_reaches_the_child_absolute(tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """The child's cwd differs from the parent's, so a relative DB_PATH would open a
    different file there."""
    monkeypatch.chdir(tmp_path)
    env = child_runner.child_env({"DB_PATH": "rel/portfolio.db"})
    assert Path(env["DB_PATH"]) == (tmp_path / "rel" / "portfolio.db").resolve()
    keep = child_runner.child_env({"PYTHONIOENCODING": "latin-1", "PYTHONPATH": "extra"})
    assert keep["PYTHONIOENCODING"] == "latin-1"
    assert keep["PYTHONPATH"] == str(child_runner.package_root()) + os.pathsep + "extra"


# -- inside the child ------------------------------------------------------------------------
def test_lower_own_priority_in_a_child(runner: ChildRunner, tmp_path: Path) -> None:
    out = tmp_path / "nice.json"
    result = _done(runner.spawn(f"{_FX}.renice", [str(out)]))
    assert result.ok, result.stderr_tail
    report = _read_json(out)
    if sys.platform == "win32":
        assert report["applied"] == []                   # os.nice does not exist here
        assert not hasattr(os, "nice")
    else:
        assert "nice" in report["applied"] and report["niceness"] == 19
        if sys.platform == "linux" and report["sched_idle"] is not None:
            assert report["policy"] == report["sched_idle"]


def test_apply_test_stubs_in_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PD_TEST_STUBS", raising=False)
    monkeypatch.delenv("PD_TEST_MODE", raising=False)
    assert child_runner.apply_test_stubs_from_env() is None
    monkeypatch.setenv("PD_TEST_STUBS", f"{_FX}.stub_module")
    with pytest.raises(RuntimeError, match="PD_TEST_MODE"):
        child_runner.apply_test_stubs_from_env()
    monkeypatch.setenv("PD_TEST_MODE", "0")
    with pytest.raises(RuntimeError, match="PD_TEST_MODE"):
        child_runner.apply_test_stubs_from_env()
    assert f"{_FX}.stub_module" not in sys.modules        # refused BEFORE any import


def test_apply_test_stubs_across_the_process_boundary(runner: ChildRunner,
                                                      tmp_path: Path) -> None:
    out = tmp_path / "stub.json"
    stubs = {"PD_TEST_STUBS": f"{_FX}.stub_module"}
    result = _done(runner.spawn(f"{_FX}.stub_entry", [str(out)],
                                env={**stubs, "PD_TEST_MODE": "1"}))
    assert result.ok, result.stderr_tail
    assert _read_json(out) == {"applied": f"{_FX}.stub_module", "value": "stubbed"}
    assert stub_target.VALUE == "real"                    # the parent was never patched

    refused = tmp_path / "refused.json"
    env = {**stubs, "PD_TEST_MODE": ""}
    result = _done(runner.spawn(f"{_FX}.stub_entry", [str(refused)], env=env))
    assert result.returncode == 1 and not refused.exists()
    assert "refusing to load test stubs" in result.stderr_tail


# -- layering --------------------------------------------------------------------------------
@pytest.mark.parametrize("module", ["flags", "proc_lock", "child_runner"])
def test_the_new_shared_modules_import_nothing_above_shared(module: str) -> None:
    """``shared/`` is L0 (architecture.md): only ``shared`` siblings and the stdlib."""
    path = Path(child_runner.__file__).with_name(f"{module}.py")
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                assert node.level == 1, f"{module}: relative import above shared"
            else:
                name = node.module or ""
                assert not name.startswith("portfolio_dash") or \
                    name.startswith("portfolio_dash.shared"), f"{module}: imports {name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("portfolio_dash") or \
                    alias.name.startswith("portfolio_dash.shared"), \
                    f"{module}: imports {alias.name}"
