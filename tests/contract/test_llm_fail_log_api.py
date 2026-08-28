"""The failure-log admin surface: counts, .jsonl export, clear (AI-D64).

The load-bearing test here is `test_clearing_never_touches_the_billing_record`. It repeats
at the HTTP layer what `tests/shared/test_llm_fail_log.py` pins at the store, because the
two failures are different: the store test catches a bad `DELETE`, this one catches a route
that clears "the AI records" a little too helpfully. Remaining budget is
`sum(topups) - sum(llm_usage.cost)`, so reaching `llm_usage` would hand back spent money.
"""

import json
import sqlite3
from decimal import Decimal

from fastapi.testclient import TestClient

from portfolio_dash.shared import llm_fail_log as fl
from portfolio_dash.shared.llm_config import add_topup, budget_remaining


def _seed(conn: sqlite3.Connection, n: int = 3, agent: str = "ai_agents_input") -> None:
    fl.ensure_table(conn)
    for i in range(n):
        fl.record(
            conn, agent=agent, outcome="invalid_json", model="m",
            prompt=f"prompt {i}", raw_output=f"reply {i}", error_reason="boom",
        )


def test_summary_is_empty_and_honest_before_anything_fails(
    api_client: TestClient,
) -> None:
    body = api_client.get("/api/llm-fail-log").json()
    assert body["total"] == 0
    assert body["by_agent"] == []
    assert body["recent"] == []
    assert body["capacity"] > 0  # the ring's size is disclosed, not implied


def test_summary_counts_by_agent_and_reports_the_oldest(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _seed(golden_db, 3, "ai_agents_input")
    _seed(golden_db, 1, "news_organize")
    body = api_client.get("/api/llm-fail-log").json()
    assert body["total"] == 4
    counts = {r["agent"]: r["n"] for r in body["by_agent"]}
    assert counts == {"ai_agents_input": 3, "news_organize": 1}
    assert all(r["oldest"] for r in body["by_agent"])


def test_the_preview_is_trimmed_but_says_how_much_it_trimmed(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """The panel must not ship a 16 KiB prompt per row — but it must not lie about size."""
    fl.ensure_table(golden_db)
    fl.record(golden_db, agent="a", outcome="invalid_json", prompt="x" * 5000)
    row = api_client.get("/api/llm-fail-log").json()["recent"][0]
    assert len(row["prompt"]) == 400
    assert row["prompt_len"] == 5000


def test_export_is_valid_jsonl_with_every_field_on_every_line(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _seed(golden_db, 2)
    r = api_client.post("/api/llm-fail-log/export")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")
    assert "llm_fail_log_" in r.headers["content-disposition"]
    assert ".jsonl" in r.headers["content-disposition"]
    lines = r.text.splitlines()
    assert len(lines) == 2
    for line in lines:
        rec = json.loads(line)
        assert set(fl._COLUMNS) <= set(rec)


def test_export_carries_the_FULL_prompt_not_the_preview(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """The export is the fine-tuning artefact; truncating it would defeat the purpose."""
    fl.ensure_table(golden_db)
    fl.record(golden_db, agent="a", outcome="invalid_json", prompt="y" * 5000)
    rec = json.loads(api_client.post("/api/llm-fail-log/export").text.splitlines()[0])
    assert len(rec["prompt"]) == 5000


def test_export_of_an_empty_log_is_an_empty_file_not_an_error(
    api_client: TestClient,
) -> None:
    r = api_client.post("/api/llm-fail-log/export")
    assert r.status_code == 200
    assert r.text == ""


def test_export_and_clear_can_be_scoped_to_one_agent(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _seed(golden_db, 2, "ai_agents_input")
    _seed(golden_db, 3, "news_organize")
    only = api_client.post("/api/llm-fail-log/export?agent=news_organize").text
    assert len(only.splitlines()) == 3

    r = api_client.request("DELETE", "/api/llm-fail-log?agent=news_organize")
    assert r.json() == {"ok": True, "deleted": 3}
    assert api_client.get("/api/llm-fail-log").json()["total"] == 2


def test_clear_empties_the_log(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _seed(golden_db, 4)
    assert api_client.request("DELETE", "/api/llm-fail-log").json()["deleted"] == 4
    assert api_client.get("/api/llm-fail-log").json()["total"] == 0


def test_clearing_never_touches_the_billing_record(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """AI-D64's red line, pinned a second time at the route.

    A clear that reached `llm_usage` would raise remaining budget — spent money handed
    back — and nothing else in the app would notice.
    """
    _seed(golden_db, 3)
    golden_db.execute(
        "INSERT INTO llm_usage (ts, model, agent, input_tokens, output_tokens, cost, "
        "cache_tokens) VALUES ('2026-08-28T00:00:00+08:00','m','a',10,5,'0.25',0)"
    )
    add_topup(golden_db, Decimal("10"), note="test")
    golden_db.commit()

    before_budget = budget_remaining(golden_db)
    before_usage = golden_db.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0]

    assert api_client.request("DELETE", "/api/llm-fail-log").json()["ok"] is True

    assert budget_remaining(golden_db) == before_budget
    assert golden_db.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0] == before_usage
