"""Shared set-up for the settlement SERVICE tests (package E): a private, file-backed golden
copy as the configured database, the store's triggers live and verified, and the worker
replaced by ``tests/settlement/stub_settle_worker.py``.

Not a conftest (``tests/settlement/conftest.py`` belongs to the store's package): each test
module builds its fixture from :func:`prepare` so the set-up has one owner.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.api import auth_store
from portfolio_dash.api import settlement_service as service
from portfolio_dash.data_ingestion.store import insert_transaction
from portfolio_dash.ops import notify
from portfolio_dash.portfolio import settle_worker
from portfolio_dash.portfolio import settlement_store as st
from portfolio_dash.portfolio.settlement_models import RunRequest
from portfolio_dash.scheduler import jobs
from portfolio_dash.shared.config import get_settings
from portfolio_dash.shared.models.enums import Side
from tests.reference.corpora import open_db
from tests.settlement import stub_settle_worker as stub

TPE = ZoneInfo("Asia/Taipei")


def prepare(path: Path, monkeypatch: pytest.MonkeyPatch, *, stub_worker: bool = True) -> Path:
    """Make *path* (a corpus copy) the configured database with a live, verified trigger
    set, the service's in-process state empty, and (by default) the stub worker inline."""
    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.setenv("PD_SETTLE", "on")
    monkeypatch.setenv("PD_SETTLE_MODE", "inline")
    monkeypatch.setenv("PD_PREWARM", "0")
    get_settings.cache_clear()
    conn = open_db(path)
    try:
        notify.ensure_seeded(conn)  # DDL first: the verified schema_version is recorded last
        st.install_or_verify(conn, calc_version_changed=False)
        st.record_schema_version(conn)
        conn.commit()
    finally:
        conn.close()
    service.reset_for_tests()
    stub.PLAN.clear()
    stub.RAN.clear()
    PUSHES.clear()
    if stub_worker:
        monkeypatch.setattr(settle_worker, "run", stub.run)
    monkeypatch.setattr(service, "notify_failure", _record_push(service.notify_failure))
    return path


PUSHES: list[int] = []


def _record_push(real: Callable[..., Any]) -> Callable[..., Any]:
    def wrapped(run_id: int, **kwargs: Any) -> Any:
        PUSHES.append(run_id)
        return real(run_id, **kwargs)

    return wrapped


def db(path: Path) -> sqlite3.Connection:
    return open_db(path)


def job_row(path: Path, run_id: int) -> sqlite3.Row:
    conn = open_db(path)
    try:
        row: sqlite3.Row | None = conn.execute(
            "SELECT * FROM job_runs WHERE id = ?", (run_id,)).fetchone()
        assert row is not None, f"no job_runs row {run_id}"
        return row
    finally:
        conn.close()


def report(path: Path, run_id: int) -> Any:
    conn = open_db(path)
    try:
        found = st.get_run(conn, run_id)
        assert found is not None, f"no report {run_id}"
        return found
    finally:
        conn.close()


def state(path: Path) -> st.StoreState:
    conn = open_db(path)
    try:
        return st.store_state(conn, reporting=get_settings().reporting_currency)
    finally:
        conn.close()


def runs(path: Path) -> list[Any]:
    conn = open_db(path)
    try:
        return list(reversed(st.list_runs(conn, limit=100).items))
    finally:
        conn.close()


def back_dated_trade(path: Path, day: date) -> None:
    """A ledger write the triggers record (a buy of 2330 on *day*)."""
    conn = open_db(path)
    try:
        insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                           quantity=Decimal("1"), price=Decimal("500"), fees=Decimal("0"),
                           tax=Decimal("0"), trade_date=day)
        conn.commit()
    finally:
        conn.close()


def fake_preparing_run(path: Path, *, scope: str = "dirty", reason: str = "button",
                       through: date | None = None, claim: bool = False,
                       started_by: str | None = None) -> int:
    """A run report + its ``job_runs`` row as the service would have written them (and,
    with *claim*, claimed as a worker would) — with no worker behind it."""
    conn = open_db(path)
    try:
        now = datetime.now(TPE)
        through = through or now.date() - timedelta(days=1)
        run_id = jobs.start_job_run(conn, jobs.SETTLEMENT_JOB_ID, now=now)
        request = RunRequest(scope=scope, reason=reason, started_by=started_by,  # type: ignore[arg-type]
                             through=through)
        st.create_run(conn, request, run_id=run_id, trigger_text=service.trigger_text(
            reason, now=now), started_at=now)
        if claim:
            st.claim(conn, run_id, scope=scope, run_through=through)  # type: ignore[arg-type]
        conn.commit()
        return run_id
    finally:
        conn.close()


def protect(path: Path) -> None:
    """Protected mode (a user exists): pushes are no longer suppressed as guest / demo."""
    conn = open_db(path)
    try:
        auth_store.create_user(conn, username="owner", name="owner", password="pw-123456",
                               now=datetime.now(TPE))
        conn.commit()
    finally:
        conn.close()


def wait_until(predicate: Callable[[], bool], timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting")
        time.sleep(0.05)
