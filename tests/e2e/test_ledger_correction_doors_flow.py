"""E2E: the 交易帳本 correction doors keep their promises (M3-01 / M3-02 / M3-03).

Three defects, all in ``web/ledger.js``, all of the same shape — the SERVER answered
correctly and the page threw the answer away:

* **M3-01 (P1)** — ``DELETE /api/ledgers/fx/{id}`` answers 422 ``negative_cash`` whose own
  message ends 「確認無誤可強制寫入」, and ``remove_fx``'s docstring says the ack still
  deletes because this is a correction door. ``delWithConfirm`` recognised only ``oversell``,
  so the code fell through to a fail toast with no confirm button — and since this page holds
  the whole app's ONLY 換匯 delete control, that row could never be deleted from anywhere. A
  dead end inside an error that promises an exit.
* **M3-02 / M3-03** — the edit modal calls ``POST /api/input/manual/preview`` for the
  computed fee/tax and read only ``resp.fee`` / ``resp.tax``: a trade moved to 2099-12-31
  wrote 200 in silence while that same response carried 「交易日期 … 晚於今日,確認無誤?」.
  Rendering the payload verbatim would have introduced a SECOND lie, because the preview
  belongs to the ENTRY door, which auto-registers an unknown symbol — 「寫入時將自動查詢並
  註冊」 — while this door answers 400 「請先至「標的管理」註冊」. The panel therefore shows
  the entry door's issues re-stated for a CORRECTION.

Driven against the REAL stack (uvicorn + SQLite + the shipped static frontend), because
every one of the three is a wiring defect between a correct server and a correct-looking
page: a contract test on the endpoint passes today and passed before the fix.
"""

import json
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest
from playwright.sync_api import Page
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    """Re-enable loopback sockets PER TEST (pytest-socket re-bans before every test); each flow
    spawns a fresh isolated uvicorn (free-port probe + readiness poll need loopback TCP)."""
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _get_json(base_url: str, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(base_url + path, timeout=5) as r:  # noqa: S310 (loopback)
        data: dict[str, Any] = json.loads(r.read().decode("utf-8"))
        return data


def _sink(page: Page) -> tuple[list[str], list[str]]:
    """Console/page-error sinks. Chromium logs one 「Failed to load resource … 400/422」 line
    per deliberate rejection these flows provoke; that is the SERVER doing its job, so exactly
    those two statuses are filtered (the house pattern — see test_ai_input_union_flow) while
    every other console error, and any other status, still fails the flow."""
    console_errors: list[str] = []
    page_errors: list[str] = []

    def _console(m: Any) -> None:
        if getattr(m, "type", None) != "error":
            return
        text = getattr(m, "text", "")
        if "Failed to load resource" in text and ("400" in text or "422" in text):
            return
        console_errors.append(text)

    page.on("console", _console)
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    return console_errors, page_errors


@pytest.mark.e2e
def test_fx_delete_negative_cash_offers_the_ack_the_server_promised(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """M3-01: 換匯 delete -> 422 negative_cash -> danger confirm -> ack -> the row is GONE.

    The golden AAPL purchase was paid out of the USD this conversion produced, so removing it
    strands that spend and the schwab USD pool dips below zero — the same setup
    ``tests/contract/test_ledgers_mutations_api.py::test_delete_fx_removes_row`` uses on the
    endpoint. What is asserted HERE is the half that test cannot see: that a control exists
    for the ack, and that the row actually leaves the ledger through it.
    """
    base = flow_server(_seed_golden)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    page.goto(base + "/trades.html", wait_until="load")
    page.click("#tab-lfx")
    page.wait_for_selector("#fx-body tr")
    before = page.locator("#fx-body tr").count()
    assert before >= 1, "golden must hold at least one fx conversion"

    # ---- 刪除 -> the ordinary 「刪除換匯」 confirm ------------------------------------
    page.locator("#fx-body tr").first.locator(".btn-row-del").click()
    page.wait_for_selector(".modal-title:has-text('刪除換匯')")
    with page.expect_response("**/api/ledgers/fx/**") as first:
        page.click(".modal-foot .btn-danger")
    assert first.value.status == 422, f"expected the negative_cash guard, got {first.value.status}"
    assert "ack_negative" not in first.value.url

    # ---- the fix: a SECOND, danger-styled confirm (before it, a bare fail toast) -------
    page.wait_for_selector(".modal-title:has-text('現金將變為負數')")
    assert "強制寫入" in page.locator(".modal-body").inner_text(), (
        "the dialog must carry the server's own message, ack promise included"
    )
    with page.expect_response("**/api/ledgers/fx/**") as second:
        page.click(".modal-foot .btn-danger")
    assert "ack_negative=true" in second.value.url, second.value.url
    assert second.value.status == 200, f"the ack must still delete, got {second.value.status}"

    # ---- the row is really gone: the table AND the server agree -----------------------
    page.wait_for_selector(".toast-ok")
    page.wait_for_function(
        f"() => document.querySelectorAll('#fx-body tr').length === {before - 1}"
    )
    assert _get_json(base, "/api/ledgers/fx?limit=500")["total_count"] == before - 1

    assert not console_errors and not page_errors, (
        f"fx delete ack flow: console={console_errors!r} page={page_errors!r}"
    )


@pytest.mark.e2e
def test_edit_modal_surfaces_preview_issues_restated_for_a_correction(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """M3-02 + M3-03 (one root): the edit modal shows the preview's issues, edit-corrected.

    M3-02 — a date moved to 2099-12-31 must WARN (it wrote 200 in silence).
    M3-03 — an unregistered symbol must say what THIS door will do (refuse), not what the
    entry door does (auto-register); the save that follows must agree with the warning.
    """
    base = flow_server(_seed_golden)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#tx-body tr.expandable")
    page.locator("#tx-body tr.expandable").first.locator(".wl-actions .btn").first.click()
    page.wait_for_selector(".modal-title:has-text('編輯交易')")
    # No preview has run yet, so the panel is collapsed — opening the dialog must not fire
    # one (it would overwrite a broker-supplied fee with the engine's own figure).
    assert page.locator(".modal-body .issues .issue").count() == 0

    # ---- M3-02: a future trade date is warned about, not written in silence ------------
    with page.expect_response("**/api/input/manual/preview"):
        page.fill(".modal-body .field:nth-child(1) input", "2099-12-31")
    page.wait_for_selector(".modal-body .issue-warn:has-text('晚於今日')")

    # ---- M3-03: the unregistered-symbol note states THIS door's outcome ----------------
    # 代號 recomputes on `change`, not on every keystroke (unchanged, deliberate: a preview
    # per character), so the field is blurred the way a user leaving it would.
    with page.expect_response("**/api/input/manual/preview"):
        page.fill(".modal-body .field:nth-child(3) input", "ZZZZ")
        page.locator(".modal-body .field:nth-child(3) input").press("Tab")
    page.wait_for_selector(".modal-body .issue-error:has-text('不會自動註冊')")
    panel = page.locator(".modal-body .issues").inner_text()
    assert "自動查詢並註冊" not in panel, (
        "the entry door's auto-register PROMISE must not be shown on the correction door"
    )

    # ---- and the save agrees with what the panel said --------------------------------
    with page.expect_response("**/api/ledgers/transactions/**") as saved:
        page.click(".modal-foot .btn-primary")
    assert saved.value.status == 400, (
        f"unregistered symbol must be refused, got {saved.value.status}")
    page.wait_for_selector(".toast-fail")

    assert not console_errors and not page_errors, (
        f"edit issue panel flow: console={console_errors!r} page={page_errors!r}"
    )
