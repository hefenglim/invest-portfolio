"""Who made a change (spec 2026-10-18 §7 "Who made the change", plan P14 and W1-B "Actor").

The application stamps the trail rows it writes through a TEMP trigger installed on the
connections it opens for a mutating request (``user:<login>`` / ``user:guest``) or a job
(``job:<id>``); a writer it did not open keeps ``actor = NULL`` (外部程式／腳本). A GET or a
read-only POST connection never carries the trigger (DEF-065).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pytest_socket import disable_socket, enable_socket
from starlette.requests import Request

from portfolio_dash.api import deps
from portfolio_dash.api.app import create_app
from portfolio_dash.data_ingestion.store import insert_cash_movement
from portfolio_dash.portfolio import settlement_store as st
from portfolio_dash.portfolio.settlement_models import (
    ACTOR_BOOT,
    ACTOR_GUEST,
    ACTOR_JOB_PREFIX,
    actor_text,
)
from portfolio_dash.scheduler import jobs
from portfolio_dash.shared import db
from portfolio_dash.shared.config import get_settings
from portfolio_dash.shared.enums import Currency
from tests.reference.corpora import open_db
from tests.settlement.conftest import CorpusFactory

TEMP_TRIGGERS = "SELECT COUNT(*) FROM temp.sqlite_master WHERE type = 'trigger'"


def _settled(path: Path) -> None:
    conn = open_db(path)
    try:
        st.install_or_verify(conn, calc_version_changed=False)
        conn.execute("UPDATE settle_meta SET generation = 1, settled_through = '2026-06-11', "
                     "held_through = '2026-06-11', first_day = '2026-01-05' WHERE id = 1")
        conn.commit()
    finally:
        conn.close()


def _actors(path: Path) -> list[str | None]:
    conn = open_db(path)
    try:
        return [r[0] for r in conn.execute("SELECT actor FROM settle_change_log ORDER BY id")]
    finally:
        conn.close()


@pytest.fixture
def app_db(corpus_db: CorpusFactory, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A settled golden copy as the configured database (``shared/db.get_connection``)."""
    path = corpus_db("golden").path
    _settled(path)
    monkeypatch.setenv("DB_PATH", str(path))
    get_settings.cache_clear()
    try:
        yield path
    finally:
        get_settings.cache_clear()


def _deposit(conn: sqlite3.Connection, day: date = date(2026, 2, 2)) -> None:
    insert_cash_movement(conn, account_id="tw_broker", move_date=day, kind="DEPOSIT",
                         ccy=Currency.TWD, amount=Decimal("100"))


# --- install_actor ----------------------------------------------------------------------------


def test_a_raw_writer_keeps_a_null_actor_and_the_app_stamps_its_own(app_db: Path) -> None:
    raw = sqlite3.connect(app_db)
    _deposit(raw)
    raw.close()
    conn = db.connect_path(app_db)
    assert db.install_actor(conn, "user:owner")
    _deposit(conn, date(2026, 2, 3))
    conn.close()
    assert _actors(app_db) == [None, "user:owner"]
    assert [actor_text(a, viewer="owner") for a in _actors(app_db)] == ["外部程式／腳本", "你"]


def test_installing_moves_no_schema_version_counts_no_change_and_can_be_replaced(
        app_db: Path) -> None:
    conn = db.connect_path(app_db)
    try:
        version = conn.execute("PRAGMA schema_version").fetchone()[0]
        assert db.install_actor(conn, db.job_actor("quotes_tw"))
        assert conn.total_changes == 0 and not conn.in_transaction
        assert conn.execute("PRAGMA schema_version").fetchone()[0] == version
        assert conn.execute(TEMP_TRIGGERS).fetchone()[0] == 1
        _deposit(conn)
        assert db.install_actor(conn, ACTOR_BOOT)  # again: the actor is replaced
        assert conn.execute(TEMP_TRIGGERS).fetchone()[0] == 1
        _deposit(conn, date(2026, 2, 3))
    finally:
        conn.close()
    assert _actors(app_db) == ["job:quotes_tw", "system:boot"]
    assert db.job_actor("x") == f"{ACTOR_JOB_PREFIX}x"


def test_install_is_a_no_op_without_the_trail_table(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "bare.db")
    try:
        assert not db.install_actor(conn, ACTOR_BOOT)
        assert conn.execute(TEMP_TRIGGERS).fetchone()[0] == 0
    finally:
        conn.close()


def test_a_session_with_an_actor_stamps_and_one_without_does_not(app_db: Path) -> None:
    with db.session(actor=ACTOR_BOOT) as conn:
        _deposit(conn)
    with db.session() as conn:
        _deposit(conn, date(2026, 2, 3))
    assert _actors(app_db) == [ACTOR_BOOT, None]


