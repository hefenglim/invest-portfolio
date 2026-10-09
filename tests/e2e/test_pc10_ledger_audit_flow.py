"""E2E (Playwright, real server + real frontend) — post-closure item 10 (owner 2026-09-30).

The ledger audit trail had no reader: every edit / delete wrote the row's before-image to
``ledger_audit`` and no page could open it. This flow seeds the trail through the REAL store
doors — enough corrections for two pages — and walks what the owner ruled:

* 資料中心 lists it newest first, in zh (期初庫存 · 編輯), with the account named by its display
  name — the 期初庫存 row key ``schwab/AAPL`` reads 「嘉信 Schwab／AAPL」, never the raw id;
* a row opens onto its before-image as labelled fields (帳戶 / 股數 …), not a JSON blob;
* the shared pager pages it (page 2 holds older rows);
* the export centre's 「帳本操作稽核 CSV」 downloads a BOM'd CSV whose account tokens the fetch
  layer has already resolved to display names.
"""

import csv
import io
import re
import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest
from playwright.sync_api import Page, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    delete_dividend,
    insert_dividend,
    insert_transaction,
    update_transaction,
    upsert_instrument,
    upsert_opening,
)
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.e2e.conftest import FlowServerFactory

#: Enough corrections that the trail cannot fit on one page at the largest page size (500).
_EDITS = 505
_RAW_IDS = re.compile(r"(?<![\w])(?:tw_broker|schwab|moomoo_my)(?![\w])")


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _seed(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Semiconductors", name="TSMC", board="TWSE"))
    upsert_instrument(conn, Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
                                       sector="Tech", name="Apple"))
    txn = insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                             quantity=Decimal("1000"), price=Decimal("500"), fees=Decimal("0"),
                             tax=Decimal("0"), trade_date=date(2026, 1, 5))
    for n in range(_EDITS):   # the oldest rows: one edit each, quantity 1000 -> 1001 -> …
        update_transaction(conn, txn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                           quantity=Decimal(1001 + n), price=Decimal("500"), fees=Decimal("0"),
                           tax=Decimal("0"), trade_date=date(2026, 1, 5), daytrade=False)
    div = insert_dividend(conn, account_id="tw_broker", symbol="2330",
                          div_date=date(2026, 3, 1), div_type="CASH", gross=Decimal("5000"),
                          withholding=Decimal("0"), net=Decimal("5000"))
    delete_dividend(conn, div)
    upsert_opening(conn, account_id="schwab", symbol="AAPL", shares=Decimal("3"),
                   original_cost_total=Decimal("270"), build_date=date(2025, 12, 1))
    # The NEWEST audit row: the keyed 期初庫存 edit, whose row id embeds the account.
    upsert_opening(conn, account_id="schwab", symbol="AAPL", shares=Decimal("4"),
                   original_cost_total=Decimal("360"), build_date=date(2025, 12, 1))
    conn.commit()


def _errors(page: Page) -> list[str]:
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    return errors


@pytest.mark.e2e
def test_the_audit_trail_lists_pages_expands_and_exports(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed)
    page = fresh_page
    errors = _errors(page)
    total = _EDITS + 2   # the edits, the dividend delete, the opening edit

    page.goto(base + "/data-center.html", wait_until="load")
    first = page.locator("#la-body tr[data-audit-id]").first
    expect(first).to_be_visible()
    expect(page.locator("#la-count")).to_have_text(f"共 {total:,} 筆")

    # Newest first, in zh, the account by its display name.
    cells = first.locator("td")
    expect(cells.nth(1)).to_have_text("期初庫存")
    expect(cells.nth(2)).to_have_text("嘉信 Schwab／AAPL")
    expect(cells.nth(3)).to_contain_text("編輯")
    expect(first.locator(".la-act-update")).to_have_count(1)
    second = page.locator("#la-body tr[data-audit-id]").nth(1)
    expect(second.locator("td").nth(1)).to_have_text("股利帳本")
    expect(second.locator(".la-act-delete")).to_have_text("刪除")

    # The before-image opens onto labelled fields.
    first.locator("details.la-before > summary").click()
    fields = first.locator(".la-fields")
    expect(fields).to_be_visible()
    labels = fields.locator("dt").all_inner_texts()
    values = fields.locator("dd").all_inner_texts()
    shown = dict(zip(labels, values, strict=True))
    assert shown["帳戶"] == "嘉信 Schwab", shown
    assert shown["股數"] == "3", shown          # the BEFORE value, not the 4 it became
    assert shown["原始總成本"] == "270", shown
    assert "{" not in "".join(values), values   # no JSON blob, no unresolved token

    body_text = page.inner_text("#la-body")
    assert not _RAW_IDS.search(body_text), _RAW_IDS.findall(body_text)

    # The shared pager: page 2 holds OLDER rows.
    newest_id = int(first.get_attribute("data-audit-id") or "0")
    pager = page.locator("#la-pager")
    expect(pager).to_be_visible()
    pager.locator("button.pg-btn", has_text=re.compile(r"^2$")).click()
    expect(page.locator("#la-body tr[data-audit-id]").first).not_to_have_attribute(
        "data-audit-id", str(newest_id))
    page2_first = int(page.locator("#la-body tr[data-audit-id]").first
                      .get_attribute("data-audit-id") or "0")
    assert page2_first < newest_id
    expect(page.locator("#la-body tr[data-audit-id]").first.locator("td").nth(1)) \
        .to_have_text("交易帳本")

    # A phone-width page does not scroll sideways: the wide before-image cell scrolls inside
    # its .table-wrap (the whole-site layout sweep's rule), with a row expanded.
    page.set_viewport_size({"width": 390, "height": 844})
    page.locator("#la-body details.la-before > summary").first.click()
    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert overflow <= 0, overflow
    page.set_viewport_size({"width": 1280, "height": 900})

    # The export centre's CSV.
    page.goto(base + "/settings.html#exports", wait_until="load")
    card = page.locator(".ec-card", has_text="帳本操作稽核 CSV")
    expect(card).to_be_visible()
    with page.expect_download() as dl_info:
        card.locator("button").click()
    download = dl_info.value
    assert download.suggested_filename == "ledger_audit_all_all.csv"
    path = download.path()
    assert path is not None
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), raw[:8]
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
    assert rows[0][:5] == ["audit_id", "at", "table", "table_label", "row"]
    assert len(rows) == 1 + total
    last = dict(zip(rows[0], rows[-1], strict=True))   # oldest first -> the opening is last
    assert last["row"] == "嘉信 Schwab／AAPL"
    assert last["account_id"] == "schwab"              # the key column keeps the id
    assert "帳戶：嘉信 Schwab" in last["before"]
    assert "{account:" not in raw.decode("utf-8-sig")  # every token resolved at download

    assert not errors, errors
