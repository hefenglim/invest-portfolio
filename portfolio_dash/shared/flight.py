"""Share ONE in-flight computation between concurrent identical requests (2026-10-13).

Why: on the prod VM one dashboard build over the owner's 6-year ledger took tens of seconds,
and at least seven pages fetch ``/api/dashboard``. A sync route cannot be cancelled when the
browser leaves the page, so clicking through six pages left six identical builds running
side by side on one small CPU — measured: still computing 20 minutes later, with
``/api/health`` answering in 7 s because every request queued behind them. A request that
arrives while an identical build is running now waits for that build instead of starting
its own.

What a caller can rely on:

* **In flight only — nothing is kept.** Only callers that arrive while the computation is
  RUNNING share it. When it returns, the flight is gone; the next caller computes afresh.
  So no figure that depends on the clock (a quote's age, 「未來日期」) outlives the request
  that computed it, and there is no cache to invalidate.
* **A write closes the window.** A caller never joins a computation that may have started
  before a write it could otherwise observe. Two signals feed :func:`write_generation`:
  every mutating HTTP request brackets itself with :func:`begin_write` / :func:`end_write`
  (``api/app.py``) — no sharing at all while one runs — and every ``shared.db.session``
  that changed a row commits inside a write window of its own and calls :func:`note_write`
  when it closes, which covers the scheduler and the background runners too. The session's
  window is not redundant: a request's connection commits in its dependency's exit, after
  the HTTP window has already closed. A background writer's commit that lands DURING someone
  else's build is seen by that build's joiners as it would be by a caller who had arrived
  a moment earlier — the same answer, a few seconds older — never as a lost write of
  their own.
* **Every participant owns its object.** The leader returns the object it computed; each
  joiner gets ``copy(result)``, made by the leader BEFORE it returns, so a caller that
  mutates its answer can never race another caller's copy.
* **Errors are shared too.** If the computation raises, every joiner raises the same
  exception — a joiner never silently falls back to computing it a second time.
* **Re-entry is safe.** A thread that asks for a key it is itself computing computes it
  directly instead of waiting on itself.

The key decides what "identical" means; ``None`` means "do not share" and simply computes.

**The result cache sits on top** (PERF 2026-10-18, ``shared/result_cache.py``): it KEEPS
the latest result per name until the database changes, and delegates its in-flight sharing
to :func:`coalesce` with its own key — which adds the database's ``PRAGMA data_version`` to
:func:`write_generation`. The write windows here stay the cache's process-local signal.
"Nothing is kept" remains true of this module.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Hashable
from typing import cast

_lock = threading.Lock()
_generation = 0
_writes_in_progress = 0


class _Flight:
    __slots__ = ("copies", "done", "error", "joiners", "owner")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.owner = threading.get_ident()
        self.joiners = 0
        self.copies: list[object] = []
        self.error: BaseException | None = None


_flights: dict[Hashable, _Flight] = {}


def note_write() -> None:
    """A row was changed — no caller from now on joins a computation started before it."""
    global _generation
    with _lock:
        _generation += 1


def begin_write() -> None:
    """A mutating request starts: stop sharing until :func:`end_write`."""
    global _generation, _writes_in_progress
    with _lock:
        _generation += 1
        _writes_in_progress += 1


def end_write() -> None:
    """The mutating request is over (committed or not)."""
    global _generation, _writes_in_progress
    with _lock:
        _writes_in_progress -= 1
        _generation += 1


def write_generation() -> int | None:
    """The current write generation, or ``None`` while a write is in progress (no sharing)."""
    with _lock:
        return None if _writes_in_progress else _generation


def coalesce[T](key: Hashable | None, compute: Callable[[], T], *,
                copy: Callable[[T], T]) -> T:
    """Run ``compute()``, or wait for the identical computation already running under ``key``.

    ``key=None`` computes directly. ``copy`` gives each joiner its own object (see the module
    docstring for the guarantees).
    """
    if key is None:
        return compute()
    with _lock:
        flight = _flights.get(key)
        if flight is None:
            flight = _Flight()
            _flights[key] = flight
            leader = True
        elif flight.owner == threading.get_ident():
            leader = False
            flight = None          # re-entry: compute directly, never wait on oneself
        else:
            flight.joiners += 1
            leader = False
    if flight is None:
        return compute()
    if not leader:
        flight.done.wait()
        with _lock:
            if flight.error is not None:
                raise flight.error
            return cast(T, flight.copies.pop())
    try:
        result = compute()
    except BaseException as exc:
        with _lock:
            del _flights[key]
            flight.error = exc
        flight.done.set()
        raise
    with _lock:
        del _flights[key]           # no one can join from here on
        joiners = flight.joiners
    try:
        flight.copies = [copy(result) for _ in range(joiners)]
    except Exception as exc:  # noqa: BLE001 — the joiners get it; the leader keeps its answer
        flight.error = exc
    finally:
        flight.done.set()           # a joiner must never wait forever
    return result
