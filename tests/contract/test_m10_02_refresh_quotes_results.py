"""M10-02 — the refresh-quotes door tells the caller what it lost, structurally.

``POST /api/actions/refresh-quotes`` used to answer ``{run_ids, jobs}`` and nothing else; the
toast then said 「報價更新完成」 over 18 lost holdings. The response now carries an ADDITIVE
``results`` block per job built from the ``RefreshSummary`` (never parsed back out of the
detail string), and the run row is ``partial`` when a held instrument failed. The existing
contract (``tests/contract/test_actions_api.py``) asserts only ``set(jobs)`` and
``len(run_ids)``, so it is untouched.

``api_client`` skips the lifespan, so the held-symbols seam is registered here the way the
app registers it — through the same function ``api/app.py`` wires.
"""

import sqlite3
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.api.routers.actions import held_symbols
from portfolio_dash.pricing.registry import Registry
from portfolio_dash.scheduler import jobs
from portfolio_dash.scheduler.jobs import register_held_symbols_fn


@pytest.fixture
def _hermetic_all_fail(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """An empty provider chain: every symbol and pair fails, no socket is opened."""
    monkeypatch.setattr(
        jobs, "default_registry", lambda conn=None: Registry(providers={}, order={})
    )
    register_held_symbols_fn(held_symbols)
    yield
    register_held_symbols_fn(None)


def test_results_block_is_additive_and_partial_when_holdings_fail(
    api_client: TestClient, golden_db: sqlite3.Connection, _hermetic_all_fail: None
) -> None:
    r = api_client.post("/api/actions/refresh-quotes", json={"markets": ["US"]})
    assert r.status_code == 200
    b = r.json()
    # The old contract is intact …
    assert b["jobs"] == ["quotes_us"] and len(b["run_ids"]) == 1
    # … and the new block says what happened, per job, from structured counts.
    res = b["results"]["quotes_us"]
    assert res["run_id"] == b["run_ids"][0]
    assert res["status"] == "partial"
    assert "AAPL" in res["held_failed"]
    assert res["held_failed"] == sorted(res["held_failed"])
    assert set(res["fx_failed"]) == {"USDTWD", "USDMYR", "MYRTWD"}
    # The run row and the response say the same thing.
    row = golden_db.execute(
        "SELECT status, detail FROM job_runs WHERE id = ?", (res["run_id"],)
    ).fetchone()
    assert row["status"] == "partial"
    assert row["detail"] == res["detail"]
    assert "AAPL" in row["detail"] and "…" not in row["detail"]


def test_held_symbols_seam_reads_the_golden_positions(golden_db: sqlite3.Connection) -> None:
    held = held_symbols(golden_db)
    assert {"AAPL", "2330"} <= held


def test_status_endpoint_passes_partial_through(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """``last_run.status`` is raw so the chip can render 部分; ``ok`` stays False — a
    partial run is not a success (owner ruling), the RENDERER distinguishes it from 失敗."""
    golden_db.execute(
        "INSERT INTO job_runs (job_id, started_at, finished_at, status, detail) "
        "VALUES ('quotes_us','2026-06-10T17:30:06+08:00',"
        "'2026-06-10T17:30:36+08:00','partial','1 ok, 1 failed failed: AAPL')"
    )
    golden_db.commit()
    us = api_client.get("/api/scheduler/status").json()["jobs"]["quotes_us"]
    assert us["last_run"]["status"] == "partial"
    assert us["last_run"]["ok"] is False
    assert us["last_run"]["message"] == "1 ok, 1 failed failed: AAPL"


def test_runs_endpoint_passes_partial_through(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    golden_db.execute(
        "INSERT INTO job_runs (job_id, started_at, finished_at, status, detail) "
        "VALUES ('quotes_us','2026-06-10T17:30:06+08:00',"
        "'2026-06-10T17:30:36+08:00','partial','1 ok, 1 failed failed: AAPL')"
    )
    golden_db.commit()
    rows = api_client.get("/api/scheduler/runs", params={"job_id": "quotes_us"}).json()["rows"]
    assert rows[0]["status"] == "partial"


def test_app_wires_the_held_symbols_seam() -> None:
    """D39's lesson: a seam nobody registers is a seam nobody notices — and unregistered,
    the verdict over-reports (every instrument counts). Pin the wiring line."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2]
           / "portfolio_dash" / "api" / "app.py").read_text(encoding="utf-8")
    assert "register_held_symbols_fn(actions.held_symbols)" in src
