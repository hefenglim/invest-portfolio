"""E2E — DEF-081 (owner ruling 2026-09-26): the AI 戰績 tab names each task by its name.

Real server, real database, nothing stubbed: a task and two scored evaluations are written
to the golden ledger, and the page must print the task's name — in 「各洞察任務命中率」 and in
預測明細 — where it printed 「任務 #N」, with the id kept in the tooltip.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal

import pytest
from playwright.sync_api import Page, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.llm_insight import evaluations_store as es
from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory

_NAME = "個股健檢（戰績測試）"


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _seed_scored_task(conn: sqlite3.Connection) -> None:
    _seed_golden(conn)
    now = datetime(2026, 6, 11, 14, 30)
    cs.ensure_seeded(conn)
    es.ensure_tables(conn)
    task = cs.create_insight_type(conn, name=_NAME, scope="per_symbol", now=now)
    for insight_id, hit in ((1, True), (2, False)):
        es.add_evaluation(conn, insight_id=insight_id, insight_type_id=task.id,
                          calibration_version=None, is_shadow=False, status="scored",
                          quant_hit=hit, narrative_score=70, miss=not hit,
                          actual_value=Decimal("0.01"), confidence=70, now=now)
    conn.commit()


@pytest.mark.e2e
def test_the_track_record_prints_task_names_not_ids(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_scored_task)
    page = fresh_page
    page_errors: list[str] = []
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    page.goto(base + "/insights.html", wait_until="load")
    page.click('button[data-tab="score"]')

    combo = page.locator("#combo-rows .strat-row .name")
    expect(combo).to_have_count(1)
    expect(combo).to_have_text(_NAME)
    expect(combo).to_be_visible()
    rows = page.locator("#score-body tr")
    expect(rows).to_have_count(2)
    for i in range(2):
        first = rows.nth(i).locator("td").first
        expect(first).to_have_text(_NAME)
        assert (first.get_attribute("title") or "").startswith("任務 #")
    body = page.locator("#combo-rows, #score-body").all_inner_texts()
    assert not any("任務 #" in t for t in body), body
    assert not page_errors, page_errors
