"""PERF plan W4 / spec §12.1: once the LAST write window of a burst closes, today's dashboard and
alerts are pre-warmed into the result cache, so the page opened after 儲存 is a cache hit.

The write-window middleware (``api/app.py``) schedules ``alert_inputs.prewarm_today`` through
``result_cache.schedule_prewarm`` (debounced, off unless ``PD_PREWARM=1``) after every request
that is not a read — and never after a GET or a declared read-only POST (``api/read_only.py``),
which write nothing.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.api import alert_inputs
from portfolio_dash.api.app import WRITE_PREWARM_DELAY_S
from portfolio_dash.shared import result_cache


@pytest.fixture
def scheduled(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Callable[[], object], float]]:
    calls: list[tuple[Callable[[], object], float]] = []

    def record(fn: Callable[[], object], delay: float) -> bool:
        calls.append((fn, delay))
        return True

    monkeypatch.setattr(result_cache, "schedule_prewarm", record)
    return calls


def test_a_write_schedules_the_prewarm_three_seconds_after_it_closes(
    api_client: TestClient, scheduled: list[tuple[Callable[[], object], float]],
) -> None:
    # any mutating method opens and closes a write window, whatever its route answers
    assert api_client.post("/api/w4-probe-no-such-route").status_code in (404, 405)
    assert scheduled == [(alert_inputs.prewarm_today, WRITE_PREWARM_DELAY_S)]
    assert WRITE_PREWARM_DELAY_S == 3.0


def test_a_read_schedules_nothing(
    api_client: TestClient, scheduled: list[tuple[Callable[[], object], float]],
) -> None:
    assert api_client.get("/api/health").status_code == 200
    api_client.post("/api/whatif", json={})  # a declared read-only POST
    assert scheduled == []


def test_the_real_schedule_is_debounced_and_off_by_default() -> None:
    """The unit-test default is ``PD_PREWARM=0``: nothing is scheduled, nothing runs."""
    assert result_cache.schedule_prewarm(lambda: None, WRITE_PREWARM_DELAY_S) is False
