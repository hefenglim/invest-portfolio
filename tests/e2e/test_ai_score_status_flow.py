"""E2E: the AI 戰績 rows say what they ARE (AI-D26) — pending/undetermined ≠ 「✓ 命中」.

Drives the REAL stack (uvicorn subprocess + SQLite + served web/) with only
``/api/ai-score`` stubbed by ``page.route`` (a canned three-row payload: one scored hit,
one pending_data, one undetermined). ``_row_wire`` has always carried ``status``; the
defect was that ``renderScoreRows`` never read it, so an unscored row (miss=0) painted as
「✓ 命中」 — the page reported a better record than the ledger held.

Asserts: the three rows render as 「✓ 命中」/「… 待資料」/「— 未定」 respectively, and
EXACTLY ONE cell claims a hit (pre-fix: three).
"""

import json
from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory

_ROW_BASE = {
    "calibration_version": 1,
    "is_shadow": False,
    "narrative_score": None,
    "actual_value": None,
    "confidence": 70,
    "evaluated_at": "2026-06-14T18:00:00",
}

_SCORE = json.dumps({
    "totals": {"n": 1, "miss_count": 0, "miss_rate": "0",
               "quant_hit_count": 1, "quant_hit_rate": "1", "avg_narrative": None},
    "by_combo": [],
    "calibration_bins": [],
    "rows": [
        {**_ROW_BASE, "id": 3, "insight_id": 30, "insight_type_id": 10,
         "status": "undetermined", "quant_hit": None, "miss": False},
        {**_ROW_BASE, "id": 2, "insight_id": 29, "insight_type_id": 10,
         "status": "pending_data", "quant_hit": None, "miss": False},
        {**_ROW_BASE, "id": 1, "insight_id": 28, "insight_type_id": 10,
         "status": "scored", "quant_hit": True, "miss": False},
    ],
    "rows_total_count": 3,
})


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    """Re-enable loopback sockets PER TEST (pytest-socket re-bans before every test); the
    flow server spawns a fresh isolated uvicorn (free-port probe + readiness poll)."""
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


@pytest.mark.e2e
def test_score_rows_render_their_true_status(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_golden)
    page = fresh_page
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(getattr(m, "text", ""))
            if getattr(m, "type", None) == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    page.route("**/api/ai-score*",
               lambda route: route.fulfill(status=200, content_type="application/json",
                                           body=_SCORE))

    page.goto(base + "/insights.html", wait_until="load")
    page.click('button[data-tab="score"]')
    rows = page.locator("#score-body tr")
    expect(rows).to_have_count(3)

    # Each row says what it IS — the two unscored rows wear status chips, not a hit.
    expect(rows.nth(0)).to_contain_text("— 未定")
    expect(rows.nth(1)).to_contain_text("… 待資料")
    expect(rows.nth(2)).to_contain_text("✓ 命中")
    # Pre-fix all THREE rows claimed a hit (miss=0 → "✓ 命中"); exactly one may.
    expect(page.locator("#score-body td", has_text="✓ 命中")).to_have_count(1)

    assert not console_errors and not page_errors, (
        f"AI score status rows: console={console_errors!r} page={page_errors!r}")
