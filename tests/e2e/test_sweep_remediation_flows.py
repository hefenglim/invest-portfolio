"""E2E counter-evidence for the three 2026-08-27 sweep findings that live in the BROWSER.

Each of these is invisible to an API test, because in each case the server was already
correct and the browser was not sending, reading, or offering what the screen implied:

  * **F-03** — 「確認寫入勾選列」 posted the whole pasted CSV. ``commit_preview`` has always
    taken an ``accept`` set; nothing ever filled it from the checkboxes, so unticking rows
    changed nothing and the ledger gained rows the user had explicitly removed.
  * **F-05** — the symbol picker's rows are bound on ``mousedown`` (deliberately, to beat the
    input's ``focusout``). ``mousedown`` is not what Enter fires, so typing filtered the list
    and then nothing could choose from it.
  * **F-06** — the 目標 % fields were ``toFixed(1)`` views of full-precision weights while the
    footer total and the POSTed plan summed the unrounded values, so the footer said 100.00%
    over seventeen fields that visibly summed to 100.1%.
"""

import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest
from playwright.sync_api import Page, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import GOLDEN_NOW, PriceRow, _seed_golden
from tests.e2e.conftest import FlowServerFactory

_CSV = (
    "account,symbol,side,date,shares,price\n"
    "tw_broker,2330,buy,2026-02-01,100,510\n"
    "tw_broker,2330,buy,2026-02-02,100,520\n"
    "tw_broker,2330,buy,2026-02-03,100,530\n"
)


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    """Loopback re-enabled PER TEST (pytest-socket re-bans before each one)."""
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


@pytest.mark.e2e
def test_the_csv_import_writes_only_the_ticked_rows(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_golden)
    page = fresh_page
    page.goto(f"{base}/trades.html", wait_until="networkidle")
    page.click("#tab-csv")
    page.fill("#csv-paste", _CSV)
    # The preview is debounced; wait for the three rows rather than for a clock.
    expect(page.locator("#csv-body tr")).to_have_count(3, timeout=20000)

    boxes = page.locator("#csv-body input[type=checkbox]")
    expect(boxes).to_have_count(3)
    boxes.nth(1).uncheck()
    boxes.nth(2).uncheck()
    assert boxes.nth(0).is_checked()

    page.click("#csv-confirm")
    banner = page.locator("#csv-result")
    expect(banner).to_be_visible(timeout=20000)
    text = banner.inner_text()
    assert "成功 1 筆" in text, f"wrote rows the user unticked — banner said: {text}"
    assert "跳過 2 筆" in text, text


