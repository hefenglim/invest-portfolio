"""E2E (real server + real frontend) — DEF-017, R2 bounce: 最近匯入 and its 復原 dialog follow
the ledger on the SAME page.

R2 made ``GET /api/import/batches`` report each batch's LIVE ``row_count`` and drop an emptied
batch — correct, and invisible on the page: after a dividend was deleted on the 股利 ledger tab
the 最近匯入 list was not re-read, and its 復原 dialog still said 「將刪除這批匯入寫進帳本的 1
筆股利紀錄」 and then deleted 0.

Two paths, both driven in a browser on ``trades.html`` (the page that holds the input pane with
最近匯入 AND the ledger tables):

* **a delete on the ledger tab** re-reads the list — through the page's one refresh seam
  (``input.js::refreshAfterLedgerChange``), which used to skip it for every "external" caller;
* **the 復原 dialog** quotes the count the server holds when it opens, not the one the list
  cached — a batch emptied behind the page (another tab, the API) offers no dialog at all.
"""

import json
from typing import Any

from playwright.sync_api import Page, expect

from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory

_DIVIDENDS = (
    "account,symbol,date,type,gross,withholding,net\n"
    "tw_broker,2330,2026-04-15,CASH,1000,0,1000\n"
    "tw_broker,2330,2026-05-15,CASH,1200,0,1200\n"
)


def _import(page: Page) -> dict[str, Any]:
    """Commit a two-row dividend file through the real import door (opens one batch)."""
    body = {"kind": "dividends", "csv_text": _DIVIDENDS, "ack_warnings": True,
            "source_name": "div.csv"}
    out: dict[str, Any] = page.evaluate(
        "(b) => window.pdApi.post('/api/import/commit', b)", body)
    assert isinstance(out.get("import_batch_id"), int), out
    return out


def _dividend_ids(page: Page) -> list[int]:
    rows = page.evaluate("() => window.pdApi.get('/api/ledgers/dividends', {limit: 500})")
    return [r["id"] for r in rows["rows"] if r["date"] in ("2026-04-15", "2026-05-15")]


def _open(page: Page, base: str) -> None:
    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_function("() => !!window.pdReloadImportBatches && !!window.pdApi")
    page.click("#tab-csv")
    # The batch ROW, not the tbody: an empty tbody has no box until loadBatches has filled it.
    expect(_batch_row(page)).to_be_visible()


def _batch_row(page: Page):  # type: ignore[no-untyped-def]
    return page.locator("#bk-batches tr", has_text="div.csv")


def test_a_ledger_delete_on_the_same_page_re_reads_the_batch_list(
    flow_server: FlowServerFactory, fresh_page: Page,
) -> None:
    base = flow_server(_seed_golden)
    page = fresh_page
    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_function("() => !!window.pdApi")
    _import(page)
    _open(page, base)
    expect(_batch_row(page)).to_contain_text("2")

    # Delete ONE of the batch's dividends on the ledger tab of the same page.
    page.click("#tab-ldiv")
    row = page.locator("#div-body tr", has_text="2026-04-15")
    expect(row).to_have_count(1)
    row.locator("button", has_text="刪除").click()
    page.locator(".modal-backdrop .modal", has_text="刪除股利").locator(
        "button", has_text="刪除").click()
    expect(page.locator("#div-body tr", has_text="2026-04-15")).to_have_count(0)

    # 最近匯入 follows, with no reload: the live count and what the commit originally wrote.
    expect(_batch_row(page)).to_contain_text("1（原寫入 2）")

    # Delete the other one: the batch owns nothing any more and leaves the list.
    page.locator("#div-body tr", has_text="2026-05-15").locator(
        "button", has_text="刪除").click()
    page.locator(".modal-backdrop .modal", has_text="刪除股利").locator(
        "button", has_text="刪除").click()
    expect(page.locator("#div-body tr", has_text="2026-05-15")).to_have_count(0)
    expect(_batch_row(page)).to_have_count(0)


def test_the_undo_dialog_quotes_the_live_count_not_the_cached_one(
    flow_server: FlowServerFactory, fresh_page: Page,
) -> None:
    base = flow_server(_seed_golden)
    page = fresh_page
    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_function("() => !!window.pdApi")
    _import(page)
    _open(page, base)
    expect(_batch_row(page)).to_contain_text("2")
    first, second = _dividend_ids(page)

    # A row leaves BEHIND the page (another tab / the API) — the list on screen still says 2.
    page.evaluate("(id) => window.pdApi.del('/api/ledgers/dividends/' + id)", first)
    expect(_batch_row(page)).to_contain_text("2")
    _batch_row(page).locator("button", has_text="復原").click()
    dialog = page.locator(".modal-backdrop .modal", has_text="復原這批匯入")
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text("將刪除這批匯入寫進帳本的 1 筆股利紀錄")
    dialog.locator("button", has_text="取消").click()
    # …and the list itself was brought up to date by the re-read.
    expect(_batch_row(page)).to_contain_text("1（原寫入 2）")

    # The last row leaves behind the page too: no dialog that would promise a delete of 1.
    page.evaluate("(id) => window.pdApi.del('/api/ledgers/dividends/' + id)", second)
    _batch_row(page).locator("button", has_text="復原").click()
    expect(page.get_by_text("沒有可復原的列")).to_be_visible()
    expect(page.locator(".modal-backdrop .modal", has_text="復原這批匯入")).to_have_count(0)
    expect(_batch_row(page)).to_have_count(0)
    left = page.evaluate("() => window.pdApi.get('/api/import/batches')")
    assert json.dumps(left["batches"]) == "[]", left
