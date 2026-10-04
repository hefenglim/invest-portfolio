"""E2E (R3, pre-merge review round 2, 2026-10-04): editing an AI-drafted dividend re-validates
it against the trades of the SAME paste.

CR-02 refuses a dividend whose account opened no position by its date, and the AI door counts
a buy in the same paste as that position (the page commits 交易 before 股利). The first preview
honoured it; an EDIT did not, because the draft table re-validated only the edited kind's
drafts — the server saw no trades, and the dividend turned into a hard
``dividend_without_position``: unticked, untickable, unwritable with its own buy.

Driven on the real stack; the flow server has no LLM, so the 解析 request is rewritten in
flight into the door's edit form (``drafts``), which calls no model.
"""

import json
from collections.abc import Iterator
from typing import Any

import pytest
from playwright.sync_api import Page, Route, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.data_ingestion.store import upsert_instrument
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    """Loopback re-enabled PER TEST (pytest-socket re-bans before each one)."""
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _seed(conn: Any) -> None:
    """Golden + 2454 registered and NEVER held, so the paste's own buy is its only position."""
    _seed_golden(conn)
    upsert_instrument(conn, Instrument(symbol="2454", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Semiconductors", name="MediaTek", board="TWSE"))
    conn.commit()


#: The "model's" answer: a buy of 2454 and its dividend two weeks later, in ONE paste.
_DRAFTS: list[dict[str, Any]] = [
    {"kind": "txn", "account_id": "tw_broker", "symbol": "2454", "side": "BUY",
     "date": "2026-05-01", "shares": "1000", "price": "500"},
    {"kind": "div", "account_id": "tw_broker", "symbol": "2454", "date": "2026-05-15",
     "type": "CASH", "gross": "1000"},
]


@pytest.mark.e2e
def test_editing_a_dividend_keeps_its_buy_from_the_same_paste(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed)
    page = fresh_page
    page_errors: list[str] = []
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    edits: list[dict[str, Any]] = []

    def _ai(route: Route) -> None:
        body = route.request.post_data_json or {}
        if "drafts" in body:
            edits.append(body)
            route.continue_()
            return
        route.continue_(post_data=json.dumps({"drafts": {"rows": _DRAFTS}}))

    page.route("**/api/input/ai/preview", _ai)
    page.goto(f"{base}/trades.html", wait_until="networkidle")
    page.click("#tab-ai")
    page.fill("#ai-text", "5/1 買 2454 1000 股 @500；5/15 2454 現金股利 1,000")
    page.click("#ai-parse")
    div_row = page.locator("#ai-body-dividends tr")
    expect(div_row).to_have_count(1, timeout=20000)
    tick = div_row.locator("input[type=checkbox]")
    expect(tick).to_be_enabled()
    expect(tick).to_be_checked()                     # the first preview: clean

    # Edit the dividend's gross: the re-validation must still see the buy of the same paste.
    gross = div_row.locator("input[data-field=gross]")
    with page.expect_response("**/api/input/ai/preview") as answer:
        gross.fill("1200")
        gross.press("Tab")
    assert answer.value.status == 200
    expect(div_row).not_to_contain_text("沒有任何持倉紀錄")
    expect(tick).to_be_enabled()
    expect(tick).to_be_checked()
    sent_rows = edits[-1]["drafts"]["rows"]
    assert any(r.get("kind") == "txn" and r.get("symbol") == "2454" for r in sent_rows), sent_rows

    # Moving the BUY after the dividend strands it — the dividend is re-judged when the trade
    # it depends on changes, not only when it is edited itself.
    buy_date = page.locator("#ai-body-transactions tr input[data-field=date]")
    buy_date.fill("2026-05-20")
    buy_date.press("Tab")
    expect(div_row).to_contain_text("沒有任何持倉紀錄", timeout=20000)
    expect(tick).to_be_disabled()
    assert not page_errors, page_errors
