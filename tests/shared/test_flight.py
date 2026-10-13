"""`shared/flight.py`: concurrent identical computations run once (2026-10-13).

On the prod VM six page loads in a row left six identical dashboard builds running side by
side for 20+ minutes. These pin the sharing and every guarantee the module docstring makes.
"""

import threading
import time
from collections.abc import Callable, Iterator

import pytest

from portfolio_dash.shared import flight
from portfolio_dash.shared.flight import (
    begin_write,
    coalesce,
    end_write,
    note_write,
    write_generation,
)

KEY = ("test", 1)


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    yield
    assert flight._flights == {}, "a flight outlived its computation"
    assert flight._writes_in_progress == 0


def _wait_until(pred: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 5
    while not pred():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.005)


def _joiners() -> int:
    with flight._lock:
        f = flight._flights.get(KEY)
        return -1 if f is None else f.joiners


class _Gate:
    """A computation that runs until released, counting its runs."""

    def __init__(self, result: object = None, error: Exception | None = None) -> None:
        self.release = threading.Event()
        self.runs = 0
        self.result = result if result is not None else {"value": [1, 2, 3]}
        self.error = error

    def __call__(self) -> object:
        self.runs += 1
        assert self.release.wait(5)
        if self.error is not None:
            raise self.error
        return self.result


def _deep(d: object) -> object:
    return {k: list(v) for k, v in d.items()} if isinstance(d, dict) else d


def _run(target: Callable[[], object], out: dict[str, object], name: str) -> threading.Thread:
    def go() -> None:
        try:
            out[name] = target()
        except Exception as exc:  # noqa: BLE001 — recorded for the assertion
            out[name] = exc
    th = threading.Thread(target=go)
    th.start()
    return th


def _leader_and_joiner(gate: _Gate, copy: Callable[[object], object] = _deep
                       ) -> dict[str, object]:
    out: dict[str, object] = {}
    lead = _run(lambda: coalesce(KEY, gate, copy=copy), out, "leader")
    _wait_until(lambda: _joiners() == 0)
    join = _run(lambda: coalesce(KEY, gate, copy=copy), out, "joiner")
    _wait_until(lambda: _joiners() == 1)
    gate.release.set()
    lead.join(5)
    join.join(5)
    return out


def test_no_key_always_computes() -> None:
    runs = [0]

    def compute() -> int:
        runs[0] += 1
        return runs[0]

    assert coalesce(None, compute, copy=lambda x: x) == 1
    assert coalesce(None, compute, copy=lambda x: x) == 2


def test_a_concurrent_identical_request_shares_the_run_and_owns_its_copy() -> None:
    gate = _Gate()
    out = _leader_and_joiner(gate)
    assert gate.runs == 1
    assert out["leader"] is gate.result
    assert out["joiner"] == gate.result
    assert out["joiner"] is not gate.result
    assert out["joiner"]["value"] is not gate.result["value"]   # type: ignore[index]


def test_nothing_is_kept_after_the_run() -> None:
    gate = _Gate()
    gate.release.set()
    coalesce(KEY, gate, copy=_deep)
    coalesce(KEY, gate, copy=_deep)
    assert gate.runs == 2


def test_an_error_reaches_every_joiner_and_runs_once() -> None:
    gate = _Gate(error=ValueError("帳本無法重放"))
    out = _leader_and_joiner(gate)
    assert gate.runs == 1
    assert isinstance(out["leader"], ValueError)
    assert isinstance(out["joiner"], ValueError)


def test_a_failed_copy_fails_the_joiner_not_the_leader() -> None:
    def bad_copy(_: object) -> object:
        raise RuntimeError("copy failed")

    gate = _Gate()
    out = _leader_and_joiner(gate, copy=bad_copy)
    assert out["leader"] is gate.result
    assert isinstance(out["joiner"], RuntimeError)


def test_re_entry_computes_instead_of_waiting_on_itself() -> None:
    def outer() -> str:
        return "outer:" + coalesce(KEY, lambda: "inner", copy=lambda x: x)

    out: dict[str, object] = {}
    th = _run(lambda: coalesce(KEY, outer, copy=lambda x: x), out, "r")
    th.join(5)
    assert not th.is_alive(), "deadlocked on its own flight"
    assert out["r"] == "outer:inner"


def test_the_write_window_and_the_generation() -> None:
    g0 = write_generation()
    assert g0 is not None
    note_write()
    g1 = write_generation()
    assert g1 is not None and g1 > g0
    begin_write()
    try:
        assert write_generation() is None          # no sharing while a write runs
        begin_write()                              # nested / concurrent writes
        end_write()
        assert write_generation() is None
    finally:
        end_write()
    g2 = write_generation()
    assert g2 is not None and g2 > g1
