"""E2E: the W4 union door — one parse, THREE sections, per-kind commits (AI-D17/D18/D21).

Drives the REAL stack (uvicorn subprocess + SQLite + served web/) with the LLM seam stubbed
by ``page.route`` (the flow server has no provider): ``/api/input/ai/preview`` returns a
canned MIXED preview — one transaction, one dividend, one cash row, plus one confessed
``unparsed`` row — and ``/api/import/commit`` is canned while its request bodies are captured.

Asserts the contract the owner ruled on:

  * the preview renders THREE sections (交易／股利／資金), an absent kind would stay hidden;
  * the cash rows show the server-owned 中文 kind label AND an explicit sign (＋入金 /
    −券商費用 — the debit arm pinned by VALUE, not truthiness) — a mislabelled kind
    reverses the pool by 2× with no error, so the label+sign ARE the guard;
  * the unparsed row is surfaced in the banner, not dropped;
  * 寫入全部勾選 fires ONE /api/import/commit PER KIND, each carrying only its own rows;
  * full success clears every section (no double-submit);
  * a HARD failure on one kind still settles the kinds that already committed (section
    retired, ledger refreshed), then reports the failure and stays retry-able.
"""

import json
from collections.abc import Iterator
from typing import Any

import pytest
from playwright.sync_api import Page, Route, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory

_TXN_CSV = ("account,symbol,side,date,shares,price,daytrade,short_sale,note\n"
            "tw_broker,2330,BUY,2026-06-01,1000,600,0,0,\n")
_DIV_CSV = ("account,symbol,date,type,gross,withholding,net,reinvest_shares,reinvest_price\n"
            "tw_broker,2330,2026-06-03,CASH,1000,0,1000,,\n")
_CASH_CSV = ("account,date,kind,ccy,amount,acq_home_amount,note\n"
             "tw_broker,2026-06-01,DEPOSIT,TWD,50000,,\n"
             "tw_broker,2026-06-02,BROKER_FEE,TWD,25,,\n")

_PREVIEW = json.dumps({
    "previews": {
        "transactions": {
            "rows": [{"n": 0, "status": "ok", "reason": None, "code": None,
                      "data": {"account_id": "tw_broker", "symbol": "2330", "side": "buy",
                               "trade_date": "2026-06-01", "quantity": "1000", "price": "600",
                               "fee": "855", "tax": "0", "daytrade": "0", "short_sale": "0"}}],
            "summary": {"total": 1, "ok": 1, "warn": 0, "error": 0},
        },
        "dividends": {
            "rows": [{"n": 0, "status": "ok", "reason": None, "code": None,
                      "data": {"account_id": "tw_broker", "symbol": "2330",
                               "date": "2026-06-03", "type": "CASH", "gross": "1000",
                               "withholding": "0", "net": "1000"}}],
            "summary": {"total": 1, "ok": 1, "warn": 0, "error": 0},
        },
        "cash": {
            "rows": [{"n": 0, "status": "ok", "reason": None, "code": None,
                      "data": {"account_id": "tw_broker", "date": "2026-06-01",
                               "kind": "DEPOSIT", "kind_label": "入金", "sign": "1",
                               "ccy": "TWD", "amount": "50000"}},
                     # A DEBIT row: sign arrives as the STRING "-1" — a JS truthiness
                     # check would render it as an inflow ("-1" is truthy); the render
                     # must compare by VALUE. The label+sign ARE the guard against a
                     # mislabelled kind reversing the pool by 2×.
                     {"n": 1, "status": "ok", "reason": None, "code": None,
                      "data": {"account_id": "tw_broker", "date": "2026-06-02",
                               "kind": "BROKER_FEE", "kind_label": "券商費用", "sign": "-1",
                               "ccy": "TWD", "amount": "25"}}],
            "summary": {"total": 2, "ok": 2, "warn": 0, "error": 0},
        },
    },
    "unparsed": [{"text": "TWD 轉 USD 31500", "reason": "換匯請改用換匯登錄"}],
    "meta": {"model": "mock", "via": "litellm", "cost_usd": None},
    "csv_texts": {"transactions": _TXN_CSV, "dividends": _DIV_CSV, "cash": _CASH_CSV},
})


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    """Re-enable loopback sockets PER TEST (pytest-socket re-bans before every test); each flow
    spawns a fresh isolated uvicorn (free-port probe + readiness poll need loopback TCP)."""
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


