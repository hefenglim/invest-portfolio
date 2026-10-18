"""The settlement service: starts, watches, cancels and reports settlement runs (PERF).

Spec 2026-10-18 §9.1, §9.4, §15. The parent of every run: it computes the run's inputs (the
``RunRequest``, P12), creates the ``preparing`` report and its ``job_runs`` row before the
spawn, and finalises both when the child exits (P7). Wave 0 fixes the boot hook only.
"""

import sqlite3


def on_boot(conn: sqlite3.Connection) -> None:
    """Called by the lifespan after the settle DDL (and, from wave 1, the trigger check).

    A no-op until wave 2 (package E), which finalises orphan runs and starts the boot run.
    """
    del conn


__all__ = ["on_boot"]
