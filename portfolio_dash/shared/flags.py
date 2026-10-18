"""The settlement program's process flags (PERF plan P5), read from ``os.environ``.

Four switches, each a closed vocabulary:

- ``PD_SETTLE`` — ``on`` / ``off``, prod ``on``. ``off`` is the kill switch: no run starts and
  every read is a full live build (spec §9.1).
- ``PD_SETTLE_MODE`` — ``child`` / ``inline``, prod ``child``: where a settlement run executes
  (spec §9.2).
- ``PD_JOB_MODE`` — ``child`` / ``inline``, prod ``child``: where a scheduled job executes
  (spec §16).
- ``PD_PREWARM`` — ``1`` / ``0``, prod ``1``: pre-warm today's result after a run (§9.3 #8).

**Read on every call, never cached.** `Settings` (`shared/config.py`) is `lru_cache`d, and the
tests flip these flags per test with ``monkeypatch.setenv``; a cached copy would make a flip
silently ineffective. A read is one dict lookup, so nothing is gained by caching.

**Unset means the prod default; anything else outside the vocabulary raises.** Exact match:
``ON``, ``true`` or an empty string are refused with a ``ValueError`` naming the variable and
its allowed values. A typo in the kill switch must stop the boot, not quietly mean "on".

The unit-test defaults (``on`` / ``inline`` / ``inline`` / ``0``) are set by
``tests/conftest.py``; the e2e servers set ``PD_SETTLE=off`` unless a flow opts in (plan P5).
**Inline mode is synchronous** — a caller never runs it on a request thread or a background
thread (plan P5).

Imports nothing internal, so every layer may read a flag.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

SettleMode = Literal["child", "inline"]
JobMode = Literal["child", "inline"]

#: variable -> its allowed values, in the order the error message lists them.
ALLOWED: dict[str, tuple[str, ...]] = {
    "PD_SETTLE": ("on", "off"),
    "PD_SETTLE_MODE": ("child", "inline"),
    "PD_JOB_MODE": ("child", "inline"),
    "PD_PREWARM": ("1", "0"),
}

#: variable -> the value an unset variable reads as (production).
PROD_DEFAULTS: dict[str, str] = {
    "PD_SETTLE": "on",
    "PD_SETTLE_MODE": "child",
    "PD_JOB_MODE": "child",
    "PD_PREWARM": "1",
}


@dataclass(frozen=True)
class Flags:
    """All four flags, read and validated together (e.g. once at boot, for the log)."""

    settle_enabled: bool
    settle_mode: SettleMode
    job_mode: JobMode
    prewarm_enabled: bool


def raw(name: str) -> str:
    """The validated value of flag *name* (its prod default when unset).

    Raises ``KeyError`` for a name that is not one of the four flags, and ``ValueError``
    naming the variable and its allowed values for a value outside the vocabulary.
    """
    allowed = ALLOWED[name]
    value = os.environ.get(name)
    if value is None:
        return PROD_DEFAULTS[name]
    if value not in allowed:
        raise ValueError(
            f"{name}={value!r} is not valid; allowed values: {', '.join(allowed)} "
            f"(unset means {PROD_DEFAULTS[name]!r})"
        )
    return value


def _mode(name: str) -> Literal["child", "inline"]:
    return "inline" if raw(name) == "inline" else "child"


def settle_enabled() -> bool:
    """``PD_SETTLE``: False only for ``off`` — the kill switch."""
    return raw("PD_SETTLE") == "on"


def settle_mode() -> SettleMode:
    """``PD_SETTLE_MODE``: ``child`` (a separate process) or ``inline`` (in-process, sync)."""
    return _mode("PD_SETTLE_MODE")


def job_mode() -> JobMode:
    """``PD_JOB_MODE``: ``child`` (a job worker process) or ``inline`` (in-process, sync)."""
    return _mode("PD_JOB_MODE")


def prewarm_enabled() -> bool:
    """``PD_PREWARM``: True for ``1``."""
    return raw("PD_PREWARM") == "1"


def current() -> Flags:
    """Read and validate every flag at once (raises on the first invalid one)."""
    return Flags(
        settle_enabled=settle_enabled(),
        settle_mode=settle_mode(),
        job_mode=job_mode(),
        prewarm_enabled=prewarm_enabled(),
    )