@pytest.mark.e2e
def test_mixed_union_renders_three_sections_and_commits_per_kind(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_golden)
    page = fresh_page
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(getattr(m, "text", ""))
            if getattr(m, "type", None) == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    commits: list[dict[str, Any]] = []

    def _commit_route(route: Route) -> None:
        body = route.request.post_data_json or {}
        commits.append(body)
        # Honest count: one written row per committed data line (the cash file has two).
        written = len([ln for ln in (body.get("csv_text") or "").splitlines()[1:]
                       if ln.strip()])
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"written": written, "skipped": 0}))

    page.route("**/api/input/ai/preview",
               lambda route: route.fulfill(status=200, content_type="application/json",
                                           body=_PREVIEW))
    page.route("**/api/import/commit", _commit_route)

    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#csv-kinds .chip", state="attached")
    page.click("#tab-ai")
    page.wait_for_selector("#ai-dropzone", state="visible")
    page.fill("#ai-text", "買 2330、配息、入金、外加一筆換匯")
    page.click("#ai-parse")
    page.wait_for_selector("#ai-body-transactions tr", state="visible")

    # Three sections render; the unparsed banner confesses the fx row.
    expect(page.locator("#ai-sec-transactions")).to_be_visible()
    expect(page.locator("#ai-sec-dividends")).to_be_visible()
    expect(page.locator("#ai-sec-cash")).to_be_visible()
    expect(page.locator("#ai-unparsed")).to_be_visible()
    expect(page.locator("#ai-unparsed")).to_contain_text("TWD 轉 USD 31500")

    # AI-D21: the cash row carries the 中文 label AND the explicit sign.
    # (columns: [✓] 帳戶 日期 類型 幣別 金額 …→ 類型 is the 4th td, 金額 the 6th)
    cash_rows = page.locator("#ai-body-cash tr")
    expect(cash_rows.nth(0).locator("td:nth-child(4) .dir-chip")).to_have_text("＋ 入金")
    expect(cash_rows.nth(0).locator("td:nth-child(6)")).to_contain_text("＋")
    # The DEBIT arm: sign "-1" renders the sell-coloured chip and a − amount — pinned by
    # VALUE, so a truthiness regression ("-1" is truthy) fails here instead of silently
    # showing every outflow as an inflow.
    debit_chip = cash_rows.nth(1).locator("td:nth-child(4) .dir-chip")
    expect(debit_chip).to_have_text("− 券商費用")
    expect(debit_chip).to_have_class("dir-chip dir-sell")
    expect(cash_rows.nth(1).locator("td:nth-child(6)")).to_contain_text("−")
    # The dividend type renders in Chinese too.
    expect(page.locator("#ai-body-dividends td:nth-child(5)")).to_have_text("現金")
    # The txn row carries daytrade "0" / short_sale "0" STRINGS — neither chip may render
    # ("0" is truthy; the daytrade file pins both arms of the same String(...) === '1'
    # pattern, this pins the 放空 arm's negative side).
    expect(page.locator("#ai-body-transactions .dir-short")).to_have_count(0)
    expect(page.locator("#ai-body-transactions .dir-daytrade")).to_have_count(0)

    # Commit: ONE call per kind, each carrying only its own rows.
    page.click("#ai-write-all")
    expect(page.locator("#ai-result")).to_contain_text("寫入完成", timeout=15000)

    kinds = [c.get("kind") for c in commits]
    assert kinds == ["transactions", "dividends", "cash"], commits
    assert "1000,600" in commits[0]["csv_text"]
    assert "CASH,1000" in commits[1]["csv_text"]
    assert "DEPOSIT,TWD,50000" in commits[2]["csv_text"]

    # Full success cleared every section — a second identical commit is impossible.
    expect(page.locator("#ai-write-all")).to_be_disabled()

    assert not console_errors and not page_errors, (
        f"AI union flow: console={console_errors!r} page={page_errors!r}")


