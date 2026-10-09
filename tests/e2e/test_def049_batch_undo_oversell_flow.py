"""E2E (real server + real frontend) — DEF-049: 最近匯入 › 復原 asks before it strands a sell.

The verifier's demo steps, in a browser on ``trades.html``: a CSV import adds 2330 × 100, a
hand-entered sell of 1,050 is covered only because of it, and 復原 on that batch used to delete
the buy with no question (200 ``{deleted: 1}``) — the sell became 賣超 and its cost basis was
discarded. The same buy deleted on the 交易 ledger tab asked first (「賣超確認」).

Now 復原 opens the ledger tab's own 「賣超確認」 dialog, naming the sell (date, symbol, shares
sold vs held) and the consequence. 取消 → nothing changes (row count and batch as before);
確認 → the batch is undone with the acknowledgement and leaves the list.
"""

from typing import Any

from playwright.sync_api import Page, expect

from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory

_BUY = "account,symbol,side,date,shares,price\ntw_broker,2330,BUY,2026-02-02,100,500\n"


def _tx_count(page: Page) -> int:
    out: dict[str, Any] = page.evaluate(
        "() => window.pdApi.get('/api/ledgers/transactions', {limit: 500})")
    return int(out["total_count"])


def _batch_row(page: Page):  # type: ignore[no-untyped-def]
    return page.locator("#bk-batches tr", has_text="buy2330.csv")


def test_undo_that_strands_a_sell_asks_first_and_cancel_changes_nothing(
    flow_server: FlowServerFactory, fresh_page: Page,
) -> None:
    base = flow_server(_seed_golden)
    page = fresh_page
    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_function("() => !!window.pdApi")
    imported = page.evaluate(
        "(b) => window.pdApi.post('/api/import/commit', b)",
        {"kind": "transactions", "csv_text": _BUY, "ack_warnings": True,
         "source_name": "buy2330.csv"})
    assert isinstance(imported.get("import_batch_id"), int), imported
    sold = page.evaluate(
        "(b) => window.pdApi.post('/api/input/manual/commit', b)",
        {"account_id": "tw_broker", "symbol": "2330", "side": "sell",
         "date": "2026-02-10", "shares": "1050", "price": "550"})
    assert isinstance(sold.get("txn_id"), int), sold

    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_function("() => !!window.pdReloadImportBatches && !!window.pdAckConfirm")
    page.click("#tab-csv")
    expect(_batch_row(page)).to_be_visible()
    before = _tx_count(page)

    # 復原 → the ordinary undo confirm → 復原 → the server refuses → 賣超確認 opens.
    _batch_row(page).locator("button", has_text="復原").click()
    page.locator(".modal-backdrop .modal", has_text="復原這批匯入").locator(
        "button", has_text="復原").click()
    ack = page.locator(".modal-backdrop .modal", has_text="賣超確認")
    expect(ack).to_be_visible()
    for part in ("2026-02-10", "2330", "賣出 1050 股", "超過當日持股 1000 股",
                 "成本基礎會被捨棄（待釐清）"):
        expect(ack).to_contain_text(part)
    expect(ack).not_to_contain_text("{account:")          # the account token is resolved
    expect(ack.locator("button", has_text="我了解，仍要復原")).to_be_visible()

    # 取消: nothing was deleted and nothing will be.
    ack.locator("button", has_text="取消").click()
    expect(page.locator(".modal-backdrop .modal")).to_have_count(0)
    assert _tx_count(page) == before
    expect(_batch_row(page)).to_be_visible()

    # 確認: the undo goes through WITH the acknowledgement, and the batch leaves the list.
    _batch_row(page).locator("button", has_text="復原").click()
    page.locator(".modal-backdrop .modal", has_text="復原這批匯入").locator(
        "button", has_text="復原").click()
    page.locator(".modal-backdrop .modal", has_text="賣超確認").locator(
        "button", has_text="我了解，仍要復原").click()
    expect(page.get_by_text("賣超部位待釐清")).to_be_visible()
    expect(_batch_row(page)).to_have_count(0)
    assert _tx_count(page) == before - 1
