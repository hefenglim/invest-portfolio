"""M7-07: `/api/insight-tasks/status` must not replay the whole book for nothing.

`build_status` read the dashboard only inside `_gather_facts`, once per task — so with
zero tasks it read it zero times, while building it unconditionally beforehand. Measured
on the QA fixture: a 124-byte response cost 1,939 ms, of which `build_dashboard` was
1,401 ms, and 87% of THAT was `daily_value_series` replaying the book once per day
(`build_book` called 2,053 times).

The fix moves construction to first use. Same function, same arguments, same connection
snapshot — only the evaluation ORDER changes, so these tests pin BOTH halves: the call
count AND that the payload is byte-identical to an eagerly-built one.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.api import insight_service
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.portfolio.dashboard_models import DashboardData


def _count_builds(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls = [0]
    real = build_dashboard  # the same object insight_service imported

    def counted(*a: Any, **kw: Any) -> Any:
        calls[0] += 1
        return real(*a, **kw)

    monkeypatch.setattr(insight_service, "build_dashboard", counted)
    return calls


def test_zero_tasks_never_builds_the_dashboard(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: no task asked for it, so nobody pays for it."""
    calls = _count_builds(monkeypatch)
    r = api_client.get("/api/insight-tasks/status")
    assert r.status_code == 200
    assert r.json()["tasks"] == []
    assert calls[0] == 0, f"built the dashboard {calls[0]}x for a task list of zero"


def test_one_task_builds_it_exactly_once(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lazy, not memo-less: N tasks must still share ONE build, not trigger N of them."""
    sp = api_client.post(
        "/api/strategy-prompts", json={"name": "S", "body": "{{kpis_json}}"}
    ).json()
    for name in ("A", "B", "C"):
        api_client.post(
            "/api/insight-types",
            json={"name": name, "scope": "portfolio", "strategy_ids": [sp["id"]]},
        )
    calls = _count_builds(monkeypatch)
    r = api_client.get("/api/insight-tasks/status")
    assert r.status_code == 200
    assert len(r.json()["tasks"]) == 3
    assert calls[0] == 1, f"three tasks triggered {calls[0]} dashboard builds"


def test_the_lazily_built_data_actually_reaches_gather_facts(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure mode a lazy refactor invites: the loop runs, but on a `None`.

    `_gather_facts` types `data` as `DashboardData`, so passing `None` would not raise at
    the boundary — it would surface as an AttributeError deep inside, or worse, as a task
    card derived from nothing. Spy on the argument itself.
    """
    sp = api_client.post(
        "/api/strategy-prompts", json={"name": "S", "body": "{{kpis_json}}"}
    ).json()
    api_client.post(
        "/api/insight-types",
        json={"name": "Daily", "scope": "portfolio", "strategy_ids": [sp["id"]]},
    )
    seen: list[Any] = []
    real = insight_service._gather_facts

    def spy(conn: Any, it: Any, data: Any, **kw: Any) -> Any:
        seen.append(data)
        return real(conn, it, data, **kw)

    monkeypatch.setattr(insight_service, "_gather_facts", spy)
    r = api_client.get("/api/insight-tasks/status")
    assert r.status_code == 200
    assert len(seen) == 1
    assert seen[0] is not None, "the loop ran on a None dashboard"
    assert isinstance(seen[0], DashboardData)
