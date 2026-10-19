"""A stand-in for the settlement worker (package D), for the service tests (package E).

It honours the worker contract the service relies on (``api/settlement_service.py``'s
docstring): read the ``RunRequest`` from the ``preparing`` report, take the settle lock FIRST,
write the progress file with its own pid, claim, then end the run through ``swap`` or
``end_without_publish`` and freeze the report. The settled figures are synthetic (``100`` per
day): these tests are about the lifecycle, not about what a day is worth.

Each call consumes the next behaviour of a plan:

- in-process (inline mode): :data:`PLAN`, a list the test fills;
- in a child (``python -m tests.settlement.stub_settle_worker --run <id>``): the JSON list in
  the file named by ``PD_STUB_SETTLE_PLAN`` (read-modify-write, one child at a time).

Behaviours: ``publish`` (default), ``fail``, ``die_before_claim``, ``die_after_claim``,
``sleep`` (claims, then sleeps until terminated), ``selfcheck_diff``, ``change_during``
(commits a back-dated trade after the claim, so the swap publishes partially).
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import traceback
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from portfolio_dash.data_ingestion.store import insert_transaction
from portfolio_dash.portfolio import settlement_store as st
from portfolio_dash.portfolio.settlement_models import (
    PHASES,
    HistoryResult,
    PeriodKpi,
    RunFailure,
    RunPhase,
    RunProgress,
)
from portfolio_dash.shared import proc_lock
from portfolio_dash.shared.config import get_settings
from portfolio_dash.shared.db import connect_path
from portfolio_dash.shared.models.enums import Side

PLAN: list[str] = []
#: Behaviours the in-process stub ran, in order (assertions read it).
RAN: list[tuple[int, str]] = []
PLAN_ENV = "PD_STUB_SETTLE_PLAN"


class StubDied(RuntimeError):
    """An in-process stand-in for a child that died."""


def _next_behaviour() -> str:
    path = os.environ.get(PLAN_ENV)
    if path:
        file = Path(path)
        plan = json.loads(file.read_text(encoding="utf-8")) if file.exists() else []
        behaviour = plan.pop(0) if plan else "publish"
        file.write_text(json.dumps(plan), encoding="utf-8")
        return str(behaviour)
    return PLAN.pop(0) if PLAN else "publish"


def _data_dir(db_path: Path) -> Path:
    return db_path.resolve().parent


def _write_progress(db_path: Path, run_id: int, *, phase: str) -> None:
    conn = connect_path(db_path)
    try:
        report = st.get_run(conn, run_id)
    finally:
        conn.close()
    assert report is not None
    keys = [k for k, _ in PHASES]
    at = keys.index(phase)
    phases = [RunPhase(key=k, label=label,
                       status="done" if i < at else "running" if i == at else "pending")
              for i, (k, label) in enumerate(PHASES)]
    progress = RunProgress(
        run_id=run_id, pid=os.getpid(), heartbeat=datetime.now(UTC), status="running",
        reason=report.summary.reason, started_by=report.summary.started_by,
        trigger_text=report.summary.trigger_text, started_at=report.summary.started_at,
        phases=phases)
    target = _data_dir(db_path) / "settlement-progress.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(progress.model_dump_json(by_alias=True), encoding="utf-8")
    # Windows: replacing a file another process has open (a status read) raises
    # PermissionError — a real worker must retry, so the stand-in does too.
    for attempt in range(50):
        try:
            os.replace(tmp, target)
            break
        except PermissionError:
            if attempt == 49:
                raise
            time.sleep(0.02)


def _publish(conn: sqlite3.Connection, run_id: int, claim: st.Claim, *,
             selfcheck_diff: bool = False) -> None:
    reporting = get_settings().reporting_currency
    calc = st.calc_version(reporting)
    if claim.run_from is None:
        result = st.swap(conn, run_id, calc_version=calc, bench_state=None,
                         through=claim.run_through, first_day=None, reporting=reporting)
    else:
        new = st.next_generation(conn)
        if claim.generation > 0:
            st.copy_before(conn, claim.generation, new, claim.run_from)
        first = claim.run_from
        if claim.generation > 0 and claim.first_day is not None:
            first = min(first, claim.first_day)
        days = [claim.run_from + timedelta(days=i)
                for i in range((claim.run_through - claim.run_from).days + 1)]
        st.write_days(conn, new, [st.SettledDay(
            day=d, total_value=Decimal("100"), net_invested=Decimal("50"),
            net_worth=Decimal("101"), cash_total=Decimal("1"), cash_incomplete=False,
            incomplete=False, options_open=False) for d in days])
        st.write_periods(conn, new, [
            st.SettledPeriod(kind=k, period=p, end_day=e, kpi=PeriodKpi(reporting_ccy=reporting))
            for k, p, e in st.period_ends(first, claim.run_through) if e >= claim.run_from])
        result = st.swap(conn, run_id, calc_version=calc, bench_state=None,
                         through=claim.run_through, first_day=first, reporting=reporting)
    report = st.get_run(conn, run_id)
    assert report is not None
    if selfcheck_diff:
        report = report.model_copy(update={
            "summary": report.summary.model_copy(update={"selfcheck_diff": True}),
            "history": HistoryResult(first_changed_day=claim.run_from,
                                     note="settled_day 與已發布版本不同")})
    st.finalise_run(conn, run_id, result.status, report)


def run(run_id: int, *, db_path: Path) -> None:
    behaviour = _next_behaviour()
    RAN.append((run_id, behaviour))
    in_child = bool(os.environ.get(PLAN_ENV))
    lock = proc_lock.lock(proc_lock.settlement_lock_path(_data_dir(db_path)), timeout=5)
    conn = connect_path(db_path)
    try:
        report = st.get_run(conn, run_id)
        assert report is not None
        request = report.inputs
        _write_progress(db_path, run_id, phase="load")
        if behaviour == "die_before_claim":
            if in_child:
                os._exit(3)
            raise StubDied("died before the claim")
        claim = st.claim(conn, run_id, scope=request.scope, run_through=request.through)
        _write_progress(db_path, run_id, phase="valuate")
        if behaviour == "die_after_claim":
            if in_child:
                os._exit(3)
            raise StubDied("died after the claim")
        if behaviour == "sleep":
            time.sleep(120)
        if behaviour == "fail":
            st.end_without_publish(conn, run_id, "failed")
            failed = st.get_run(conn, run_id)
            assert failed is not None
            st.finalise_run(conn, run_id, "failed", failed.model_copy(update={
                "failure": RunFailure(phase="valuate", reason_text="測試：逐日估值失敗")}))
            raise RuntimeError("stub failure in valuate")
        if behaviour == "change_during" and claim.run_from is not None:
            other = connect_path(db_path)
            try:
                insert_transaction(other, account_id="tw_broker", symbol="2330", side=Side.BUY,
                                   quantity=Decimal("1"), price=Decimal("500"),
                                   fees=Decimal("0"), tax=Decimal("0"),
                                   trade_date=claim.run_from + timedelta(days=30))
                other.commit()
            finally:
                other.close()
        _publish(conn, run_id, claim, selfcheck_diff=behaviour == "selfcheck_diff")
    finally:
        conn.close()
        lock.release()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stub_settle_worker")
    parser.add_argument("--run", type=int, required=True, dest="run_id")
    args = parser.parse_args(argv)
    try:
        run(args.run_id, db_path=get_settings().db_path)
    except Exception:  # noqa: BLE001 — the traceback is the child's stderr, as for D's
        traceback.print_exc(file=sys.stderr)
        return 1
    return 0



if __name__ == "__main__":  # pragma: no cover - the child process entry
    raise SystemExit(main())
