"""E2E: clear-on-success + REAL per-row checkbox filtering for the AI and CSV panes (A5/C7).

Drives the REAL stack (uvicorn + SQLite + served web/). The AI panel's LLM seam is stubbed with
``page.route`` (the flow server has no provider): ``/api/input/ai/preview`` returns a canned
two-row preview + csv, ``/api/import/commit`` is canned + its request body captured, and (for the
partial path) ``/api/import/preview`` returns the re-validation of the committed csv.

Asserts the C7 contract:
  * AI — commit writes ONLY the CHECKED rows (an unchecked row's csv line is never sent);
  * AI — a FULL success (skipped == 0) clears the text / preview / csv / write button and shows
    the in-pane banner (`#ai-result`), so a double-submit is impossible;
  * AI — a PARTIAL success keeps ONLY the skipped rows visible and does NOT clear the text;
  * CSV — a full success clears the paste + shows the `#csv-result` banner (real preview/commit).

ZERO console / page errors throughout (every stub is a 200).
"""

import json
from collections.abc import Iterator
from typing import Any

import pytest
from playwright.sync_api import Page, Route, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory

# Two clean drafts (2330 is registered in the golden seed) with DISTINCT prices so the filtered
# commit body can be probed: 600 (row 0) must be sent, 610 (row 1) must NOT when row 1 is unchecked.
_AI_CSV = (
    "account,symbol,side,date,shares,price,note\n"
    "tw_broker,2330,buy,2026-06-01,1000,600,\n"
    "tw_broker,2330,buy,2026-06-02,500,610,\n"
)


def _row(n: int, status: str, price: str, dte: str, shares: str) -> dict[str, Any]:
    return {"n": n, "status": status, "reason": None if status == "ok" else "賣超風險",
            "code": None,
            "data": {"account_id": "tw_broker", "symbol": "2330", "side": "buy",
                     "trade_date": dte, "quantity": shares, "price": price}}


_AI_ROWS = [_row(0, "ok", "600", "2026-06-01", "1000"), _row(1, "ok", "610", "2026-06-02", "500")]


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    """Re-enable loopback sockets PER TEST (pytest-socket re-bans before every test); each flow
    spawns a fresh isolated uvicorn (free-port probe + readiness poll need loopback TCP)."""
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _sink(page: Page) -> tuple[list[str], list[str]]:
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(getattr(m, "text", ""))
            if getattr(m, "type", None) == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    return console_errors, page_errors


def _route_ai_preview(page: Page, csv_text: str, rows: list[dict[str, Any]]) -> None:
    body = {"rows": rows,
            "summary": {"total": len(rows), "ok": sum(1 for r in rows if r["status"] == "ok"),
                        "warn": 0, "error": 0},
            "meta": {"model": "stub", "via": "litellm", "cost_usd": None},
            "csv_text": csv_text}
    page.route("**/api/input/ai/preview",
               lambda route: route.fulfill(status=200, content_type="application/json",
                                           body=json.dumps(body)))


def _route_commit(page: Page, captured: list[str], resp: dict[str, int]) -> None:
    def _h(route: Route) -> None:
        captured.append(route.request.post_data or "")
        route.fulfill(status=200, content_type="application/json", body=json.dumps(resp))
    page.route("**/api/import/commit", _h)


def _route_import_preview(page: Page, rows: list[dict[str, Any]]) -> None:
    ok = sum(1 for r in rows if r["status"] == "ok")
    err = sum(1 for r in rows if r["status"] == "error")
    body = {"rows": rows, "summary": {"ok": ok, "warn": 0, "error": err}}
    page.route("**/api/import/preview",
               lambda route: route.fulfill(status=200, content_type="application/json",
                                           body=json.dumps(body)))


def _open_ai_tab(page: Page, base: str) -> None:
    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#csv-kinds .chip", state="attached")  # boot done
    page.click("#tab-ai")
    page.wait_for_selector("#ai-dropzone", state="visible")


def _parse(page: Page) -> None:
    page.fill("#ai-text", "在元大買 2330 兩筆")
    with page.expect_response("**/api/input/ai/preview"):
        page.click("#ai-parse")
    page.wait_for_selector("#ai-body tr")