@pytest.mark.e2e
def test_unticking_everything_disables_the_confirm_button(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """Previously it enabled on 「anything non-error exists」, so a user who had unticked every
    row was still invited to press a button that would have written all of them."""
    base = flow_server(_seed_golden)
    page = fresh_page
    page.goto(f"{base}/trades.html", wait_until="networkidle")
    page.click("#tab-csv")
    page.fill("#csv-paste", _CSV)
    expect(page.locator("#csv-body tr")).to_have_count(3, timeout=20000)
    expect(page.locator("#csv-confirm")).to_be_enabled()

    boxes = page.locator("#csv-body input[type=checkbox]")
    for i in range(3):
        boxes.nth(i).uncheck()
    expect(page.locator("#csv-confirm")).to_be_disabled()


@pytest.mark.e2e
def test_the_symbol_picker_can_be_driven_from_the_keyboard(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_golden)
    page = fresh_page
    page.goto(f"{base}/trades.html", wait_until="networkidle")
    page.wait_for_function(
        "() => document.querySelector('#m-account')"
        " && document.querySelector('#m-account').options.length > 0")
    page.select_option("#m-account", "tw_broker")

    page.click("#m-symbol")
    page.fill("#m-symbol", "23")
    page.wait_for_timeout(400)          # the picker paints from cache, then refreshes
    page.press("#m-symbol", "ArrowDown")
    page.press("#m-symbol", "Enter")

    assert page.input_value("#m-symbol") == "2330", (
        "ArrowDown+Enter did not select the filtered row — rows are bound on mousedown, "
        "which the keyboard never fires")


def _seed_three_equal_positions(conn: "sqlite3.Connection") -> None:
    """THREE equally-valued holdings, so each weight is 1/3.

    The golden fixture holds two positions whose weights round to a clean 100.0, which is why
    an invariant test over it passes with the defect still present — the fields and the footer
    agree by luck. One third is 33.3 to one decimal place, and three of them sum to 99.9: the
    smallest arrangement that actually makes the two numbers disagree.
    """
    seed_accounts(conn)
    for sym in ("AAA", "BBB", "CCC"):
        upsert_instrument(conn, Instrument(symbol=sym, market=Market.TW,
                                           quote_ccy=Currency.TWD, sector="Test", name=sym))
        insert_transaction(conn, account_id="tw_broker", symbol=sym, side=Side.BUY,
                           quantity=Decimal("100"), price=Decimal("100"),
                           fees=Decimal("0"), tax=Decimal("0"),
                           trade_date=date(2026, 1, 5))
    upsert_prices(conn, [PriceRow(instrument=sym, market=Market.TW, as_of=date(2026, 6, 9),
                                  close=Decimal("100"), source="test")
                         for sym in ("AAA", "BBB", "CCC")], fetched_at=GOLDEN_NOW)
    conn.commit()


@pytest.mark.e2e
def test_the_rebalance_footer_total_equals_the_visible_fields(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """Two numbers about the same thing, on the same screen, must agree.

    Read from the DOM on BOTH sides on purpose: asserting the footer against a recomputed
    expectation would only prove the test can do arithmetic. The defect was that the footer
    and the fields disagreed, so the fields ARE the expectation.
    """
    base = flow_server(_seed_three_equal_positions)
    page = fresh_page
    page.goto(f"{base}/index.html", wait_until="networkidle")
    page.click(".rb-open-btn")
    expect(page.locator(".rb-drawer")).to_be_visible(timeout=20000)
    expect(page.locator(".rb-input").first).to_be_visible(timeout=20000)

    shown = page.eval_on_selector_all(
        ".rb-input", "els => els.map(e => Number(e.value) || 0)")
    assert shown, "the drawer rendered no target-weight fields"
    field_sum = round(sum(shown), 6)

    footer = page.eval_on_selector_all(
        ".rb-kv", "els => els.map(e => e.textContent || '')")
    total_text = next((t for t in footer if "目標合計" in t), "")
    assert total_text, f"no 目標合計 in the footer: {footer}"
    footer_pct = float(total_text.replace("目標合計", "").replace("%", "").strip())

    # Guard the guard: with three equal positions the fields must NOT sum to a clean 100,
    # or this test would pass on a fixture that cannot express the defect.
    assert abs(field_sum - 100.0) > 0.05, (
        f"the fixture rounds too cleanly to detect the defect: {shown}")
    assert abs(footer_pct - field_sum) < 0.05, (
        f"footer says {footer_pct}% but the {len(shown)} visible fields sum to {field_sum}%")


@pytest.mark.e2e
def test_an_oversell_preview_shows_the_discarded_basis_with_its_reason(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """F-01, owner ruling 2026-08-27: show the average the LEDGER will hold, and say why.

    The card used to print 「500.00 → 500.00」 directly above its own warning that the basis
    was about to be permanently discarded. It now prints the zero the replay actually leaves,
    with a note — so the projection agrees with the row the user is about to see AND cannot be
    read as 「your average cost is nothing」.
    """
    base = flow_server(_seed_golden)
    page = fresh_page
    page.goto(f"{base}/trades.html", wait_until="networkidle")
    page.wait_for_function(
        "() => document.querySelector('#m-account')"
        " && document.querySelector('#m-account').options.length > 0")
    page.select_option("#m-account", "tw_broker")
    page.fill("#m-symbol", "2330")
    page.click("#m-side-sell")
    page.fill("#m-date", "2026-06-12")
    page.fill("#m-shares", "1500")          # tw_broker holds 1,000 — undeclared oversell
    page.fill("#m-price", "600")
    # The server preview is debounced; wait for the projection itself, not for a clock.
    page.wait_for_function(
        "() => (document.querySelector('#m-pc-rows') || {}).textContent"
        "  && document.querySelector('#m-pc-rows').textContent.indexOf('調整均價') >= 0",
        timeout=20000)

    rows = page.locator("#m-pc-rows").inner_text()
    assert "基礎已捨棄" in rows, f"the zero is unexplained: {rows}"
    # The OLD averages are still shown, so the reader sees what is being discarded. They
    # DIFFER from each other in this fixture (2330 carries a 5,000 dividend, so the adjusted
    # basis is 495 against an original 500) — which is itself worth asserting: a projection
    # that collapsed both to one number would pass a laxer check.
    assert "500.00" in rows and "495.00" in rows, rows

    # Each average must project to the ledger's ZERO, not repeat its own pre-trade value.
    cells = page.eval_on_selector_all(
        "#m-pc-rows .pc-row", "els => els.map(e => e.textContent || '')")
    for label, old_avg in (("原始均價", "500.00"), ("調整均價", "495.00")):
        row = next((t for t in cells if label in t), "")
        assert row, f"no {label} row: {cells}"
        before, _, after = row.partition("→")
        assert old_avg in before, f"{label} lost its pre-trade value: {row!r}"
        assert after.strip().startswith("0.00"), (
            f"{label} still projects the pre-trade average: {row!r}")
