"""A run's recorded cost is what the run spent — every run, through every door.

Owner ruling 2026-09-30 on the verifier's R10 observation ③ (「evaluate_insights #212 以大師
模型評分 10 張花費 $0.0021532，但該 job_runs 列 cost_usd 為空」). Root cause (91ec1b0):

* static jobs never wrote their spend. ``scheduler/jobs.py:1876`` (``run_job_outcome``) and
  ``:1915`` (``finish_job_run``, used by ``run_job_func`` and the manual news worker
  ``api/routers/news.py:143``) set ``finished_at / status / detail`` only. The spend was
  re-derived at READ time for one popover (``api/routers/scheduler.py:364-470``: the five
  LLM jobs' agents summed inside the run's time window), while the run history
  (``_run_row``, ``cost_usd: row["cost_usd"]``) printed — for the same run;
* insight runs added up the cards they PRODUCED (``llm_insight/generate.py:483-485``,
  ``total_cost += completion.cost``): a retry after a broken reply, or a primary model
  that failed before the fallback answered, was paid for and left off the run — and off
  the mid-run budget check that shares the variable.

The fix makes the ledger the one source: ``shared.llm.usage_tally`` is opened around a
run's work and ``log_usage`` (the one writer of ``llm_usage``) adds every row to the
innermost open tally; the run row records the tally's cost, calls and tokens.

Why no test caught it: the popover and the history were each pinned on their own (the
window sum by ``test_scheduler_api``, the history by nothing), and every insight test's fake
provider answered on the first try, so "sum of the cards" and "sum of the calls" never
differed. The tests below compare a run's row with the ``llm_usage`` rows it wrote.
"""

import ast
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

import portfolio_dash
from portfolio_dash.api import news_service
from portfolio_dash.api.routers import news as news_router
from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.llm_insight import generate
from portfolio_dash.llm_insight import insights_store as istore
from portfolio_dash.llm_insight import variables as V
from portfolio_dash.llm_insight.generate import RunInputs
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.scheduler import jobs
from portfolio_dash.shared import llm as llm_mod
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.llm_config import (
    LLMRole,
    ModelConfig,
    add_topup,
    ensure_llm_seeded,
    set_role,
    upsert_model,
)

_PKG = Path(portfolio_dash.__file__).resolve().parent
NOW = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))


@pytest.fixture
def conn(golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
         ) -> Iterator[sqlite3.Connection]:
    @contextmanager
    def _session() -> Iterator[sqlite3.Connection]:
        yield golden_db

    monkeypatch.setattr(jobs, "session", _session)
    monkeypatch.setattr(news_router, "session", _session)
    yield golden_db
    jobs.register_evaluation_runner(None)


def _bill(conn: sqlite3.Connection, agent: str, cost: str, tin: int, tout: int) -> None:
    llm_mod.log_usage(conn, model="google/gemini-2.5-flash-lite", agent=agent,
                      input_tokens=tin, output_tokens=tout, cost=Decimal(cost))


def _scoring_runner(conn: sqlite3.Connection, *, now: datetime) -> str:
    """#212's shape: the master model scores two due cards."""
    _bill(conn, "master_score", "0.0012", 1715, 245)
    _bill(conn, "master_score", "0.0009532", 1703, 198)
    return "評分 2 張、延後 0 張；晉升：無"


