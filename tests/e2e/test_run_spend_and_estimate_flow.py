"""E2E: the scheduler history prints what a run spent; the request ledger marks an estimate.

Owner ruling 2026-09-30 on the verifier's R10 observations ③ and ④. Before the fix the
排程中心 run history printed — for ``evaluate_insights`` #212 (which had spent $0.0021532),
and the 設定 › AI request ledger printed 「0 · 0 · $0.0000」 for every reply gemini cut off.

This drives the REAL pages on a flow server whose database holds REAL rows: the scoring run
goes through ``scheduler.jobs.run_job`` with a runner that books two ``master_score`` calls,
and the ledger rows are written by ``shared.llm.log_usage`` — nothing is canned.
"""

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import Page
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.scheduler import jobs
from portfolio_dash.shared import llm
from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory

_NOW = datetime(2026, 9, 28, 17, 25, tzinfo=ZoneInfo("Asia/Taipei"))
_GEMINI = "google/gemini-2.5-flash-lite"


def _scoring(conn: sqlite3.Connection, *, now: datetime) -> str:
    for tin, tout, cost in ((1715, 245, "0.0012"), (1703, 198, "0.0009532")):
        llm.log_usage(conn, model=_GEMINI, agent="master_score", input_tokens=tin,
                      output_tokens=tout, cost=Decimal(cost), estimated=False)
    return "評分 2 張、延後 0 張；晉升：無"


def _seed(conn: sqlite3.Connection) -> None:
    _seed_golden(conn)
    jobs.create_scheduler_tables(conn)
    jobs.register_evaluation_runner(_scoring)
    try:
        jobs.run_job(conn, "evaluate_insights", now=_NOW)
    finally:
        jobs.register_evaluation_runner(None)
    # #519's shape last, so it is the newest row: a reply cut off with no usage block.
    llm.log_usage(conn, model=_GEMINI, agent="insight_generate", input_tokens=3512,
                  output_tokens=14, cost=Decimal("0.0003568"), estimated=True)


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


@pytest.mark.e2e
def test_the_history_prints_the_runs_spend_and_the_ledger_marks_an_estimate(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed)
    page = fresh_page
    page_errors: list[str] = []
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    page.goto(base + "/settings.html#scheduler", wait_until="load")
    page.wait_for_selector("#hist-body tr", state="attached")
    hist = page.evaluate("""() => [...document.querySelectorAll('#hist-body tr')].map((tr) => {
        const tds = tr.querySelectorAll('td');
        return { text: tr.textContent, cost: tds[tds.length - 1].textContent };
    })""")
    scoring = [h for h in hist if "評分 2 張" in h["text"]]
    assert [h["cost"] for h in scoring] == ["$0.002"], hist

    page.goto(base + "/settings.html#llm", wait_until="load")
    page.wait_for_selector("#req-body tr", state="attached")
    rows = page.evaluate("""() => {
        const probe = document.createElement('span');
        probe.className = 'badge';
        document.body.appendChild(probe);
        const neutral = getComputedStyle(probe).color;
        probe.remove();
        const amber = getComputedStyle(document.documentElement)
          .getPropertyValue('--amber').trim();
        return { neutral, amber, rows: [...document.querySelectorAll('#req-body tr')].map((tr) => {
          const tag = tr.querySelector('.usage-est');
          return { agent: tr.children[2].textContent, tokens_in: tr.children[3].textContent,
                   tag: tag ? tag.textContent : null, title: tag ? tag.title : null,
                   color: tag ? getComputedStyle(tag).color : null };
        }) };
    }""")
    newest, *older = rows["rows"]
    assert newest["agent"] == "insight_generate" and newest["tokens_in"] == "3,512", rows
    assert newest["tag"] == "估算", rows
    assert "未回報用量" in (newest["title"] or ""), rows
    # Provenance, not a warning: the neutral badge colour, never the amber of 過期.
    assert newest["color"] == rows["neutral"], rows
    assert [r["tag"] for r in older] == [None] * len(older), rows
    assert page_errors == []