@pytest.mark.e2e
def test_a_failed_kind_still_settles_the_kinds_that_committed(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """A hard failure on ONE kind must not strand the kinds that already wrote.

    The per-kind loop posts transactions → dividends → cash. Here dividends answers 500.
    The transactions commit ALREADY succeeded server-side, so the UI must settle it —
    retire its section, refresh the ledger, report the partial counts — then surface the
    failure and leave the failed + never-attempted kinds' rows in place, retry-able.
    (Pre-fix trace: the catch-all restored the button and returned after one bare
    寫入失敗 toast; the committed kind's rows stayed visible looking uncommitted, and a
    retry re-posted them — the server's content-hash dedupe absorbed the double write,
    but the UI lied about what had landed.)
    """
    base = flow_server(_seed_golden)
    page = fresh_page
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(getattr(m, "text", ""))
            if getattr(m, "type", None) == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    commits: list[dict[str, Any]] = []
    fail_dividends = {"on": True}

    def _commit_route(route: Route) -> None:
        body = route.request.post_data_json or {}
        commits.append(body)
        if fail_dividends["on"] and body.get("kind") == "dividends":
            route.fulfill(status=500, content_type="application/json",
                          body=json.dumps({"error": {"code": "db_error",
                                                     "message": "database is locked"}}))
            return
        written = len([ln for ln in (body.get("csv_text") or "").splitlines()[1:]
                       if ln.strip()])
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"written": written, "skipped": 0}))

    page.route("**/api/input/ai/preview",
               lambda route: route.fulfill(status=200, content_type="application/json",
                                           body=_PREVIEW))
    page.route("**/api/import/commit", _commit_route)

    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#csv-kinds .chip", state="attached")
    page.click("#tab-ai")
    page.wait_for_selector("#ai-dropzone", state="visible")
    page.fill("#ai-text", "買 2330、配息、入金")
    page.click("#ai-parse")
    page.wait_for_selector("#ai-body-transactions tr", state="visible")

    page.click("#ai-write-all")

    # The committed kind's section RETIRES even though a sibling kind failed...
    expect(page.locator("#ai-sec-transactions")).to_be_hidden(timeout=15000)
    # ...the failure is surfaced (fail toasts persist until dismissed)...
    expect(page.locator(".toast-fail .msg")).to_contain_text("database is locked")
    # ...and the failed + never-attempted kinds keep their rows, retry-able.
    expect(page.locator("#ai-sec-dividends")).to_be_visible()
    expect(page.locator("#ai-sec-cash")).to_be_visible()
    expect(page.locator("#ai-write-all")).to_be_enabled()

    # Retry with the door healed: ONLY the remainder is posted — transactions is not
    # re-committed (its section + csv were retired by the settlement above).
    fail_dividends["on"] = False
    commits.clear()
    page.click("#ai-write-all")
    expect(page.locator("#ai-result")).to_contain_text("寫入完成", timeout=15000)
    kinds = [c.get("kind") for c in commits]
    assert kinds == ["dividends", "cash"], commits
    expect(page.locator("#ai-write-all")).to_be_disabled()

    # The intentional mocked 500 emits ONE expected browser "Failed to load resource"
    # network log (same precedent as the 402/409/503 degrade filter in
    # test_ai_input_flow.py); any OTHER console error — and ANY page error — still fails.
    real_console = [e for e in console_errors
                    if not ("Failed to load resource" in e and "500" in e)]
    assert not real_console and not page_errors, (
        f"AI union mid-loop failure flow: console={real_console!r} page={page_errors!r}")


@pytest.mark.e2e
def test_the_ack_retry_also_settles_kinds_before_reporting_a_failure(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """The SAME settlement rule inside the 422 confirm-retry loop.

    Two kinds trip warnings_unacknowledged; the user confirms; the first acked commit
    succeeds and the second answers 500. The acked kind must be settled (section retired)
    before the failure is reported — stranding it leaves a committed kind on screen
    looking uncommitted, the exact defect of the main loop one function up.
    """
    base = flow_server(_seed_golden)
    page = fresh_page
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(getattr(m, "text", ""))
            if getattr(m, "type", None) == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    calls: dict[str, int] = {}

    def _commit_route(route: Route) -> None:
        body = route.request.post_data_json or {}
        kind = str(body.get("kind"))
        calls[kind] = calls.get(kind, 0) + 1
        # First pass: dividends + cash both refuse with unacknowledged warnings.
        if not body.get("ack_warnings") and kind in ("dividends", "cash"):
            route.fulfill(status=422, content_type="application/json",
                          body=json.dumps({"error": {
                              "code": "warnings_unacknowledged",
                              "message": "有警告列需確認後才寫入"}}))
            return
        # The ack pass: dividends writes, cash dies hard.
        if kind == "cash":
            route.fulfill(status=500, content_type="application/json",
                          body=json.dumps({"error": {"code": "db_error",
                                                     "message": "database is locked"}}))
            return
        written = len([ln for ln in (body.get("csv_text") or "").splitlines()[1:]
                       if ln.strip()])
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"written": written, "skipped": 0}))

    page.route("**/api/input/ai/preview",
               lambda route: route.fulfill(status=200, content_type="application/json",
                                           body=_PREVIEW))
    page.route("**/api/import/commit", _commit_route)

    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#csv-kinds .chip", state="attached")
    page.click("#tab-ai")
    page.wait_for_selector("#ai-dropzone", state="visible")
    page.fill("#ai-text", "買 2330、配息、入金")
    page.click("#ai-parse")
    page.wait_for_selector("#ai-body-transactions tr", state="visible")

    # First pass: transactions commits clean; the two warned kinds collect ONE confirm.
    page.click("#ai-write-all")
    expect(page.locator("#ai-sec-transactions")).to_be_hidden(timeout=15000)
    confirm = page.locator(".modal-backdrop").last.get_by_role("button", name="確認寫入")
    confirm.wait_for(state="visible")
    confirm.click()

    # The acked kind (dividends) wrote BEFORE cash failed — it must be settled anyway.
    expect(page.locator("#ai-sec-dividends")).to_be_hidden(timeout=15000)
    expect(page.locator(".toast-fail .msg")).to_contain_text("database is locked")
    expect(page.locator("#ai-sec-cash")).to_be_visible()  # the failed kind stays, retry-able
    assert calls.get("dividends") == 2 and calls.get("cash") == 2, calls  # 422 then ack

    real_console = [e for e in console_errors
                    if not ("Failed to load resource" in e and ("500" in e or "422" in e))]
    assert not real_console and not page_errors, (
        f"AI union ack-failure flow: console={real_console!r} page={page_errors!r}")
