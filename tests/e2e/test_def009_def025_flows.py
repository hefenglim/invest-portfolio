"""E2E: the verifier's own clicks for DEF-025 (bulk 賣超) and DEF-009 (折讓款 row edit).

DEF-025 (owner ruling 2026-09-24, 比照手動輸入): in CSV 匯入 and AI 輸入 a 賣超 row is NOT
pre-ticked; committing it opens a dialog that names the row and says 「成本基礎會被永久捨棄」,
every box unticked; 取消 writes nothing; ticking + 寫入勾選的警告列 writes it — and the request
names the row in ``ack_rows`` (the server refuses it otherwise).

DEF-009 (owner ruling 2026-09-24): the 資金管理 edit dialog on a CONFIRMED 折讓款 lets 日期／類型／
金額／備註 all be edited; the save lands; the month the credit booked stays booked.

Drives the REAL stack (uvicorn + SQLite + served web/). The first commit's 422 is intrinsic to
the acknowledgement flow, so the browser's one resource-load line for it is benign.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.request
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from playwright.sync_api import Page, Request, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.data_ingestion.store import insert_cash_movement, insert_transaction
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.enums import Side
from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory

_CSV = ("account,symbol,side,date,shares,price\n"
        "tw_broker,2330,buy,2026-06-05,10,600\n"
        "tw_broker,2330,sell,2026-06-06,1500,600\n")        # holds 1,000 + 10 → 賣超


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _get_json(base: str, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(base + path, timeout=5) as r:  # noqa: S310 (loopback)
        data: dict[str, Any] = json.loads(r.read().decode("utf-8"))
        return data


def _sells(base: str) -> list[dict[str, Any]]:
    return [r for r in _get_json(base, "/api/ledgers/transactions")["rows"]
            if r["symbol"] == "2330" and r["side"].lower() == "sell"]


def _sink(page: Page) -> tuple[list[str], list[str]]:
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(getattr(m, "text", ""))
            if getattr(m, "type", None) == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    return console_errors, page_errors


def _real_errors(console_errors: list[str]) -> list[str]:
    return [t for t in console_errors if not ("Failed to load resource" in t and "422" in t)]


def _commit_bodies(page: Page) -> list[dict[str, Any]]:
    bodies: list[dict[str, Any]] = []

    def _on(req: Request) -> None:
        if req.url.endswith("/api/import/commit") and req.method == "POST":
            data = req.post_data_json
            if isinstance(data, dict):
                bodies.append(data)
    page.on("request", _on)
    return bodies


@pytest.mark.e2e
def test_def025_csv_oversell_is_unticked_named_and_written_only_when_ticked(
    flow_server: FlowServerFactory, fresh_page: Page,
) -> None:
    base = flow_server(_seed_golden)
    page = fresh_page
    console_errors, page_errors = _sink(page)
    bodies = _commit_bodies(page)
    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#csv-kinds .chip", state="attached")
    page.click("#tab-csv")
    with page.expect_response("**/api/import/preview"):
        page.fill("#csv-paste", _CSV)
    rows = page.locator("#csv-body tr")
    expect(rows).to_have_count(2, timeout=20000)
    boxes = page.locator("#csv-body input[type=checkbox]")
    expect(boxes.nth(0)).to_be_checked()
    expect(boxes.nth(1)).not_to_be_checked()              # the 賣超 row: NOT pre-ticked
    expect(rows.nth(1)).to_contain_text("預設不勾選")

    # The owner ticks it deliberately → the commit NAMES it before anything is written.
    boxes.nth(1).check()
    page.click("#csv-confirm")
    dialog = page.locator(".modal-backdrop .modal", has_text="匯入警告確認")
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text("第 2 列 2330")
    expect(dialog).to_contain_text("成本基礎會被永久捨棄")
    ticks = dialog.locator("input.imp-warn-tick")
    expect(ticks).to_have_count(1)
    expect(ticks.first).not_to_be_checked()
    expect(dialog.locator("button", has_text="寫入勾選的警告列")).to_be_disabled()
    dialog.locator("button", has_text="取消，停在這一步").click()
    expect(dialog).to_have_count(0)
    assert _sells(base) == [], "cancel wrote the 賣超 row"
    assert all("ack_rows" not in b for b in bodies), bodies

    # Second attempt: tick it in the dialog and confirm → written, under ITS OWN ack.
    page.click("#csv-confirm")
    expect(dialog).to_be_visible()
    dialog.locator("input.imp-warn-tick").first.check()
    with page.expect_response("**/api/import/commit") as acked:
        dialog.locator("button", has_text="寫入勾選的警告列").click()
    assert acked.value.status == 200, acked.value.text()
    sent = bodies[-1]
    assert sent["ack_warnings"] is True and sent["select"] == [0, 1] and sent["ack_rows"] == [1]
    assert [r["shares"] for r in _sells(base)] == ["1500"]
    expect(page.locator("#csv-result")).to_be_visible()
    assert not _real_errors(console_errors) and not page_errors, (console_errors, page_errors)


_AI_CSV = ("account,symbol,side,date,shares,price,daytrade,short_sale,note\n"
           "tw_broker,2330,sell,2026-06-06,1500,600,0,0,\n")
_AI_PREVIEW = json.dumps({
    "previews": {"transactions": {
        "rows": [{"n": 0, "status": "warn", "reason": "賣出 1500 股，超過持有的 1000 股",
                  "code": None, "kinds": ["sell_exceeds_holdings"],
                  "data": {"account_id": "tw_broker", "symbol": "2330", "side": "sell",
                           "trade_date": "2026-06-06", "quantity": "1500", "price": "600",
                           "fee": "1282", "tax": "2700", "daytrade": "0",
                           "short_sale": "0"}}],
        "summary": {"total": 1, "ok": 0, "warn": 1, "error": 0}}},
    "unparsed": [],
    "meta": {"model": "stub", "via": "litellm", "cost_usd": None},
    "csv_texts": {"transactions": _AI_CSV},
})


@pytest.mark.e2e
def test_def025_ai_door_oversell_is_unticked_and_acknowledged_per_row(
    flow_server: FlowServerFactory, fresh_page: Page,
) -> None:
    """The AI 輸入 door: same rule. The LLM seam is stubbed; the commit and the re-preview
    are the REAL endpoints, so the server's own refusal drives the dialog."""
    base = flow_server(_seed_golden)
    page = fresh_page
    console_errors, page_errors = _sink(page)
    bodies = _commit_bodies(page)
    page.route("**/api/input/ai/preview",
               lambda route: route.fulfill(status=200, content_type="application/json",
                                           body=_AI_PREVIEW))
    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#csv-kinds .chip", state="attached")
    page.click("#tab-ai")
    page.fill("#ai-text", "賣 2330 1500 股")
    page.click("#ai-parse")
    row = page.locator("#ai-body-transactions tr")
    expect(row).to_have_count(1)
    box = row.first.locator("input[type=checkbox]")
    expect(box).not_to_be_checked()                        # the 賣超 row: NOT pre-ticked
    expect(page.locator("#ai-write-all")).to_be_disabled()
    box.check()
    page.click("#ai-write-all")
    dialog = page.locator(".modal-backdrop .modal", has_text="匯入警告確認")
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text("成本基礎會被永久捨棄")
    expect(dialog.locator("input.imp-warn-tick").first).not_to_be_checked()
    dialog.locator("input.imp-warn-tick").first.check()
    with page.expect_response("**/api/import/commit") as acked:
        dialog.locator("button", has_text="寫入勾選的警告列").click()
    assert acked.value.status == 200, acked.value.text()
    assert bodies[0]["ack_warnings"] is False and "ack_rows" not in bodies[0]
    assert bodies[-1]["ack_rows"] == [0]
    assert [r["shares"] for r in _sells(base)] == ["1500"]
    expect(page.locator("#ai-sec-transactions")).to_be_hidden(timeout=15000)
    assert not _real_errors(console_errors) and not page_errors, (console_errors, page_errors)


