"""Test stubs for the REAL settlement worker in a child (``PD_TEST_STUBS``, wave-2 merge).

Imported by ``settle_worker.main()`` through ``child_runner.apply_test_stubs_from_env`` (it
needs ``PD_TEST_MODE=1``): importing this module patches one step of the real worker, picked
by ``PD_STUB_WORKER`` —

- ``die_before_claim``: the process exits (code 3) where the worker would claim;
- ``slow_claim``: sleeps ``PD_STUB_SLEEP_S`` seconds before the claim (the 準備中 window);
- ``slow_valuate``: sleeps before the valuation, after the claim (a run to kill mid-way);
- ``slow_phase``: sleeps before the ``_Run`` method named by ``PD_STUB_PHASE`` (``_load``,
  ``_check``, ``_plan``, ``_valuate``, ``_close``, ``_bench``, ``_swap``) — a kill in any phase.

``slow_*`` sleep only while the marker file named by ``PD_STUB_ONCE`` exists, and delete it
first, so the run that follows (a resume, a chained run) is not slowed again. Everything else
is the real worker: the lock, the progress file, the claim, the swap.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

from portfolio_dash.portfolio import settle_worker

_MODE = os.environ.get("PD_STUB_WORKER", "")
_SLEEP_S = float(os.environ.get("PD_STUB_SLEEP_S", "60"))


def _once() -> bool:
    marker = os.environ.get("PD_STUB_ONCE")
    if not marker:
        return False
    path = Path(marker)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


_worker_main = sys.modules.get("__main__")
_target = _worker_main if hasattr(_worker_main, "_Run") else settle_worker
_real_claim = _target._Run._claim  # type: ignore[union-attr]
_real_valuate = _target._Run._valuate  # type: ignore[union-attr]


def _claim(self: Any) -> None:
    if _MODE == "die_before_claim":
        os._exit(3)
    if _MODE == "slow_claim" and _once():
        time.sleep(_SLEEP_S)
    _real_claim(self)


def _valuate(self: Any, *args: Any, **kwargs: Any) -> Any:
    if _MODE == "slow_valuate" and _once():
        time.sleep(_SLEEP_S)
    return _real_valuate(self, *args, **kwargs)


_PHASE = os.environ.get("PD_STUB_PHASE", "")


def _slowed(real: Any) -> Any:
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        if _MODE == "slow_phase" and _once():
            time.sleep(_SLEEP_S)
        return real(self, *args, **kwargs)

    return wrapper


# The child runs the worker as ``python -m``: its classes live in ``__main__``, a second copy
# of the module next to the ``settle_worker`` imported above. Patch every copy present.
for _module in {id(m): m for m in (settle_worker, sys.modules.get("__main__"))
                if m is not None and hasattr(m, "_Run")}.values():
    _module._Run._claim = _claim
    _module._Run._valuate = _valuate
    if _MODE == "slow_phase" and _PHASE:
        setattr(_module._Run, _PHASE, _slowed(getattr(_module._Run, _PHASE)))
