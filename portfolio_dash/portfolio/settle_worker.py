"""The settlement worker (PERF spec 2026-10-18 §9.2): one run of the settlement, in a child.

Started by ``api/settlement_service.py`` as ``python -m portfolio_dash.portfolio.settle_worker
--run <id>`` (``shared/child_runner.py``), or called in-process by :func:`run` when
``PD_SETTLE_MODE=inline`` (hermetic tests, Windows development; synchronous, never on a request
thread). It reads its :class:`~portfolio_dash.portfolio.settlement_models.RunRequest` from the
``preparing`` report of its run id, takes its OS lock FIRST, then claims (§9.4).

Imports only ``portfolio/``, ``pricing/``, ``forex/``, ``data_ingestion/`` and ``shared/`` —
never ``fastapi``, ``litellm``, ``yfinance``, ``pandas`` or any ``api`` / ``scheduler`` /
``export`` / ``strategy`` / ``news`` / ``ops`` / ``llm_insight`` module (a test inspects
``sys.modules`` after the worker's imports).

Wave 0 fixes the entry points only; the run itself is wave 2 (package D).
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from portfolio_dash.shared.config import get_settings


def run(run_id: int, *, db_path: Path) -> None:
    """Execute run *run_id* against *db_path* (never renices: it may run in the web process)."""
    raise NotImplementedError(f"settlement run {run_id} on {db_path.name}: not implemented yet")


def main(argv: Sequence[str] | None = None) -> int:
    """The child's entry: parse ``--run``, lower its own priority, then :func:`run`."""
    parser = argparse.ArgumentParser(prog="settle_worker")
    parser.add_argument("--run", type=int, required=True, dest="run_id")
    args = parser.parse_args(argv)
    run(args.run_id, db_path=get_settings().db_path)
    return 0


if __name__ == "__main__":  # pragma: no cover - the child process entry
    raise SystemExit(main())
