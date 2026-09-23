"""E2E: DEF-004 (every CSV kind previews its own values) and DEF-035/036 (the AI draft table is
editable and a contradicted amount is flagged), on the REAL stack.

DEF-004 — the verifier pasted each kind's own 範本 back and read 「DEPOSIT — —」: the 600,000 TWD
of the 資金 template, the 股利 gross, both 換匯 amounts, the 期初 build date and cost and the
公司行動 ratio were all in the preview payload and on no column. Here every template is fetched
from the server, pasted, and the value the verifier missed is read off the rendered table.

DEF-035/036 — the flow server has no LLM, so the ``/api/input/ai/preview`` request that the 解析
button sends is REWRITTEN in flight into the door's edit form (``drafts``), which calls no
model. Everything downstream is real: the preview, the fee engine, the amount check, the
regenerated CSV, and the edit round trip the draft table makes on its own.
"""

import json
from collections.abc import Iterator
from typing import Any

import pytest
from playwright.sync_api import Page, Route, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    """Loopback re-enabled PER TEST (pytest-socket re-bans before each one)."""
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


#: kind chip label -> (kind, text the first template row must show, a header it must carry)
_KINDS = [
    ("資金", "cash", "600,000", "金額"),
    ("股利", "dividends", "5,000", "毛額"),
    ("換匯", "fx", "32,000 TWD → 1,000.00 USD", "換匯"),
    ("期初", "openings", "500,000", "原始總成本"),
    ("公司行動", "corporate_actions", "每 1 股 → 10 股", "比例"),
]


@pytest.mark.e2e
def test_every_template_previews_its_own_values(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_golden)
    page = fresh_page
    console_errors, page_errors = _sink(page)
    page.goto(f"{base}/trades.html", wait_until="networkidle")
    page.click("#tab-csv")
    for label, kind, value, header in _KINDS:
        text = page.request.get(f"{base}/api/import/template?kind={kind}").body()
        page.locator("#csv-kinds .chip", has_text=label).click()
        expect(page.locator("#csv-head")).to_contain_text(header)
        with page.expect_response("**/api/import/preview"):
            page.fill("#csv-paste", text.decode("utf-8-sig"))
        first = page.locator("#csv-body tr").first
        expect(first).to_contain_text(value, timeout=20000)
        # the 期初 date is build_date — it printed 「—」 under the shared header
        if kind == "openings":
            expect(first).to_contain_text("2026-01-02")
        # the account column is the display name, never the id
        expect(first).not_to_contain_text("tw_broker")
        page.fill("#csv-paste", "")
    assert not console_errors and not page_errors, (console_errors, page_errors)


#: The "model's" answer: row 0 states a total that contradicts 100 × 600; row 1 states none.
_DRAFTS: list[dict[str, Any]] = [
    {"kind": "txn", "account_id": "tw_broker", "symbol": "2330", "side": "BUY",
     "date": "2026-06-01", "shares": "100", "price": "600", "stated_amount": "50000"},
    {"kind": "txn", "account_id": "tw_broker", "symbol": "2330", "side": "BUY",
     "date": "2026-06-02", "shares": "10", "price": "600"},
]


