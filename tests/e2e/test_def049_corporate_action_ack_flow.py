"""E2E (real server + real frontend) — DEF-049's class fix on the 公司行動 ledger tab.

Deleting (or re-rationing) a corporate action that a later sell depends on used to go straight
through: a 10:1 split under a sell of 5,000 was deleted with one 刪除, and the dashboard read
2330 at −4,000, 賣超, basis discarded. Every other ledger door asked first.

In a browser on ``trades.html``: 刪除 on the action row → the ordinary delete confirm → the
server refuses → the ledger tab's own 「賣超確認」 names the sell. 取消 → the row stays;
「我了解，仍要刪除」 → it goes. The edit dialog gets the same question for a ratio change.
"""

from typing import Any

from playwright.sync_api import Locator, Page, expect

from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory

_SPLIT = {"account_id": "tw_broker", "date": "2026-05-01", "kind": "SPLIT",
          "from_symbol": "2330", "to_symbol": "2330", "ratio_to": "10", "ratio_from": "1",
          "ack_warnings": True}
_SELL = {"account_id": "tw_broker", "symbol": "2330", "side": "sell", "date": "2026-05-02",
         "shares": "5000", "price": "60"}


def _setup(page: Page, base: str) -> None:
    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_function("() => !!window.pdApi")
    page.evaluate("(b) => window.pdApi.post('/api/ledgers/corporate-actions', b)", _SPLIT)
    sold: dict[str, Any] = page.evaluate(
        "(b) => window.pdApi.post('/api/input/manual/commit', b)", _SELL)
    assert isinstance(sold.get("txn_id"), int), sold
    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_function("() => !!window.pdApi && !!window.pdAckConfirm")
    page.click("#tab-laction")
    expect(_row(page)).to_have_count(1)


def _row(page: Page) -> Locator:
    return page.locator("#action-body tr", has_text="2330")


def _ratio_to(page: Page) -> str:
    rows = page.evaluate(
        "() => window.pdApi.get('/api/ledgers/corporate-actions', {limit: 500})")["rows"]
    assert len(rows) == 1, rows
    return str(rows[0]["ratio_to"])


def test_deleting_a_split_a_later_sell_needs_asks_first(
    flow_server: FlowServerFactory, fresh_page: Page,
) -> None:
    base = flow_server(_seed_golden)
    page = fresh_page
    _setup(page, base)

    def _ask() -> Locator:
        _row(page).locator("button", has_text="刪除").click()
        page.locator(".modal-backdrop .modal", has_text="刪除公司行動").locator(
            "button", has_text="刪除").click()
        ack = page.locator(".modal-backdrop .modal", has_text="賣超確認")
        expect(ack).to_be_visible()
        return ack

    ack = _ask()
    for part in ("此刪除將造成賣超", "2026-05-02", "賣出 5000 股", "超過當日持股 1000 股",
                 "成本基礎會被捨棄（待釐清）"):
        expect(ack).to_contain_text(part)
    expect(ack).not_to_contain_text("{account:")
    ack.locator("button", has_text="取消").click()
    expect(page.locator(".modal-backdrop .modal")).to_have_count(0)
    expect(_row(page)).to_have_count(1)                   # 取消: nothing was deleted
    assert _ratio_to(page) == "10"

    _ask().locator("button", has_text="我了解，仍要刪除").click()
    expect(_row(page)).to_have_count(0)
    rows = page.evaluate(
        "() => window.pdApi.get('/api/ledgers/corporate-actions', {limit: 500})")["rows"]
    assert rows == []


def test_editing_the_ratio_under_a_later_sell_asks_first(
    flow_server: FlowServerFactory, fresh_page: Page,
) -> None:
    base = flow_server(_seed_golden)
    page = fresh_page
    _setup(page, base)

    def _save_ratio_2() -> Locator:
        _row(page).locator("button", has_text="編輯").click()
        modal = page.locator(".modal-backdrop .modal", has_text="編輯公司行動")
        modal.locator(".field", has_text="變成／換得（股）").locator("input").fill("2")
        modal.locator("button", has_text="儲存").click()
        warn = page.locator(".modal-backdrop .modal", has_text="公司行動警告確認")
        ack = page.locator(".modal-backdrop .modal", has_text="賣超確認")
        expect(warn.or_(ack)).to_be_visible()
        if warn.count():
            warn.locator("button", has_text="我了解，仍要儲存").click()
        expect(ack).to_be_visible()
        return ack

    ack = _save_ratio_2()
    expect(ack).to_contain_text("此更正將造成賣超")
    expect(ack).to_contain_text("賣出 5000 股，超過當日持股 2000 股")
    ack.locator("button", has_text="取消").click()
    expect(page.locator(".modal-backdrop .modal")).to_have_count(0)
    assert _ratio_to(page) == "10"                        # 取消: nothing was written

    _save_ratio_2().locator("button", has_text="我了解，仍要儲存").click()
    expect(page.locator(".modal-backdrop .modal")).to_have_count(0)
    page.wait_for_function(
        "async () => (await window.pdApi.get('/api/ledgers/corporate-actions'))"
        ".rows[0].ratio_to === '2'")
