"""/api/settlement/* — the JSON contract (spec §15, plan §6.2 "Routes").

Shapes, status codes, zh messages in the ``PdApiError`` envelope, the two action-log labels,
and DEF-065 on a NON-trivial store (a published run, a pending change, an active report): the
class test in ``test_def065_get_never_writes.py`` walks every GET on a fresh boot, where the
settlement routes have nothing to read.

The app runs WITHOUT its lifespan on a file-backed golden copy that is the configured
database (the service opens its own sessions, as in production); the worker is the stub.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pytest_socket import disable_socket, enable_socket

from portfolio_dash.api import action_log
from portfolio_dash.api import settlement_service as service
from portfolio_dash.api.action_log import label_for, should_log
from portfolio_dash.api.app import create_app
from portfolio_dash.shared import proc_lock
from portfolio_dash.shared.config import get_settings
from tests.conftest import _seed_golden, init_golden_base
from tests.reference.corpora import open_db
from tests.settlement import service_support as sup

_WRITE = re.compile(r"^\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP)\b", re.I)
_NOOP_DDL = re.compile(r"^\s*CREATE\s+(TABLE|INDEX|UNIQUE\s+INDEX)\s+IF\s+NOT\s+EXISTS\b", re.I)


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "settle-api.db"
    conn = open_db(path)
    init_golden_base(conn)
    _seed_golden(conn)
    action_log.ensure_table(conn)  # boot creates it; created lazily it would move schema_version
    conn.commit()
    conn.close()
    sup.prepare(path, monkeypatch)
    enable_socket()
    try:
        yield path
    finally:
        disable_socket(allow_unix_socket=True)
        service.reset_for_tests()
        get_settings.cache_clear()


@pytest.fixture
def client(db_path: Path) -> TestClient:
    return TestClient(create_app(), raise_server_exceptions=False)


def _error(r: Any, status: int, code: str) -> str:
    assert r.status_code == status, r.text
    body = r.json()["error"]
    assert body["code"] == code, body
    assert re.search(r"[一-鿿]", body["message"]), body
    return str(body["message"])


def test_the_two_writes_have_their_action_log_labels() -> None:
    assert label_for("POST", "/api/settlement/run") == "重新結算"
    assert label_for("POST", "/api/settlement/cancel") == "取消重新結算"
    assert not should_log("GET", "/api/settlement/runs")


def test_status_shape(client: TestClient) -> None:
    r = client.get("/api/settlement/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "pending"  # an unsettled ledger
    for key in ("generation", "settled_through", "calc_ok", "dirty_from", "live_from",
                "stale_from", "stale_to", "needs_prompt", "prompt", "pending", "queued",
                "final_through", "run", "slow", "last_run", "last_change_run"):
        assert key in body, key
    assert set(body["pending"]) == {"tables", "records_total"}


def test_a_run_answers_202_and_its_report_reads_back(client: TestClient, db_path: Path) -> None:
    r = client.post("/api/settlement/run", json={"scope": "full"})
    assert r.status_code == 202, r.text
    run_id = r.json()["run_id"]
    assert r.json() == {"run_id": run_id}
    report = client.get(f"/api/settlement/runs/{run_id}").json()
    assert report["summary"]["status"] == "published"
    assert report["summary"]["reason"] == "button"
    assert report["summary"]["started_by"] == "user:guest"
    assert report["summary"]["trigger_text"].startswith("你 於 ")  # guest viewing its own run
    assert report["inputs"]["scope"] == "full" and report["restatements"] == []
    listed = client.get("/api/settlement/runs").json()
    assert listed["total_count"] == 1 and listed["rows"][0]["run_id"] == run_id
    assert client.get("/api/settlement/runs", params={"only_changes": True}).json() == {
        "rows": [], "total_count": 0}
    diff = client.get(f"/api/settlement/runs/{run_id}/diff").json()
    assert diff == {"rows": [], "total_count": 0}
    status = client.get("/api/settlement/status").json()
    assert status["state"] == "clean" and status["generation"] == 1
    assert status["last_run"]["run_id"] == run_id


def test_the_prompt_confirmation_is_a_dirty_run(client: TestClient, db_path: Path) -> None:
    client.post("/api/settlement/run", json={"scope": "full"})
    sup.back_dated_trade(db_path, date(2026, 2, 2))
    status = client.get("/api/settlement/status").json()
    assert status["needs_prompt"] and status["prompt"]["from"] == "2026-02-02"
    pending = client.get("/api/settlement/changes").json()
    assert pending["total_count"] == 1
    [row] = pending["rows"]
    assert (row["table_label"], row["op_text"], row["state"]) == ("交易", "新增", "pending")
    assert row["account_text"] == "{account:tw_broker}" and row["actor_text"] == "外部程式／腳本"
    r = client.post("/api/settlement/run", json={"scope": "dirty"})
    assert r.status_code == 202, r.text
    run_id = r.json()["run_id"]
    report = client.get(f"/api/settlement/runs/{run_id}").json()
    assert report["summary"]["reason"] == "prompt"
    assert "確認提示：交易變更" in report["summary"]["trigger_text"]
    settled = client.get("/api/settlement/changes", params={"state": f"run:{run_id}"}).json()
    assert settled["total_count"] == 1 and settled["rows"][0]["state"] == "settled"
    assert client.get("/api/settlement/status").json()["needs_prompt"] is False


def test_refusals_speak_chinese(client: TestClient, db_path: Path,
                                monkeypatch: pytest.MonkeyPatch) -> None:
    _error(client.post("/api/settlement/run", json={"scope": "nightly"}), 400,
           "validation_error")
    _error(client.post("/api/settlement/run", json={}), 400, "validation_error")
    _error(client.post("/api/settlement/cancel"), 409, "not_running")
    active = sup.fake_preparing_run(db_path)
    with proc_lock.lock(proc_lock.settlement_lock_path(), timeout=2):  # its child is alive
        message = _error(client.post("/api/settlement/run", json={"scope": "full"}), 409,
                         "already_running")
    assert f"#{active}" in message
    _error(client.get("/api/settlement/runs/9999"), 404, "not_found")
    _error(client.get("/api/settlement/runs/9999/diff"), 404, "not_found")
    _error(client.get("/api/settlement/changes", params={"state": "run:9999"}), 404,
           "not_found")
    _error(client.get("/api/settlement/changes", params={"state": "claimed"}), 400,
           "validation_error")
    _error(client.get(f"/api/settlement/runs/{active}/diff", params={"field": "x"}), 400,
           "validation_error")
    _error(client.get(f"/api/settlement/runs/{active}/diff", params={"kind": "x"}), 400,
           "validation_error")
    _error(client.get("/api/settlement/runs", params={"limit": 0}), 400, "validation_error")
    _error(client.get("/api/settlement/runs", params={"offset": -1}), 400, "validation_error")
    monkeypatch.setenv("PD_SETTLE", "off")
    _error(client.post("/api/settlement/run", json={"scope": "full"}), 409, "settle_off")
    assert client.get("/api/settlement/status").json()["state"] == "off"


class _WriteLock:
    def __init__(self, path: Path) -> None:
        self._conn = sqlite3.connect(str(path), isolation_level=None)

    def __enter__(self) -> _WriteLock:
        self._conn.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, *exc: object) -> None:
        self._conn.execute("ROLLBACK")
        self._conn.close()


def test_no_settlement_get_writes_and_each_answers_under_a_held_write_lock(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DEF-065 with something to read: a published run, a pending change, an active run."""
    seed = TestClient(create_app())
    first = seed.post("/api/settlement/run", json={"scope": "full"}).json()["run_id"]
    sup.back_dated_trade(db_path, date(2026, 2, 2))
    active = sup.fake_preparing_run(db_path)
    traced: list[str] = []
    real = sqlite3.connect

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        kwargs["timeout"] = 0.5
        conn: sqlite3.Connection = real(*args, **kwargs)
        conn.set_trace_callback(traced.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", connect)
    client = TestClient(create_app(), raise_server_exceptions=False)
    paths = ["/api/settlement/status", "/api/settlement/runs",
             f"/api/settlement/runs/{first}", f"/api/settlement/runs/{active}",
             f"/api/settlement/runs/{first}/diff", "/api/settlement/changes",
             f"/api/settlement/changes?state=run:{first}"]
    with _WriteLock(db_path):
        for url in paths:
            r = client.get(url)
            assert r.status_code == 200, (url, r.text)
    writes = [s for s in traced if _WRITE.match(s) and not _NOOP_DDL.match(s)]
    assert not writes, writes[:5]
    assert client.get("/api/settlement/status").json()["state"] == "preparing"