@pytest.mark.e2e
def test_ai_drafts_are_editable_and_a_contradicted_amount_is_not_preticked(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_golden)
    page = fresh_page
    console_errors, page_errors = _sink(page)
    edits: list[dict[str, Any]] = []
    commits: list[dict[str, Any]] = []

    def _ai(route: Route) -> None:
        body = route.request.post_data_json or {}
        if "drafts" in body:
            edits.append(body)
            route.continue_()
            return
        # the 解析 call: replace the (absent) model with a fixed extraction — no LLM involved
        route.continue_(post_data=json.dumps({"drafts": {"rows": _DRAFTS}}))

    def _commit(route: Route) -> None:
        body = route.request.post_data_json or {}
        commits.append(body)
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"written": 1, "skipped": 0}))

    page.route("**/api/input/ai/preview", _ai)
    page.route("**/api/import/commit", _commit)
    page.goto(f"{base}/trades.html", wait_until="networkidle")
    page.click("#tab-ai")
    page.fill("#ai-text", "6/1 買 2330 100 股 @600 成交金額 50,000；6/2 買 2330 10 股 @600")
    page.click("#ai-parse")
    rows = page.locator("#ai-body-transactions tr")
    expect(rows).to_have_count(2, timeout=20000)

    # DEF-035: the account is a select showing the display name, and the row is editable.
    acct = rows.nth(0).locator("select[data-field=account_id]")
    expect(acct.locator("option:checked")).to_contain_text("台灣券商")
    expect(rows.nth(0)).not_to_contain_text("tw_broker")
    # DEF-036: 100 × 600 = 60,000 against a stated 50,000 — flagged and NOT pre-ticked.
    expect(rows.nth(0)).to_contain_text("金額矛盾")
    expect(rows.nth(0).locator("input[type=checkbox]")).not_to_be_checked()
    expect(rows.nth(1).locator("input[type=checkbox]")).to_be_checked()
    # tds: ✓ 帳戶 日期 買賣 代號 股數 價格 費用(7) 稅 狀態 動作
    fee = rows.nth(1).locator("td").nth(7)
    expect(fee).to_have_text("20")                 # 10 × 600 → the NT$20 minimum

    # Edit row 1's shares: the server re-prices it (1000 × 600 × 0.1425% = 855).
    shares = rows.nth(1).locator("input[data-field=shares]")
    shares.fill("1000")
    shares.press("Tab")
    expect(rows.nth(1).locator("td").nth(7)).to_have_text("855", timeout=20000)
    assert edits and edits[-1]["drafts"]["rows"][1]["shares"] == "1000"

    # A typed value that does not parse stays as typed, is flagged, and blocks the write.
    shares = rows.nth(1).locator("input[data-field=shares]")
    shares.fill("1,000")
    shares.press("Tab")
    expect(rows.nth(1).locator("input[data-field=shares]")).to_have_value("1,000")
    expect(rows.nth(1).locator("input[data-field=shares]")).to_have_class(
        "ai-edit num invalid")
    expect(rows.nth(1)).to_contain_text("不是有效的數字")
    expect(page.locator("#ai-write-all")).to_be_disabled()
    shares = rows.nth(1).locator("input[data-field=shares]")
    shares.fill("1000")
    shares.press("Tab")
    expect(page.locator("#ai-write-all")).to_be_enabled(timeout=20000)

    # Fixing row 0's price to agree with its own stated total clears the contradiction.
    price = rows.nth(0).locator("input[data-field=price]")
    price.fill("500")
    price.press("Tab")
    expect(rows.nth(0)).not_to_contain_text("金額矛盾", timeout=20000)

    # The write carries the EDITED value, from the server-regenerated CSV.
    page.click("#ai-write-all")
    expect(page.locator("#ai-result")).to_contain_text("寫入完成", timeout=20000)
    assert len(commits) == 1, commits
    assert "tw_broker,2330,BUY,2026-06-02,1000,600" in commits[0]["csv_text"]
    assert "2026-06-01" not in commits[0]["csv_text"]     # row 0 was never ticked
    assert commits[0].get("source_name") == "AI 輸入"
    assert not console_errors and not page_errors, (console_errors, page_errors)


@pytest.mark.e2e
def test_undoing_a_batch_refreshes_the_input_panes_holdings(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """DEF-019: 最近匯入 › 復原 (broker-import.js) must reach input.js's single refresh.

    Before the fix the undo re-fetched its batch list and the ledger tables only; the holdings
    the manual picker annotates stayed at the pre-undo figures until a reload. The observable
    contract: after the undo, the input pane asks /api/input/holdings again.
    """
    base = flow_server(_seed_golden)
    page = fresh_page
    console_errors, page_errors = _sink(page)
    page.goto(f"{base}/trades.html", wait_until="networkidle")
    page.click("#tab-csv")
    with page.expect_response("**/api/import/preview"):
        page.fill("#csv-paste",
                  "account,symbol,side,date,shares,price\ntw_broker,2330,buy,2026-06-03,7,600\n")
    expect(page.locator("#csv-body tr")).to_have_count(1, timeout=20000)
    with page.expect_response("**/api/import/commit") as cm:
        page.click("#csv-confirm")
    assert cm.value.status == 200
    # DEF-017: the batch names where it came from (a pasted CSV here).
    batch = page.locator("#bk-batches tr", has_text="貼上 CSV")
    expect(batch).to_have_count(1, timeout=20000)
    page.wait_for_load_state("networkidle")

    batch.get_by_role("button", name="復原").click()
    dialog = page.locator(".modal-backdrop").last
    with page.expect_request(lambda r: "/api/input/holdings" in r.url, timeout=20000):
        dialog.get_by_role("button", name="復原").click()
    expect(page.locator("#bk-batches tr", has_text="貼上 CSV")).to_have_count(0, timeout=20000)
    assert not page_errors, page_errors
    assert not console_errors, console_errors