def _seed_confirmed_rebate(conn: sqlite3.Connection) -> None:
    """Golden + a May fee-bearing trade whose refund was CONFIRMED (a linked credit)."""
    _seed_golden(conn)
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=Decimal("1000"), price=Decimal("500"), fees=Decimal("142"),
                       tax=Decimal("0"), trade_date=date(2026, 5, 5))
    insert_cash_movement(conn, account_id="tw_broker", move_date=date(2026, 6, 1),
                         kind="REBATE", ccy=Currency.TWD, amount=Decimal("109"),
                         note="2026-05 折讓款", rebate_period="2026-05")
    conn.commit()


@pytest.mark.e2e
def test_def009_a_confirmed_rebate_is_fully_editable_and_its_month_stays_booked(
    flow_server: FlowServerFactory, fresh_page: Page,
) -> None:
    base = flow_server(_seed_confirmed_rebate)
    page = fresh_page
    console_errors, page_errors = _sink(page)
    assert all(r["month"] != "2026-05" for r in _get_json(base, "/api/rebates")["rows"])
    page.goto(base + "/cash.html#flows", wait_until="load")
    row = page.locator("#cm-body tr", has_text="折讓款")
    expect(row).to_have_count(1)
    row.locator("button", has_text="編輯").click()
    modal = page.locator(".modal-backdrop .modal", has_text="編輯資金紀錄")
    expect(modal).to_be_visible()
    date_in = modal.locator(".field", has_text="日期").locator("input")
    kind = modal.locator(".field", has_text="方向").locator("select")
    note = modal.locator(".field", has_text="備註").locator("input")
    amount = modal.locator(".field", has_text="金額").locator("input")
    for control in (date_in, kind, note, amount):
        expect(control).to_be_enabled()
    expect(kind).to_have_value("rebate")
    expect(modal).to_contain_text("2026-05 月份")
    date_in.fill("2026-06-03")
    amount.fill("100")
    note.fill("實收 100（券商對帳單）")
    modal.locator("button", has_text="儲存").click()
    # The golden tw_broker pool is negative from its unfunded buy, so any edit asks the
    # ordinary 現金將變為負數 question first — the same as for a hand-entered row.
    neg = page.locator(".modal-backdrop .modal", has_text="現金將變為負數")
    expect(neg).to_be_visible()
    neg.locator("button", has_text="我了解，仍要寫入").click()
    expect(page.locator("#cm-body tr", has_text="實收 100")).to_have_count(1)
    moved = next(m for m in _get_json(base, "/api/cash?limit=500")["movements"]["rows"]
                 if m["kind"] == "rebate")
    assert (moved["date"], moved["amount"], moved["note"], moved["rebate_period"]) == (
        "2026-06-03", "100", "實收 100（券商對帳單）", "2026-05")
    # The month the credit booked stays booked — no second credit is offered.
    assert all(r["month"] != "2026-05" for r in _get_json(base, "/api/rebates")["rows"])
    assert not _real_errors(console_errors) and not page_errors, (console_errors, page_errors)