@pytest.mark.e2e
def test_ai_full_success_clears_and_only_checked_row_written(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_golden)
    page = fresh_page
    console_errors, page_errors = _sink(page)
    committed: list[str] = []
    _route_ai_preview(page, _AI_CSV, _AI_ROWS)
    _route_commit(page, committed, {"written": 1, "skipped": 0})

    _open_ai_tab(page, base)
    _parse(page)
    expect(page.locator("#ai-body tr")).to_have_count(2)

    # uncheck row 2 (the 610 draft) — it must NOT reach the commit body.
    page.locator("#ai-body tr").nth(1).locator("input[type=checkbox]").uncheck()
    with page.expect_response("**/api/import/commit"):
        page.click("#ai-write-all")

    # REAL filtering: the committed csv carries the checked row (600) and NOT the unchecked (610).
    assert committed, "commit was not called"
    assert "600" in committed[0] and "610" not in committed[0], committed[0]

    # full success -> every AI input is wiped + the in-pane banner shows the summary.
    expect(page.locator("#ai-result")).to_be_visible()
    expect(page.locator("#ai-result")).to_contain_text("寫入完成")
    expect(page.locator("#ai-text")).to_have_value("")
    expect(page.locator("#ai-body tr")).to_have_count(0)
    expect(page.locator("#ai-write-all")).to_be_disabled()  # no double-submit possible

    assert not console_errors and not page_errors, (
        f"AI full-success flow: console={console_errors!r} page={page_errors!r}"
    )


@pytest.mark.e2e
def test_ai_partial_success_keeps_only_skipped_rows(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_golden)
    page = fresh_page
    console_errors, page_errors = _sink(page)
    committed: list[str] = []
    _route_ai_preview(page, _AI_CSV, _AI_ROWS)
    _route_commit(page, committed, {"written": 1, "skipped": 1})
    # the re-preview of the committed csv: row 0 wrote (ok), row 1 was skipped (error).
    _route_import_preview(page, [_row(0, "ok", "600", "2026-06-01", "1000"),
                                 _row(1, "error", "610", "2026-06-02", "500")])

    _open_ai_tab(page, base)
    _parse(page)
    expect(page.locator("#ai-body tr")).to_have_count(2)  # both checked by default

    with page.expect_response("**/api/import/commit"):
        page.click("#ai-write-all")

    # partial -> banner reports both counts; ONLY the skipped row remains; the text is NOT cleared.
    expect(page.locator("#ai-result")).to_contain_text("已寫入 1 筆")
    expect(page.locator("#ai-result")).to_contain_text("略過 1 筆")
    expect(page.locator("#ai-body tr")).to_have_count(1)
    expect(page.locator("#ai-text")).not_to_have_value("")

    assert not console_errors and not page_errors, (
        f"AI partial-success flow: console={console_errors!r} page={page_errors!r}"
    )


@pytest.mark.e2e
def test_csv_full_success_clears_paste(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """CSV pane, REAL preview/commit (2330 + tw_broker are seeded): a clean buy commits fully
    and the paste is cleared with the #csv-result banner (a second identical commit is blocked)."""
    base = flow_server(_seed_golden)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#csv-kinds .chip", state="attached")
    page.click("#tab-csv")
    page.wait_for_selector("#csv-paste", state="visible")

    page.fill("#csv-paste",
              "account,symbol,side,date,shares,price\ntw_broker,2330,buy,2026-06-01,1000,600")
    page.wait_for_selector("#csv-body tr")
    page.wait_for_function(
        "() => { const b = document.querySelector('#csv-confirm'); return b && !b.disabled; }")
    with page.expect_response("**/api/import/commit") as cm:
        page.click("#csv-confirm")
    assert cm.value.status == 200

    expect(page.locator("#csv-result")).to_be_visible()
    expect(page.locator("#csv-result")).to_contain_text("寫入完成")
    expect(page.locator("#csv-paste")).to_have_value("")

    assert not console_errors and not page_errors, (
        f"CSV full-success flow: console={console_errors!r} page={page_errors!r}"
    )