def _spend(conn: sqlite3.Connection, run_id: int) -> tuple[Any, ...]:
    row = conn.execute(
        "SELECT cost_usd, llm_calls, tokens_in, tokens_out FROM job_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    return tuple(row)


def test_the_scheduled_scoring_run_records_what_it_spent(
    conn: sqlite3.Connection, api_client: TestClient
) -> None:
    jobs.register_evaluation_runner(_scoring_runner)
    run_id = jobs.run_job(conn, "evaluate_insights", now=NOW)
    assert _spend(conn, run_id) == ("0.0021532", 2, 3418, 443)
    # The run history and the status popover print the SAME number for the same run.
    history = api_client.get("/api/scheduler/runs", params={"job_id": "evaluate_insights"})
    assert [r["cost_usd"] for r in history.json()["rows"]] == ["0.0021532"]
    status = api_client.get("/api/scheduler/status").json()["jobs"]["evaluate_insights"]
    assert status["last_run"]["cost"] == {
        "cost_usd": "0.0021532", "tokens_in": 3418, "tokens_out": 443, "calls": 2,
        "source": "run_row",
    }


def test_the_manual_run_door_records_it_too(conn: sqlite3.Connection) -> None:
    jobs.register_evaluation_runner(_scoring_runner)
    run_id = jobs.start_job_run(conn, "evaluate_insights", now=NOW)
    jobs.run_job_func("evaluate_insights", now=NOW)
    assert _spend(conn, run_id) == ("0.0021532", 2, 3418, 443)


def test_the_manual_news_door_records_it_too(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def organize(c: sqlite3.Connection, universe: Any, *, now: datetime) -> dict[str, Any]:
        _bill(c, "news_organize", "0.0004", 900, 120)
        return {"organized": 1, "headline": 0, "skipped": 0}

    monkeypatch.setattr(news_service, "run_news_for", organize)
    run_id = jobs.start_job_run(conn, "news_daily", now=NOW)
    news_router._news_run_worker([("2884", "TW")], now=NOW, job_id="news_daily")
    assert _spend(conn, run_id) == ("0.0004", 1, 900, 120)


def test_a_run_that_made_no_ai_call_records_no_spend(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NULL — 「沒有 AI」, printed as — — never a $0 that reads as a free call."""
    monkeypatch.setattr(jobs, "_jobs_by_id", lambda: {
        "plain": jobs.JobSpec("plain", lambda c, *, now: "完成", "0 8 * * *", "Asia/Taipei",
                              True, "d"),
    })
    run_id = jobs.run_job(conn, "plain", now=NOW)
    assert _spend(conn, run_id) == (None, None, None, None)


# --- insight runs: the retry and the failed model are part of the run ---------------------

_CUT = '{"title":"洞察","summary":"量'
_CARD = (
    '{"title":"洞察","summary":"量縮","body_md":"**2330** 量縮整理。","tags":["TW"],'
    '"symbol":null,"confidence":70,"prediction":null}'
)


class _Resp:
    def __init__(self, content: str) -> None:
        self.choices = [type("M", (), {"message": type("X", (), {"content": content})()})()]
        self.usage = type("U", (), {"prompt_tokens": 1000, "completion_tokens": 100})()


@pytest.fixture
def insight_conn(conn: sqlite3.Connection) -> sqlite3.Connection:
    cs.ensure_seeded(conn)
    istore.ensure_tables(conn)
    ensure_llm_seeded(conn)
    for alias, price in (("gemini-m", "0.1"), ("haiku-m", "1")):
        upsert_model(conn, ModelConfig(
            id=alias, model_alias=alias, provider="openai", model_name=alias, max_retries=0,
            input_price_per_mtok=Decimal(price), output_price_per_mtok=Decimal(price) * 4,
        ))
    set_role(conn, LLMRole.DEFAULT, "gemini-m")
    set_role(conn, LLMRole.DEFAULT_FALLBACK, "haiku-m")
    add_topup(conn, Decimal("100"))
    return conn


def _combo(conn: sqlite3.Connection) -> int:
    sp = cs.create_strategy(conn, name="S", body="觀察 {{kpis_json}}", now=NOW)
    it = cs.create_insight_type(conn, name="Daily", scope="portfolio", now=NOW)
    cs.set_strategies(conn, it.id, [(sp.id, 0)])
    return it.id


def _run(conn: sqlite3.Connection, it_id: int) -> generate.RunResult:
    data = build_dashboard(conn, now=NOW, reporting=Currency.TWD)
    return generate.run_insight_type(
        conn, it_id, var_contexts={None: V.VarContext(data=data, now=NOW, symbol=None)},
        inputs=RunInputs(budget_remaining=Decimal("100")), now=NOW,
    )


def test_an_insight_run_counts_the_broken_replies_and_the_failed_model(
    insight_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G-09 / #221 replayed: gemini cut off twice, haiku answered. Three calls were paid."""
    replies = {"gemini-m": [_CUT, _CUT], "haiku-m": [_CARD]}
    monkeypatch.setattr(llm_mod.litellm, "supports_response_schema", lambda **kw: False)
    monkeypatch.setattr(llm_mod.litellm, "completion",
                        lambda **kw: _Resp(replies[str(kw["model"]).rsplit("/", 1)[-1]].pop(0)))
    it_id = _combo(insight_conn)
    result = _run(insight_conn, it_id)
    usage = insight_conn.execute("SELECT model, cost FROM llm_usage ORDER BY id").fetchall()
    assert [u["model"] for u in usage] == ["gemini-m", "gemini-m", "haiku-m"]
    paid = sum(Decimal(u["cost"]) for u in usage)
    row = insight_conn.execute(
        "SELECT id FROM job_runs WHERE job_id = ?", (f"insight:{it_id}",)).fetchone()
    spend = _spend(insight_conn, row["id"])
    assert Decimal(spend[0]) == paid and spend[1:] == (3, 3000, 300)
    assert result.cost_usd == paid
    # The CARD still carries only the call that produced it (its model and tokens beside it).
    [card] = istore.list_cards(insight_conn, insight_type_id=it_id)
    assert Decimal(card.cost_usd) == Decimal(usage[2]["cost"]) < paid


def test_a_run_started_inside_another_run_is_counted_once(
    insight_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``alert_scan`` dispatches on_alert tasks: each writes its own row with its own spend,
    so the job that started them records none of it — one spend, one row."""
    monkeypatch.setattr(llm_mod.litellm, "supports_response_schema", lambda **kw: False)
    monkeypatch.setattr(llm_mod.litellm, "completion", lambda **kw: _Resp(_CARD))
    it_id = _combo(insight_conn)

    def scan(c: sqlite3.Connection, *, now: datetime) -> str:
        _run(c, it_id)
        return "派發 1 張"

    monkeypatch.setattr(jobs, "_jobs_by_id", lambda: {
        "scan": jobs.JobSpec("scan", scan, "0 8 * * *", "Asia/Taipei", True, "d"),
    })
    scan_id = jobs.run_job(insight_conn, "scan", now=NOW)
    assert _spend(insight_conn, scan_id) == (None, None, None, None)
    row = insight_conn.execute(
        "SELECT id FROM job_runs WHERE job_id = ?", (f"insight:{it_id}",)).fetchone()
    assert _spend(insight_conn, row["id"])[1] == 1


# --- the tally itself ---------------------------------------------------------------------

def test_the_innermost_tally_counts_and_the_outer_resumes(
    insight_conn: sqlite3.Connection,
) -> None:
    with llm_mod.usage_tally() as outer:
        _bill(insight_conn, "a", "1", 1, 1)
        with llm_mod.usage_tally() as inner:
            _bill(insight_conn, "b", "2", 2, 2)
        _bill(insight_conn, "c", "4", 4, 4)
    _bill(insight_conn, "d", "8", 8, 8)  # no tally open: counted nowhere, still booked
    assert (outer.calls, outer.cost, outer.tokens_in) == (2, Decimal("5"), 5)
    assert (inner.calls, inner.cost) == (1, Decimal("2"))


def test_a_thread_starts_with_no_tally(insight_conn: sqlite3.Connection) -> None:
    """A worker thread is its own run: it never books into the request's tally."""
    seen: list[object] = []
    with llm_mod.usage_tally() as mine:
        t = threading.Thread(target=lambda: seen.append(llm_mod._ACTIVE_TALLY.get()))
        t.start()
        t.join()
    assert seen == [None] and mine.calls == 0


# --- the class: every door that closes a run row records the run's spend ------------------

def _py_files() -> list[Path]:
    return sorted(p for p in _PKG.rglob("*.py") if "__pycache__" not in p.parts)


def _enclosing(tree: ast.AST) -> dict[ast.AST, str]:
    owner: dict[ast.AST, str] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef):
            for node in ast.walk(fn):
                owner.setdefault(node, fn.name)
    return owner


#: Closers that run BEFORE any work or after the insight runner raised — each with why no
#: spend is passed. Anything else that closes a run row must pass ``usage=``.
_NO_WORK_CLOSERS = {
    # the run id names no known job: finished as an error before anything ran
    ("scheduler/jobs.py", "run_job_func"): 1,
    # no insight runner registered: nothing ran
    ("scheduler/jobs.py", "_execute_insight"): 1,
    # the insight runner RAISED (a code defect, not a provider failure — those are caught
    # inside the run and recorded with its spend): the tally that counted the run's calls
    # was the runner's own and is gone with the raise; the calls stay in llm_usage, so in
    # the quota and the per-agent totals
    ("scheduler/jobs.py", "_record_insight_failure"): 1,
}


def test_every_run_closer_passes_the_runs_spend() -> None:
    found: dict[tuple[str, str], int] = {}
    closers = 0
    for path in _py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        owner = _enclosing(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(
                node.func, "id", None)
            if name not in {"finish_job_run", "_finalize_run"}:
                continue
            closers += 1
            usage = next((k.value for k in node.keywords if k.arg == "usage"), None)
            if usage is None or (isinstance(usage, ast.Constant) and usage.value is None):
                key = (path.relative_to(_PKG).as_posix(), owner.get(node, "<module>"))
                found[key] = found.get(key, 0) + 1
    assert closers >= 7, closers  # found by scan: a rename must not empty the check
    assert found == _NO_WORK_CLOSERS


def _sql_literals(tree: ast.AST) -> list[tuple[int, str]]:
    """Every string constant, adjacent literals already joined by the parser."""
    return [(n.lineno, n.value) for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def test_every_statement_that_finishes_a_run_row_writes_its_cost() -> None:
    """An UPDATE or INSERT that sets ``finished_at`` on ``job_runs`` must also write
    ``cost_usd`` — except the one no-work branch above."""
    silent: list[str] = []
    finishing = 0
    for path in _py_files():
        for line, sql in _sql_literals(ast.parse(path.read_text(encoding="utf-8"))):
            flat = " ".join(sql.split())
            if "job_runs" not in flat or "finished_at" not in flat:
                continue
            if not (flat.startswith("UPDATE job_runs SET finished_at")
                    or flat.startswith("INSERT INTO job_runs")):
                continue
            finishing += 1
            if "cost_usd" not in flat:
                silent.append(f"{path.relative_to(_PKG).as_posix()}:{line}")
    assert finishing >= 5, finishing
    assert silent == ["scheduler/jobs.py:" + str(_finalize_no_usage_line())]


def _finalize_no_usage_line() -> int:
    src = (_PKG / "scheduler" / "jobs.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "_finalize_run")
    return next(line for line, sql in _sql_literals(fn)
                if "cost_usd" not in sql and "finished_at" in sql)
