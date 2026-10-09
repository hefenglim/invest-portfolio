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
import sqlite3
import urllib.request
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from playwright.sync_api import Page
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.data_ingestion.store import insert_cash_movement
from portfolio_dash.shared.enums import Currency
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
    # DEF-042 (2026-09-24): opening the dialog DOES preview now — the row's own findings must
    # be shown before it is saved again, as the entry door never commits without a preview —
    # but that preview writes NO fee back (it would overwrite a broker-supplied fee with the
    # engine's own figure). The golden row is clean, so the panel stays collapsed.
    row = page.locator("#tx-body tr.expandable").first
    stored_fee = row.locator("td").nth(7).inner_text().strip()
    with page.expect_response("**/api/input/manual/preview"):
        row.locator(".wl-actions .btn").first.click()
    page.wait_for_selector(".modal-title:has-text('編輯交易')")
    shown = page.locator(".modal-body .field:nth-child(8) input").input_value()
    assert Decimal(shown) == Decimal(stored_fee.replace(",", "")), (shown, stored_fee)
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

    # ---- DEF-042: the future date is acknowledged like the entry door's, one tick each --
    save = page.locator(".modal-foot .btn-primary")
    assert save.is_disabled(), "an unacknowledged warning must hold 儲存, as on the entry door"
    page.locator(".modal-body .issue-warn:has-text('晚於今日') input[type=checkbox]").check()
    page.wait_for_function(
        "() => { const b = document.querySelector('.modal-foot .btn-primary');"
        " return b && !b.disabled; }")

    # ---- and the save agrees with what the panel said --------------------------------
    with page.expect_response("**/api/ledgers/transactions/**") as saved:
        page.click(".modal-foot .btn-primary")
    assert saved.value.status == 400, (
        f"unregistered symbol must be refused, got {saved.value.status}")
    page.wait_for_selector(".toast-fail")

    assert not console_errors and not page_errors, (
        f"edit issue panel flow: console={console_errors!r} page={page_errors!r}"
    )


def _seed_golden_twd_funded(conn: sqlite3.Connection) -> None:
    """Golden, plus 100,000 TWD into schwab on 01-02 — so the golden 01-08 conversion's FROM
    side is covered and editing it leaves only the TO side in question (DEF-093)."""
    _seed_golden(conn)
    insert_cash_movement(conn, account_id="schwab", move_date=date(2026, 1, 2),
                         kind="DEPOSIT", ccy=Currency.TWD, amount=Decimal("100000"))
    conn.commit()


@pytest.mark.e2e
def test_fx_edit_that_shrinks_the_to_pool_asks_and_the_ack_writes(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """DEF-093 (owner ruling A, 2026-10-03): 編輯換匯 with a smaller 換入金額 strands the AAPL
    buy that spent the USD → 422 negative_cash → the shared danger confirm → ack → written.
    Before the rule the PUT wrote 200 in silence while the row's 刪除 asked."""
    base = flow_server(_seed_golden_twd_funded)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    page.goto(base + "/trades.html", wait_until="load")
    page.click("#tab-lfx")
    page.wait_for_selector("#fx-body tr")
    row = page.locator("#fx-body tr", has_text="2026-01-08")
    row.locator("button", has_text="編輯").click()
    modal = page.locator(".modal-backdrop .modal", has_text="編輯換匯")
    modal.locator(".field", has_text="換入金額").locator("input").fill("500")
    with page.expect_response("**/api/ledgers/fx/**") as first:
        modal.locator(".modal-foot .btn-primary").click()
    assert first.value.status == 422, f"expected negative_cash, got {first.value.status}"

    page.wait_for_selector(".modal-title:has-text('現金將變為負數')")
    body = page.locator(".modal-body").inner_text()
    assert "2026-01-10" in body and "強制寫入" in body, body
    with page.expect_response("**/api/ledgers/fx/**") as second:
        page.click(".modal-foot .btn-danger")
    assert second.value.status == 200, f"the ack must write, got {second.value.status}"
    assert json.loads(second.value.request.post_data or "{}").get("ack_negative") is True

    page.wait_for_selector(".toast-ok")
    fx = [r for r in _get_json(base, "/api/ledgers/fx?limit=500")["rows"]
          if r["account_id"] == "schwab" and r["date"] == "2026-01-08"]
    assert len(fx) == 1 and Decimal(fx[0]["to_amt"]) == Decimal("500"), fx

    assert not console_errors and not page_errors, (
        f"fx edit ack flow: console={console_errors!r} page={page_errors!r}"
    )
