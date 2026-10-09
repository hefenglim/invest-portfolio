"""DEF-058 (functional test H-04, R3): the 洞察管線 header's 「最近批次」 counts cards and cost
over the SAME set of runs.

Measured on the demo: 排程中心 alert_scan 立即執行 (#185) dispatched insight:9 seven times
(runs #186-#192, one per alert event, all stamped with the scan's one ``now``; together
$0.0050487). The header read 「最近批次 2026-09-24 21:40・7 卡 本批成本 $0.001」 and
``GET /api/insight-tasks/status`` said ``last_batch {cards 7, cost_usd "0.0005979"}``: the
cards were counted over every card created at that ``started_at`` while the cost came from
``ORDER BY id DESC LIMIT 1`` — the last of the seven runs.

A batch is now defined once — the finished non-shadow insight runs sharing a ``started_at`` —
and both figures are taken over it. The pre-existing ``test_status_last_run_and_last_batch``
seeded ONE run, where "the last run" and "the batch" are the same set, so the two readings
could never disagree there: that is why it never caught this.
"""

import sqlite3
from decimal import Decimal

from fastapi.testclient import TestClient

T0 = "2026-06-11T08:00:00+08:00"
T_EARLIER = "2026-06-10T08:00:00+08:00"


def _task(api_client: TestClient) -> int:
    sp = api_client.post(
        "/api/strategy-prompts", json={"name": "S", "body": "{{kpis_json}}"}
    ).json()
    it = api_client.post(
        "/api/insight-types",
        json={"name": "Alert", "scope": "portfolio", "strategy_ids": [sp["id"]]},
    ).json()
    return int(it["id"])


def _run(conn: sqlite3.Connection, tid: int, *, started: str, finished: str | None,
         cost: str | None, shadow: int = 0) -> None:
    conn.execute(
        "INSERT INTO job_runs (job_id, started_at, finished_at, status, detail, payload, "
        "cost_usd, is_shadow) VALUES (?, ?, ?, 'ok', 'done', ?, ?, ?)",
        (f"insight:{tid}", started, finished, str(tid), cost, shadow),
    )


def _card(conn: sqlite3.Connection, tid: int, *, created: str, shadow: int = 0) -> None:
    conn.execute(
        "INSERT INTO insights (insight_type_id, symbol, is_shadow, calibration_version, "
        "fingerprint, title, summary, body_md, tags, confidence, prediction, horizon_days, "
        "due_at, input_snapshot, model, cost_usd, created_at) VALUES "
        "(?, NULL, ?, NULL, 'fp', 't', 's', 'b', '[]', NULL, NULL, 5, NULL, '{}', 'm', "
        "'0', ?)",
        (tid, shadow, created),
    )


def test_cards_and_cost_are_summed_over_the_one_batch(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """The verifier's shape: several runs of one dispatch, one ``started_at``."""
    tid = _task(api_client)
    # an OLDER batch — must not leak into the latest one
    _run(golden_db, tid, started=T_EARLIER, finished="2026-06-10T08:00:03+08:00", cost="0.5")
    _card(golden_db, tid, created=T_EARLIER)
    # the latest batch: two runs with one started_at, 0.001 + 0.002
    _run(golden_db, tid, started=T0, finished="2026-06-11T08:00:04+08:00", cost="0.001")
    _run(golden_db, tid, started=T0, finished="2026-06-11T08:00:09+08:00", cost="0.002")
    _card(golden_db, tid, created=T0)
    _card(golden_db, tid, created=T0)
    # a SHADOW run + card at the same instant — never part of the user-facing batch
    _run(golden_db, tid, started=T0, finished="2026-06-11T08:00:12+08:00", cost="0.7",
         shadow=1)
    _card(golden_db, tid, created=T0, shadow=1)
    golden_db.commit()
    lb = api_client.get("/api/insight-tasks/status").json()["health"]["last_batch"]
    assert lb["cost_usd"] == "0.003", lb  # Decimal STRING, summed — not the last run's 0.002
    assert Decimal(lb["cost_usd"]) == Decimal("0.001") + Decimal("0.002")
    assert lb["cards"] == 2
    assert lb["runs"] == 2
    assert lb["at"] == "2026-06-11T08:00:09+08:00"  # the batch's LAST finish


def test_a_run_still_running_or_without_a_cost_does_not_break_the_sum(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """Pre-existing-state variants: an unfinished sibling (NULL finished_at) is not yet part
    of the finished batch; a finished run with no recorded cost adds 0, never an error."""
    tid = _task(api_client)
    _run(golden_db, tid, started=T0, finished="2026-06-11T08:00:04+08:00", cost="0.0005979")
    _run(golden_db, tid, started=T0, finished="2026-06-11T08:00:05+08:00", cost=None)
    _run(golden_db, tid, started=T0, finished=None, cost=None)  # still running
    golden_db.commit()
    lb = api_client.get("/api/insight-tasks/status").json()["health"]["last_batch"]
    assert lb["cost_usd"] == "0.0005979" and lb["runs"] == 2


def test_the_seven_run_dispatch_reads_its_real_total(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """The demo's shape: seven runs, seven cards, $0.0050487 in all, the LAST run $0.0005979
    (both measured). The split of the other six is illustrative — only the total was read."""
    tid = _task(api_client)
    costs = ["0.0006612", "0.0007431", "0.0007204", "0.0007188", "0.0007393", "0.0008680",
             "0.0005979"]
    for i, c in enumerate(costs):
        _run(golden_db, tid, started=T0, finished=f"2026-06-11T08:00:{10 + i}+08:00", cost=c)
        _card(golden_db, tid, created=T0)
    golden_db.commit()
    lb = api_client.get("/api/insight-tasks/status").json()["health"]["last_batch"]
    assert Decimal(lb["cost_usd"]) == sum((Decimal(c) for c in costs), Decimal("0"))
    assert lb["cost_usd"] == "0.0050487"
    assert lb["cards"] == 7 and lb["runs"] == 7
