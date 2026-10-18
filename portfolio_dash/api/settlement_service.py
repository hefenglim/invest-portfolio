"""The settlement service: starts, watches, cancels and reports settlement runs (PERF).

Spec 2026-10-18 §9.1, §9.4, §15. The parent of every run: it computes the run's inputs (the
``RunRequest``, P12), creates the ``preparing`` report and its ``job_runs`` row before the
spawn, and finalises both when the child exits (P7). Wave 0 fixes the boot hook only.
"""

import sqlite3

from portfolio_dash.portfolio.settlement_store import TriggerCheck


def on_boot(conn: sqlite3.Connection, *, trigger_check: TriggerCheck) -> None:
    """Called by the lifespan after the settle DDL and the trigger check, on the boot session,
    before ``trigger_schema_version`` is recorded. *trigger_check* says whether the trigger set
    was recreated (spec §9.1: a recreated trigger starts a full run).

    A no-op until wave 2 (package E), which finalises orphan runs and starts the boot run.
    """
    del conn, trigger_check


__all__ = ["on_boot"]