def test_a_trigger_row_is_a_change_the_session_notes(app_db: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    notes: list[int] = []
    monkeypatch.setattr(db, "note_write", lambda: notes.append(1))
    with db.session(actor=ACTOR_BOOT) as conn:
        _deposit(conn)
        # the movement, the dirt, the trail row and its actor stamp
        assert conn.total_changes == 4
    assert notes == [1]


# --- the request actor (api/deps.get_conn) --------------------------------------------------


def _request(method: str, path: str, cookie: str | None = None) -> Request:
    headers = [] if cookie is None else [(b"cookie", f"pd_session={cookie}".encode())]
    return Request({"type": "http", "method": method, "path": path, "headers": headers,
                    "query_string": b""})


def _through_get_conn(request: Request, work: Callable[[sqlite3.Connection], None]) -> int:
    """Run *work* on the connection ``get_conn`` yields; return its TEMP-trigger count."""
    gen = deps.get_conn(request)
    conn = next(gen)
    temp = int(conn.execute(TEMP_TRIGGERS).fetchone()[0])
    work(conn)
    with pytest.raises(StopIteration):
        next(gen)
    return temp


def test_a_mutating_request_in_guest_mode_writes_as_guest(app_db: Path) -> None:
    assert _through_get_conn(_request("POST", "/api/cash/movements"), _deposit) == 1
    assert _actors(app_db) == [ACTOR_GUEST]
    assert actor_text(ACTOR_GUEST) == "訪客"


def test_a_logged_in_request_writes_as_its_login(app_db: Path) -> None:
    conn = open_db(app_db)
    conn.execute("INSERT INTO auth_users (username, name, password_hash, created_at) VALUES "
                 "('owner', 'Owner', 'x', '2026-01-01T00:00:00')")
    conn.execute("INSERT INTO auth_sessions (token, username, created_at, locked) VALUES "
                 "('tok', 'owner', '2026-01-01T00:00:00', 0)")
    conn.commit()
    conn.close()
    _through_get_conn(_request("PUT", "/api/cash/movements/1", cookie="tok"), _deposit)
    # protected mode without a valid session: no actor (the gate refuses such a request)
    assert _through_get_conn(_request("POST", "/api/x", cookie="bad"),
                             lambda c: _deposit(c, date(2026, 2, 3))) == 0
    assert _actors(app_db) == ["user:owner", None]


@pytest.mark.parametrize(("method", "path"), [
    ("GET", "/api/dashboard"), ("HEAD", "/api/dashboard"), ("POST", "/api/whatif"),
    ("POST", "/api/rebalance/preview"), ("POST", "/api/prompts/preview/"),
])
def test_a_read_connection_carries_no_temp_trigger_and_reads_no_session(
        app_db: Path, method: str, path: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_lookup(*_a: Any) -> None:
        raise AssertionError("a read request must not look up its actor")

    monkeypatch.setattr(deps, "request_actor", no_lookup)
    assert _through_get_conn(_request(method, path), lambda c: None) == 0


def test_a_real_request_through_the_app_is_stamped(app_db: Path) -> None:
    enable_socket()
    try:
        client = TestClient(create_app())
        r = client.post("/api/cash/movements", json={
            "account_id": "tw_broker", "date": "2026-02-02", "kind": "deposit", "ccy": "TWD",
            "amount": "1000"})
        assert r.status_code == 201, r.text
    finally:
        disable_socket(allow_unix_socket=True)
    assert _actors(app_db) == [ACTOR_GUEST]


# --- jobs ------------------------------------------------------------------------------------


def test_a_manually_run_job_and_a_cron_trigger_write_as_the_job(
        app_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def write(conn: sqlite3.Connection, *, now: datetime) -> str:
        _deposit(conn)
        return "完成。"

    spec = jobs.JobSpec("probe_job", write, "0 0 * * *", "Asia/Taipei", True, "")
    monkeypatch.setattr(jobs, "_jobs_by_id", lambda: {"probe_job": spec})
    monkeypatch.setattr(jobs, "_insight_payload", lambda conn, job_id: None)
    now = datetime(2026, 6, 11, 14, 30)
    with db.session() as conn:
        jobs.start_job_run(conn, "probe_job", now=now)
    jobs.run_job_func("probe_job", now=now)
    monkeypatch.setattr(jobs, "dispatch_job",
                        lambda conn, job_id, *, now: write(conn, now=now) and None)
    jobs.trigger_job("probe_job")
    assert _actors(app_db) == ["job:probe_job", "job:probe_job"]
