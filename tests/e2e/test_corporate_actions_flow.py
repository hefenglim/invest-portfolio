"""E2E (Playwright, real server + real frontend): corporate actions ENTERED THROUGH THE UI.

Spec 2026-08-06 §7.6 in full: "Enter each kind through the UI; assert the resulting
position, the drawer's ``＋公司行動`` term, the footer reading ✓ 對帳一致, and that a sell
which was previously blocked as an oversell now passes validation."

What makes this file different from ``test_symbol_drawer_tx_chart_flow.py``'s W5 tests: those
seed the action with ``insert_corporate_action`` and then assert how the drawer RENDERS it.
Nothing there ever opened the form. Here every action is typed into ``web/corp-action-form.js``
— the ONE form §6.7's three doors share — by a real browser, so the entry surface, the
always-on preview, the save path, the ledger refresh and the drawer are exercised as one
chain. A defect anywhere in it reddens a test here and nowhere else.

The three doors of §6.7, each covered:

  door 1  the 賣超 confirm dialog   → ``test_door1_...`` (＋the §1 scenario: a blocked sell)
  door 2  the symbol drawer footer  → ``test_door2_...`` (＋its negative control)
  door 3  the 5th ledger tab        → SPLIT / EXCHANGE / SPINOFF, one test each

Plus D13/D28's multi-account preview, E23's one-click convert-to-SPLIT (which had a
source-scanning contract test but had never been RUN), and the CSV template round trip.

⚠ **Door 1's dialog is not reachable from the manual form's own preview flow** — see
``test_door1_...``'s docstring. The dialog is the fallback for a preview/commit
DISAGREEMENT, and this file produces that disagreement the way a real deployment does: a
second surface writes to the same ledger while the form's preview sits stale. That is a
finding about the app, recorded here because the test had to be built around it.

Browser contexts come from the shared ``fresh_page`` fixture, which installs issue #67's
third-party stub; ``tests/e2e/test_thirdparty_isolation.py`` fails the build if any context
in this directory is built without it.
"""

import json
import sqlite3
import urllib.request
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from playwright.sync_api import Locator, Page, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_corporate_action,
    insert_transaction,
    upsert_instrument,
)
from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.pricing.store import upsert_fx, upsert_prices
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import GOLDEN_NOW
from tests.e2e.conftest import FlowServerFactory

ACTION_DAY = date(2026, 3, 16)
PRICE_DAY = date(2026, 6, 9)


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    """Re-enable loopback sockets PER TEST (pytest-socket re-bans before every test); each
    flow spawns a fresh isolated uvicorn (free-port probe + readiness poll need loopback)."""
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


# --------------------------------------------------------------------------- plumbing

def _get_json(base_url: str, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(base_url + path, timeout=10) as r:  # noqa: S310 (loopback)
        data: dict[str, Any] = json.loads(r.read().decode("utf-8"))
        return data


def _post_json(base_url: str, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """POST as a SECOND surface against the same ledger (used to age the browser's preview)."""
    req = urllib.request.Request(  # noqa: S310 (loopback)
        base_url + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 (loopback)
        payload: dict[str, Any] = json.loads(r.read().decode("utf-8"))
        return r.status, payload


def _holding(base_url: str, symbol: str, account_id: str | None = None) -> dict[str, Any] | None:
    for h in _get_json(base_url, "/api/dashboard")["holdings"]:
        if h["symbol"] == symbol and (account_id is None or h["account_id"] == account_id):
            row: dict[str, Any] = h
            return row
    return None


def _sink(page: Page) -> tuple[list[str], list[str]]:
    """Console-error + pageerror sinks. Chromium logs every 4xx/5xx response as a console
    error, so flows that EXPECT a refusal filter this list by substring rather than
    dropping the assertion (a dropped assertion covers every other console error too)."""
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(getattr(m, "text", ""))
            if getattr(m, "type", None) == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    return console_errors, page_errors


def _unexpected(console_errors: list[str], *allowed: str) -> list[str]:
    return [e for e in console_errors if not any(a in e for a in allowed)]


def _fx(conn: sqlite3.Connection) -> None:
    """FX rows covering any reporting currency the live server may default to."""
    upsert_fx(conn, [
        FxRow(base=Currency.USD, quote=Currency.TWD, as_of=PRICE_DAY,
              rate=Decimal("33"), source="test"),
        FxRow(base=Currency.MYR, quote=Currency.TWD, as_of=PRICE_DAY,
              rate=Decimal("7"), source="test"),
        FxRow(base=Currency.USD, quote=Currency.MYR, as_of=PRICE_DAY,
              rate=Decimal("4.4"), source="test"),
    ], fetched_at=GOLDEN_NOW)


def _tw(conn: sqlite3.Connection, symbol: str, name: str) -> None:
    upsert_instrument(conn, Instrument(symbol=symbol, market=Market.TW,
                                       quote_ccy=Currency.TWD, sector="Semiconductors",
                                       name=name, board="TWSE"))


def _us(conn: sqlite3.Connection, symbol: str, name: str) -> None:
    upsert_instrument(conn, Instrument(symbol=symbol, market=Market.US,
                                       quote_ccy=Currency.USD, sector="Tech", name=name))


# ------------------------------------------------------------------------------ seeds

def _seed_hundred_shares(conn: sqlite3.Connection) -> None:
    """§1's own scenario, before its action: 100 shares of one symbol in one account.

    A sell of 400 against it is the oversell the whole feature exists to prevent — and the
    7-for-1 SPLIT entered through the UI is what makes that sell legal.
    """
    seed_accounts(conn)
    _tw(conn, "2330", "TSMC")
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=Decimal("100"), price=Decimal("500"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 5))
    upsert_prices(conn, [PriceRow(instrument="2330", market=Market.TW, as_of=PRICE_DAY,
                                  close=Decimal("600"), source="test")], fetched_at=GOLDEN_NOW)
    _fx(conn)
    conn.commit()


def _seed_thousand_shares(conn: sqlite3.Connection) -> None:
    """1,000 shares in one account — the door-1 and E23 ledgers."""
    seed_accounts(conn)
    _tw(conn, "2330", "TSMC")
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=Decimal("1000"), price=Decimal("500"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 5))
    upsert_prices(conn, [PriceRow(instrument="2330", market=Market.TW, as_of=PRICE_DAY,
                                  close=Decimal("600"), source="test")], fetched_at=GOLDEN_NOW)
    _fx(conn)
    conn.commit()


def _seed_broker_identifier(conn: sqlite3.Connection) -> None:
    """E23's own state: a raw broker identifier registered as an instrument, with NO prices.

    D19 says such a string never becomes an instrument; E23 guards the ledgers written
    BEFORE that rule existed, where E10 passes it because it really is registered. The
    position sits under the TICKER, which is what makes the convert-to-SPLIT committable.
    """
    _seed_thousand_shares(conn)
    _tw(conn, "TWX00001", "broker internal code")
    conn.commit()


def _seed_two_account_position(conn: sqlite3.Connection) -> None:
    """ONE symbol genuinely held in TWO accounts, plus a registered+priced destination.

    D13/D28's whole subject: the owner opens the form on one account and E13 writes N rows.
    The two buys are dated two days apart so a date BETWEEN them makes the later holder
    「不受影響」 — the second half of §6.7's preview rule, reachable by typing a date.
    """
    seed_accounts(conn)
    _us(conn, "AAPL", "Apple")
    _us(conn, "NEWA", "Newco A")
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("30"), price=Decimal("100"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 10))
    insert_transaction(conn, account_id="moomoo_my", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("10"), price=Decimal("110"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 12))
    upsert_prices(conn, [
        PriceRow(instrument="AAPL", market=Market.US, as_of=PRICE_DAY,
                 close=Decimal("120"), source="test"),
        # Priced BEFORE the action date, so N3-price stays silent and the save needs no ack.
        PriceRow(instrument="NEWA", market=Market.US, as_of=date(2026, 1, 2),
                 close=Decimal("50"), source="test"),
        PriceRow(instrument="NEWA", market=Market.US, as_of=PRICE_DAY,
                 close=Decimal("60"), source="test"),
    ], fetched_at=GOLDEN_NOW)
    _fx(conn)
    conn.commit()


def _seed_spinoff_parent(conn: sqlite3.Connection) -> None:
    """A parent position plus a registered, priced child — §4.3's carve, entered by hand."""
    _seed_thousand_shares(conn)
    _tw(conn, "2330B", "TSMC Spinco")
    upsert_prices(conn, [PriceRow(instrument="2330B", market=Market.TW, as_of=PRICE_DAY,
                                  close=Decimal("120"), source="test")], fetched_at=GOLDEN_NOW)
    conn.commit()


def _seed_red_footer(conn: sqlite3.Connection) -> None:
    """The ONLY ledger shape that puts a ⚠ 對帳不一致 in the drawer — door 2's precondition.

    A STICKY 賣超 position carrying a SPLIT the replay REFUSES (E3): the share-only path
    applies the split (it cannot see basis state), the replay does not, so the two disagree
    and the footer legitimately reads red (§6.3: "a position whose basis was discarded
    genuinely does not reconcile"). AAPL is the negative control — a clean position in
    another account whose footer is green and must therefore carry NO repair button.
    """
    seed_accounts(conn)
    _tw(conn, "2330", "TSMC")
    _us(conn, "AAPL", "Apple")
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.SELL,
                       quantity=Decimal("100"), price=Decimal("500"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 5))
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=Decimal("300"), price=Decimal("500"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 2, 5))
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("10"), price=Decimal("100"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 10))
    insert_corporate_action(conn, account_id="tw_broker", action_date=ACTION_DAY,
                            kind=CorporateActionKind.SPLIT, from_symbol="2330",
                            to_symbol="2330", ratio_to=Decimal("2"), ratio_from=Decimal("1"))
    upsert_prices(conn, [
        PriceRow(instrument="2330", market=Market.TW, as_of=PRICE_DAY,
                 close=Decimal("300"), source="test"),
        PriceRow(instrument="AAPL", market=Market.US, as_of=PRICE_DAY,
                 close=Decimal("120"), source="test"),
    ], fetched_at=GOLDEN_NOW)
    _fx(conn)
    conn.commit()


def _seed_csv_target(conn: sqlite3.Connection) -> None:
    """The ledger the shipped corporate-action CSV TEMPLATE points at.

    Its first example row is a 10-for-1 SPLIT of 2330 in tw_broker; the other two name
    placeholder destinations on purpose (E10 refuses them). Registering AAPL keeps those two
    refused for ONE reason — the destination — rather than two.
    """
    _seed_thousand_shares(conn)
    _us(conn, "AAPL", "Apple")
    upsert_prices(conn, [PriceRow(instrument="AAPL", market=Market.US, as_of=PRICE_DAY,
                                  close=Decimal("120"), source="test")], fetched_at=GOLDEN_NOW)
    conn.commit()


# --------------------------------------------------------------- form driving helpers

def _tall_viewport(page: Page) -> None:
    """⚠ **A workaround for a shipped layout defect — do not quietly delete this line.**

    ``.modal`` (web/styles.css) gets ``max-height: 88dvh; overflow-y: auto`` ONLY inside the
    narrow-width media query, and ``.modal-backdrop`` is ``position: fixed`` with
    ``align-items: center``. So on a DESKTOP-width viewport a modal taller than the window
    overflows both ends with nothing to scroll — and 補登公司行動 is the tallest modal in the
    app, because §6.7 mandates an always-on preview that grows one block per holding account.

    Measured 2026-08-12 at the Playwright default 1280×720, driving the SPLIT flow below:
    modal height 804px, top −42px, 登錄公司行動's bottom edge 41px BELOW the fold, computed
    ``overflow-y: visible`` / ``max-height: none``. Playwright scrolled, reported "element is
    outside of the viewport", and timed out — i.e. a real owner on a 720px-tall window cannot
    save a corporate action at all. Reported, NOT fixed: web/ is outside this task's scope.

    These tests therefore run in a window tall enough for the form, so they measure the
    corporate-action feature rather than that CSS bug. Reproduce the bug by lowering this.
    """
    page.set_viewport_size({"width": 1440, "height": 1200})


def _open_ledger_tab(page: Page, base: str) -> None:
    """Door 3: the 公司行動 tab of the 帳本記錄 section on trades.html."""
    _tall_viewport(page)
    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#m-account option", state="attached")
    page.click("#tab-laction")
    page.wait_for_selector("#pane-laction.active")


def _open_form_from_door3(page: Page, base: str) -> Locator:
    _open_ledger_tab(page, base)
    page.click("#action-add")
    modal = page.locator(".ca-modal")
    expect(modal).to_be_visible()
    return modal


def _kind_button(modal: Locator, index: int) -> Locator:
    """0 = 同一檔股票…(SPLIT) · 1 = 整個部位換成…(EXCHANGE) · 2 = 原持股不變…(SPINOFF)."""
    return modal.locator(".ca-kind").nth(index)


def _symbol_box(modal: Locator) -> Locator:
    return modal.locator(".ca-grid input").nth(0)


def _date_box(modal: Locator) -> Locator:
    return modal.locator(".ca-grid input").nth(1)


def _ratio_from(modal: Locator) -> Locator:
    """每持有 N 股 — the LEFT integer box (ratio_from)."""
    return modal.locator(".ca-int").nth(0)


def _ratio_to(modal: Locator) -> Locator:
    """→ 變成／換得／另外配發 N 股 — the RIGHT integer box (ratio_to)."""
    return modal.locator(".ca-int").nth(1)


def _to_symbol_box(modal: Locator) -> Locator:
    return modal.locator('.ca-ratio input[placeholder="新代號"]')


def _field_input(modal: Locator, label_text: str) -> Locator:
    return modal.locator(".field").filter(has_text=label_text).locator("input")


def _save_button(modal: Locator) -> Locator:
    return modal.locator(".modal-foot .btn-primary")


def _account_card(modal: Locator, account_name: str) -> Locator:
    """One account's before/after block, addressed BY NAME.

    Not by index: the server writes the E13 batch in ``sorted()`` account-id order, which is
    not the order the owner typed and not one this test should encode.
    """
    return modal.locator(".ca-acct").filter(has_text=account_name)


def _preview_row(modal: Locator, account_index: int, row_index: int) -> Locator:
    return modal.locator(".ca-acct").nth(account_index).locator("tbody tr").nth(row_index)


def _combined_conserve(modal: Locator) -> Locator:
    """The ALL-ACCOUNTS 成本合計 verdict — a direct child of the preview host, where the
    per-account ones live inside their own ``.ca-acct`` card."""
    return modal.locator(".ca-preview > .ca-conserve")


def _cells(row: Locator) -> list[str]:
    texts: list[str] = row.locator("td").all_inner_texts()
    return [t.strip() for t in texts]


def _open_drawer(page: Page, base: str, symbol: str) -> Locator:
    """Dashboard → symbol drawer → its reconciliation footer locator."""
    if not page.url.endswith("/index.html"):
        page.goto(base + "/index.html", wait_until="load")
        page.wait_for_selector(".kpi-card")
    with page.expect_response(f"**/api/symbol/{symbol}/detail") as resp:
        page.evaluate("(s) => window.pdOpenSymbol(s)", symbol)
    assert resp.value.status == 200, f"{symbol} detail status {resp.value.status}"
    page.wait_for_selector(".sd-tx-section .sd-tx-reconcile")
    return page.locator(".sd-tx-section .sd-tx-reconcile")


# ============================================================ door 3 — the 5th ledger tab


@pytest.mark.e2e
def test_door3_split_entered_by_hand_unblocks_a_previously_refused_sell(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """§7.6 end to end on §1's own numbers: buy 100, 7-for-1, sell 400.

    Four clauses in one flow, in the order the owner meets them:
      1. the sell of 400 is REFUSED first (the ack checkbox appears, 確認寫入 is disabled),
      2. the SPLIT is typed into the shared form and its always-on preview states 成本不變,
      3. the same sell then passes with NO acknowledgement at all — the clause §7.6 calls
         the point of the feature, and the one a test of the plumbing alone would miss,
      4. the drawer's footer closes on the ＋公司行動 term and reads ✓ 對帳一致.
    """
    base = flow_server(_seed_hundred_shares)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    # --- 1. the sell that cannot be written -------------------------------------------
    _tall_viewport(page)
    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#m-account option", state="attached")
    page.select_option("#m-account", "tw_broker")
    page.click("#m-side-sell")
    page.fill("#m-symbol", "2330")
    page.fill("#m-shares", "400")
    with page.expect_response("**/api/input/manual/preview") as pv:
        page.fill("#m-price", "600")
    assert pv.value.status == 200
    page.wait_for_selector("#m-ack")                      # 400 > 100 held -> 賣超
    expect(page.locator("#m-confirm")).to_be_disabled()

    # --- 2. the repair, typed into the ONE shared form (door 3) ------------------------
    page.click("#tab-laction")
    page.wait_for_selector("#pane-laction.active")
    page.click("#action-add")
    modal = page.locator(".ca-modal")
    expect(modal).to_be_visible()
    modal.locator(".ca-grid select").select_option("tw_broker")
    _symbol_box(modal).fill("2330")
    _date_box(modal).fill(ACTION_DAY.isoformat())
    _ratio_from(modal).fill("1")
    with page.expect_response("**/api/ledgers/corporate-actions/preview") as prev:
        _ratio_to(modal).fill("7")
    assert prev.value.status == 200

    # The preview is the conservation law made visible: one account, 行動前 / 行動後, and a
    # cost total that did NOT move. Every figure is a server Decimal string.
    expect(modal.locator(".ca-acct")).to_have_count(1)
    assert _cells(_preview_row(modal, 0, 0))[:3] == ["行動前", "2330", "100"]
    after = _cells(_preview_row(modal, 0, 1))
    assert after[:3] == ["行動後", "2330", "700"], after
    assert after[4] == "50,000", after            # 成本總額 unchanged (TWD, 0 dp)
    expect(modal.locator(".ca-conserve.ok")).to_contain_text("成本不變")

    with page.expect_response("**/api/ledgers/corporate-actions") as saved:
        _save_button(modal).click()
    assert saved.value.status == 201, saved.value.text()
    expect(modal).to_have_count(0)                # the form closes on success
    expect(page.locator("#action-body tr")).to_have_count(1)   # door 3's table refreshed

    # --- the asserted position: shares re-denominated, ORIGINAL COST untouched ---------
    held = _holding(base, "2330")
    assert held is not None
    assert Decimal(held["shares"]) == Decimal("700")
    assert Decimal(held["original_cost_total"]) == Decimal("50000")

    # --- 3. the same sell, now legal ---------------------------------------------------
    page.click("#tab-manual")
    with page.expect_response("**/api/input/manual/preview") as pv2:
        page.click("#m-side-sell")                # a no-op state change that re-previews
    assert pv2.value.status == 200
    page.wait_for_function(
        "() => { const b = document.querySelector('#m-confirm'); return b && !b.disabled; }"
    )
    expect(page.locator("#m-ack")).to_have_count(0)   # no acknowledgement was ever needed
    with page.expect_response("**/api/input/manual/commit") as cm:
        page.click("#m-confirm")
    assert cm.value.status == 201, cm.value.text()

    # --- 4. the drawer -----------------------------------------------------------------
    foot = _open_drawer(page, base, "2330")
    expect(foot).to_contain_text("＋買 100")
    expect(foot).to_contain_text("−賣 400")
    expect(foot).to_contain_text("＋公司行動 600")
    expect(foot).to_contain_text("部位摘要 300 股")
    expect(foot).to_contain_text("✓ 對帳一致")
    expect(page.locator(".sd-tx-section .sd-tx-issue")).to_have_count(0)

    assert not console_errors and not page_errors, (
        f"door 3 SPLIT: console={console_errors!r} page={page_errors!r}"
    )


@pytest.mark.e2e
def test_door3_exchange_states_its_whole_multi_account_scope_before_it_writes(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """D13/D28 in the browser: the owner sees the FULL scope before committing, never after.

    The owner opens the form on one account and E13 writes N rows. Three things must be on
    screen before 登錄公司行動 is pressed — the scope banner naming the count, one
    before/after block PER account, and a combined 成本合計不變 across all of them — and a
    fourth appears the moment the date moves back between the two purchases: the account the
    action does NOT reach, named. Showing the untouched account by name is how the owner can
    tell the system read their ledger rather than merely applied a rule (§6.7).
    """
    base = flow_server(_seed_two_account_position)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    modal = _open_form_from_door3(page, base)
    _kind_button(modal, 1).click()                          # 整個部位換成另一檔股票
    modal.locator(".ca-grid select").select_option("schwab")
    _symbol_box(modal).fill("AAPL")
    _date_box(modal).fill(ACTION_DAY.isoformat())
    _ratio_from(modal).fill("1")
    _ratio_to(modal).fill("1")
    with page.expect_response("**/api/ledgers/corporate-actions/preview") as prev:
        _to_symbol_box(modal).fill("NEWA")
    assert prev.value.status == 200

    expect(modal.locator(".ca-scope")).to_contain_text("同時影響以下 2 個帳戶")
    expect(modal.locator(".ca-acct")).to_have_count(2)
    # schwab: 30 @100 -> 30 NEWA @100. The SOURCE goes to zero and the DESTINATION carries
    # the whole basis; both rows are rendered, so the reader can see nothing was created.
    schwab_rows = _account_card(modal, "Charles Schwab").locator("tbody tr")
    assert _cells(schwab_rows.nth(2))[:3] == ["行動後", "AAPL", "0"]
    child = _cells(schwab_rows.nth(3))
    assert child[1:3] == ["NEWA", "30"], child
    assert child[4] == "3,000.00", child                     # USD, 2 dp
    # …and the other holder is there too, on its own basis — one event, two accounts.
    moomoo_rows = _account_card(modal, "Moomoo MY").locator("tbody tr")
    assert _cells(moomoo_rows.nth(3))[1:3] == ["NEWA", "10"]
    expect(_combined_conserve(modal)).to_have_class("ca-conserve ok")
    expect(_combined_conserve(modal)).to_contain_text("全部帳戶成本合計不變")
    expect(_combined_conserve(modal)).to_contain_text("4,100.00")

    # Move the date BETWEEN the two buys: one holder on the day, one that opens later.
    with page.expect_response("**/api/ledgers/corporate-actions/preview") as prev2:
        _date_box(modal).fill("2026-01-11")
    assert prev2.value.status == 200
    expect(modal.locator(".ca-acct")).to_have_count(1)
    expect(modal.locator(".ca-scope")).to_have_count(0)
    expect(modal.locator(".ca-preview .hint")).to_contain_text("不受影響")
    expect(modal.locator(".ca-preview .hint")).to_contain_text("Moomoo MY")

    with page.expect_response("**/api/ledgers/corporate-actions/preview") as prev3:
        _date_box(modal).fill(ACTION_DAY.isoformat())
    assert prev3.value.status == 200
    expect(modal.locator(".ca-acct")).to_have_count(2)

    with page.expect_response("**/api/ledgers/corporate-actions") as saved:
        _save_button(modal).click()
    assert saved.value.status == 201, saved.value.text()
    assert saved.value.json()["written"] == 2               # ONE event, N rows (D13)
    expect(page.locator("#action-body tr")).to_have_count(2)

    for account_id, shares, cost in (("schwab", "30", "3000"), ("moomoo_my", "10", "1100")):
        held = _holding(base, "NEWA", account_id)
        assert held is not None, f"NEWA missing for {account_id}"
        assert Decimal(held["shares"]) == Decimal(shares)
        assert Decimal(held["original_cost_total"]) == Decimal(cost)
    assert _holding(base, "AAPL") is None                    # the source is empty

    dest = _open_drawer(page, base, "NEWA")
    expect(dest).to_contain_text("＋公司行動 40")             # 30 + 10, aggregated
    expect(dest).to_contain_text("部位摘要 40 股")
    expect(dest).to_contain_text("✓ 對帳一致")
    src = _open_drawer(page, base, "AAPL")
    expect(src).to_contain_text("−公司行動 40")               # the sign rides the OPERATOR
    expect(src).to_contain_text("部位摘要 0 股")
    expect(src).to_contain_text("✓ 對帳一致")

    assert not console_errors and not page_errors, (
        f"door 3 EXCHANGE: console={console_errors!r} page={page_errors!r}"
    )


@pytest.mark.e2e
def test_door3_spinoff_carves_the_cost_and_the_child_reconciles_from_nothing(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """§4.3 by hand: the parent keeps every share, the CHILD is created out of cost alone.

    The child has no transaction, no opening and no dividend — its entire share count comes
    from the corporate action, so its footer is the purest possible statement of the
    ＋公司行動 term. The parent is the mirror assertion: its share count did not move, so its
    term is zero and must NOT be rendered (a 「＋公司行動 0」 explains nothing).
    """
    base = flow_server(_seed_spinoff_parent)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    modal = _open_form_from_door3(page, base)
    _kind_button(modal, 2).click()                          # 原持股不變，另外多拿到一檔新股票
    modal.locator(".ca-grid select").select_option("tw_broker")
    _symbol_box(modal).fill("2330")
    _date_box(modal).fill(ACTION_DAY.isoformat())
    _ratio_from(modal).fill("4")                            # 每持有 4 股 → 另外配發 1 股
    _ratio_to(modal).fill("1")
    _to_symbol_box(modal).fill("2330B")
    with page.expect_response("**/api/ledgers/corporate-actions/preview") as prev:
        _field_input(modal, "成本分攤比例").fill("0.25")
    assert prev.value.status == 200

    parent = _cells(_preview_row(modal, 0, 2))
    child = _cells(_preview_row(modal, 0, 3))
    assert parent[:3] == ["行動後", "2330", "1,000"], parent
    assert parent[4] == "375,000", parent                   # 500,000 × (1 − 0.25)
    assert child[1:3] == ["2330B", "250"], child            # 1,000 ÷ 4
    assert child[4] == "125,000", child
    # Two after-rows -> the verdict is about the SUM, and it says so in those words.
    expect(modal.locator(".ca-conserve.ok")).to_contain_text("成本合計不變")

    with page.expect_response("**/api/ledgers/corporate-actions") as saved:
        _save_button(modal).click()
    assert saved.value.status == 201, saved.value.text()

    held_parent = _holding(base, "2330")
    held_child = _holding(base, "2330B")
    assert held_parent is not None and held_child is not None
    assert Decimal(held_parent["shares"]) == Decimal("1000")
    assert Decimal(held_parent["original_cost_total"]) == Decimal("375000")
    assert Decimal(held_child["shares"]) == Decimal("250")
    assert Decimal(held_child["original_cost_total"]) == Decimal("125000")

    child_foot = _open_drawer(page, base, "2330B")
    expect(child_foot).to_contain_text("期初 0 ＋買 0 −賣 0")
    expect(child_foot).to_contain_text("＋公司行動 250")
    expect(child_foot).to_contain_text("部位摘要 250 股")
    expect(child_foot).to_contain_text("✓ 對帳一致")

    parent_foot = _open_drawer(page, base, "2330")
    expect(parent_foot).to_contain_text("部位摘要 1,000 股")
    expect(parent_foot).to_contain_text("✓ 對帳一致")
    expect(parent_foot).not_to_contain_text("公司行動 ")      # zero term -> no term at all

    assert not console_errors and not page_errors, (
        f"door 3 SPINOFF: console={console_errors!r} page={page_errors!r}"
    )


# ================================================================== door 1 — the 賣超 dialog


@pytest.mark.e2e
def test_door1_oversell_dialog_offers_the_repair_first_and_the_sell_then_lands(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """§6.7 door 1: the most destructive confirmation in the system, as a guided repair.

    ⚠ **How this flow is reached, and why it is not the obvious one.** ``web/input.js``
    renders its own 賣超 acknowledgement INSIDE the preview (``#m-ack``) and holds 確認寫入
    disabled until it is ticked — at which point the commit carries ``ack_oversell: true``
    and the server never answers 422 ``oversell_unacknowledged``. The three-option dialog is
    wired to that 422 alone, so on the ordinary path it never opens: the owner ticks the box,
    the basis is discarded, and 補登公司行動 is never offered. (Reported, not fixed — the
    frontend is outside this task's file scope.)

    The 422 IS reachable, and this test reaches it the way a real deployment would: a second
    surface writes to the same ledger while the browser's preview sits stale. That is not a
    contrivance — this app is explicitly a 1–2 user system with an API, a CSV importer and a
    scheduler all writing the same file, and the dialog exists precisely as the fallback for
    that disagreement.

    Asserted here: the option ORDER (§6.7 lists 補登公司行動 first, because 確認 discards the
    cost basis permanently), that the repair opens the SAME shared form pre-filled from the
    sell, that the date window is bounded by the sell's own trade date, and that the sell
    which triggered the dialog goes through afterwards — untouched, and with no ack.
    """
    base = flow_server(_seed_thousand_shares)
    page = fresh_page
    console_errors, page_errors = _sink(page)
    # Chromium's console line for a failed fetch names only the STATUS, never the URL, so the
    # console filter alone could not tell this flow's one deliberate 422 from any other. The
    # URL-level ledger below closes that: exactly one non-2xx response is permitted, and it
    # has to be the commit that opens the dialog.
    refusals: list[str] = []
    page.on("response", lambda r: refusals.append(f"{r.status} {r.url}") if r.status >= 400
            else None)

    _tall_viewport(page)
    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#m-account option", state="attached")
    page.select_option("#m-account", "tw_broker")
    page.click("#m-side-sell")
    page.fill("#m-symbol", "2330")
    page.fill("#m-shares", "1000")                       # exactly the holding -> clean
    with page.expect_response("**/api/input/manual/preview") as pv:
        page.fill("#m-price", "600")
    assert pv.value.status == 200
    page.wait_for_function(
        "() => { const b = document.querySelector('#m-confirm'); return b && !b.disabled; }"
    )
    expect(page.locator("#m-ack")).to_have_count(0)
    sell_date = page.input_value("#m-date")

    # A second surface sells 900 of the same position, BACK-DATED to before the action this
    # flow is about to enter. The browser is not told; its preview (and therefore its enabled
    # 確認寫入) is now stale by exactly one oversell. The back-date is deliberate: a disposal
    # dated AFTER the action removes cost at the post-action average, so the preview's
    # before/after cost totals legitimately differ and the 成本不變 verdict is not the thing
    # under test here (see the report's note on that reading).
    status, _ = _post_json(base, "/api/input/manual/commit", {
        "account_id": "tw_broker", "symbol": "2330", "side": "sell",
        "date": "2026-02-01", "shares": "900", "price": "600"})
    assert status == 201

    with page.expect_response("**/api/input/manual/commit") as refused:
        page.click("#m-confirm")
    assert refused.value.status == 422, refused.value.text()

    options = page.locator(".os-options button")
    expect(options).to_have_count(3)
    expect(options.nth(0)).to_contain_text("補登公司行動")     # FIRST, per §6.7
    expect(options.nth(1)).to_contain_text("確認為賣超")
    expect(options.nth(2)).to_contain_text("取消")

    options.nth(0).click()
    modal = page.locator(".ca-modal")
    expect(modal).to_be_visible()
    expect(modal.locator(".ca-reason")).to_contain_text("賣超")
    expect(_symbol_box(modal)).to_have_value("2330")          # pre-filled from the sell
    assert _date_box(modal).get_attribute("max") == sell_date  # the window §6.7 mandates
    expect(modal.locator(".ca-grid select")).to_have_value("tw_broker")

    _date_box(modal).fill(ACTION_DAY.isoformat())
    _ratio_from(modal).fill("1")
    with page.expect_response("**/api/ledgers/corporate-actions/preview") as prev:
        _ratio_to(modal).fill("20")
    assert prev.value.status == 200
    after = _cells(_preview_row(modal, 0, 1))
    assert after[:3] == ["行動後", "2330", "2,000"], after     # the 100 that survive, ×20
    assert after[4] == "50,000", after
    expect(modal.locator(".ca-conserve.ok")).to_contain_text("成本不變")

    # Saving re-runs the manual preview through the form's own onSaved hook — the repaired
    # sell must come back legal WITHOUT the owner touching the form again.
    with page.expect_response("**/api/input/manual/preview") as pv2:
        _save_button(modal).click()
    assert pv2.value.status == 200
    page.wait_for_function(
        "() => { const b = document.querySelector('#m-confirm'); return b && !b.disabled; }"
    )
    expect(page.locator("#m-ack")).to_have_count(0)
    with page.expect_response("**/api/input/manual/commit") as cm:
        page.click("#m-confirm")
    assert cm.value.status == 201, cm.value.text()

    held = _holding(base, "2330")
    assert held is not None
    assert Decimal(held["shares"]) == Decimal("1000")     # (1,000 − 900) ×20 − 1,000

    foot = _open_drawer(page, base, "2330")
    expect(foot).to_contain_text("−賣 1,900")
    expect(foot).to_contain_text("＋公司行動 1,900")
    expect(foot).to_contain_text("部位摘要 1,000 股")
    expect(foot).to_contain_text("✓ 對帳一致")

    # Exactly one refusal, and it is the one this flow exists to provoke.
    assert [r.split(" ", 1)[0] for r in refusals] == ["422"], refusals
    assert refusals[0].endswith("/api/input/manual/commit"), refusals
    assert not _unexpected(console_errors, "status of 422") and not page_errors, (
        f"door 1: console={console_errors!r} page={page_errors!r}"
    )


# ================================================================== door 2 — the drawer


@pytest.mark.e2e
def test_door2_offers_the_repair_beside_a_red_footer_and_nowhere_else(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """§6.7 door 2: 補登公司行動 appears BESIDE the mismatch, and only there.

    Two halves, and the second is the one that makes the first mean anything: on a ⚠ 對帳不一致
    footer the button renders and opens the SAME shared form pre-filled from that symbol; on a
    ✓ 對帳一致 footer it does not render at all, because a repair button on a healthy position
    is a control with nothing to fix.

    The form then tells the truth about this particular ledger rather than pretending. The
    only state that reddens a footer is one where the replay refused an action, and on this
    ledger it refused because the position's basis had already been discarded — so a NEW
    action on it is refused for the same reason (``oversold_source``: 「成本基礎已被捨棄，
    無法套用公司行動。請先補登缺少的買進或期初庫存」), named in full with 登錄公司行動 held
    disabled. A door that opened onto a form which fails only on save would be the "button
    that ends in an error" §6.7 rejects elsewhere.
    """
    base = flow_server(_seed_red_footer)
    page = fresh_page
    _, page_errors = _sink(page)
    # On ANY ledger holding a 賣超 position `POST /api/whatif` answers 500 (the drawer's 試算
    # posts on open): `strategy/whatif.py` replays with the STRICT build_book, which raises
    # OversellError. Pre-existing and ledger-wide — see the same note in
    # test_symbol_drawer_tx_chart_flow.py. Filtered by URL so any OTHER 5xx still fails.
    bad: list[str] = []
    page.on("response", lambda r: bad.append(r.url) if r.status >= 500 else None)

    _tall_viewport(page)
    foot = _open_drawer(page, base, "2330")
    expect(foot).to_contain_text("⚠ 對帳不一致")
    door = foot.locator("button", has_text="補登公司行動")
    expect(door).to_be_visible()

    door.click()
    modal = page.locator(".ca-modal")
    expect(modal).to_be_visible()
    expect(modal.locator(".ca-reason")).to_contain_text("對帳不一致")
    expect(_symbol_box(modal)).to_have_value("2330")
    expect(_save_button(modal)).to_be_disabled()          # nothing previewed yet

    # Door 2 prefills the drawer's account filter, and an UNFILTERED drawer passes '' — so
    # the select lands on whichever account /api/accounts returns first, which need not be
    # the one holding the symbol. Picking the holder is what the owner does here, and it is
    # also what makes the refusal below a single, attributable finding rather than one per
    # account (E13 always includes the submitting account, so a non-holder adds its own E1a).
    modal.locator(".ca-grid select").select_option("tw_broker")
    _date_box(modal).fill("2026-04-01")
    _ratio_from(modal).fill("1")
    with page.expect_response("**/api/ledgers/corporate-actions/preview") as prev:
        _ratio_to(modal).fill("3")
    assert prev.value.status == 200
    # The refusal is NAMED, not merely rendered red: it says the basis was discarded and what
    # to do instead. Asserting the sentence rather than the class is the difference between a
    # test that survives the message being replaced by an empty box and one that does not.
    issue = modal.locator(".ca-issue-error")
    expect(issue).to_have_count(1)
    expect(issue).to_contain_text("成本基礎已被捨棄")
    expect(issue).to_contain_text("請先補登缺少的買進或期初庫存")
    expect(_save_button(modal)).to_be_disabled()

    # ⚠ **The form opens UNDERNEATH the drawer that launched it** — `.modal-backdrop` is
    # z-index 60 (web/styles.css:416) and `.sd-backdrop` is 70 (web/detail.css:4), so every
    # control on the form that overlaps the drawer is inert. Measured 2026-08-12: clicking
    # 取消 fails with "<div class=\"sd-tx-reconcile\"> from <div class=\"sd-backdrop\">
    # subtree intercepts pointer events", and because the interceptor is the drawer's own
    # CONTENT (not the bare backdrop, which does close on click) a real owner's click does
    # nothing at all. Reported, not fixed: web/ is outside this task's scope. Esc — the
    # recovery a user is forced into — dismisses the drawer, and the SAME button then takes a
    # real hit-tested click, which is the cleanest proof that stacking is the only thing
    # wrong with door 2's form.
    page.keyboard.press("Escape")
    expect(page.locator(".sd-backdrop")).to_have_count(0)
    modal.locator(".modal-foot .btn").first.click()       # 取消
    expect(modal).to_have_count(0)

    # The negative control: a clean position in another account, same drawer, no button.
    clean = _open_drawer(page, base, "AAPL")
    expect(clean).to_contain_text("✓ 對帳一致")
    expect(clean.locator("button", has_text="補登公司行動")).to_have_count(0)

    unexpected = [u for u in bad if not u.endswith("/api/whatif")]
    assert not unexpected and not page_errors, (
        f"door 2: unexpected 5xx={unexpected!r} page={page_errors!r}"
    )


# ============================================== E23 — the one-click convert-to-SPLIT (D22)


@pytest.mark.e2e
def test_e23_one_click_convert_rewrites_the_form_and_the_converted_row_commits(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """D22's repair, RUN rather than read.

    E23 shipped with a contract test that reads ``web/corp-action-form.js`` as text and
    asserts every field of the API's ``fix`` is named there. That proves the wiring exists;
    it cannot prove the button renders, that clicking it rewrites the form, or that the
    rewritten row previews clean — which is what an e2e is for.

    The ledger is the one E23 exists for: the position sits under the TICKER while the
    statement names a raw broker identifier that no provider will ever price. Entered as a
    merger the row is refused outright (E1a — the identifier holds nothing). One click
    converts it to the 1-for-20 SPLIT §3.4 says it always was, the warning clears, and
    登錄公司行動 turns from disabled to enabled with no acknowledgement asked for.
    """
    base = flow_server(_seed_broker_identifier)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    modal = _open_form_from_door3(page, base)
    _kind_button(modal, 1).click()                         # 整個部位換成另一檔股票 (EXCHANGE)
    modal.locator(".ca-grid select").select_option("tw_broker")
    _symbol_box(modal).fill("TWX00001")
    _date_box(modal).fill("2026-06-20")                    # after 2330's stored price
    _ratio_from(modal).fill("20")
    _ratio_to(modal).fill("1")
    with page.expect_response("**/api/ledgers/corporate-actions/preview") as prev:
        _to_symbol_box(modal).fill("2330")
    assert prev.value.status == 200

    # The merger reading is refused, and the refusal arrives WITH its repair attached.
    expect(modal.locator(".ca-issue-error").first).to_be_visible()
    expect(_save_button(modal)).to_be_disabled()
    fix = modal.locator(".ca-fix")
    expect(fix).to_have_text("改記為分割(SPLIT)")
    expect(modal.locator(".ca-fix-note").first).to_contain_text("2330")

    with page.expect_response("**/api/ledgers/corporate-actions/preview") as prev2:
        fix.click()
    assert prev2.value.status == 200

    # The form itself was rewritten — kind, both symbols, and the ratio carried across
    # untouched (the identifier change and the re-denomination are ONE event).
    expect(_kind_button(modal, 0)).to_have_class("ca-kind active")
    expect(_symbol_box(modal)).to_have_value("2330")
    expect(_to_symbol_box(modal)).to_be_hidden()           # a SPLIT has no destination box
    expect(_ratio_from(modal)).to_have_value("20")
    expect(_ratio_to(modal)).to_have_value("1")
    expect(modal.locator(".ca-issue")).to_have_count(0)    # the warning CLEARED
    expect(_save_button(modal)).to_be_enabled()            # …and no ack was asked for
    after = _cells(_preview_row(modal, 0, 1))
    assert after[:3] == ["行動後", "2330", "50"], after     # 1,000 ÷ 20
    assert after[4] == "500,000", after                    # on the same basis

    with page.expect_response("**/api/ledgers/corporate-actions") as saved:
        _save_button(modal).click()
    assert saved.value.status == 201, saved.value.text()

    row = page.locator("#action-body tr")
    expect(row).to_have_count(1)
    expect(row).to_contain_text("分割")                     # stored as a SPLIT, not a merger
    expect(row).to_contain_text("每 20 股 → 1 股")

    held = _holding(base, "2330")
    assert held is not None
    assert Decimal(held["shares"]) == Decimal("50")
    assert Decimal(held["original_cost_total"]) == Decimal("500000")

    foot = _open_drawer(page, base, "2330")
    expect(foot).to_contain_text("−公司行動 950")
    expect(foot).to_contain_text("部位摘要 50 股")
    expect(foot).to_contain_text("✓ 對帳一致")

    assert not console_errors and not page_errors, (
        f"E23 one-click: console={console_errors!r} page={page_errors!r}"
    )


# ============================================== the CSV round trip (§6.5's header guard)


@pytest.mark.e2e
def test_corporate_action_csv_template_round_trips_through_the_browser(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """§6.5's round-trip header guard, as the owner performs it: download, paste, write.

    The contract suite proves the RENDERED template re-parses through the builder. What it
    cannot see is the browser leg: the served bytes carry a UTF-8 BOM and CRLF line endings,
    they arrive through a blob download, and they are pasted back into a textarea. A header
    that survives ``render_import_template`` but not that trip is a template the owner cannot
    actually use.

    The template's own three example rows also pin WHY two of them are refused: their
    destinations are placeholders, and E10 is keyed on registration (D19), never on the shape
    of a string. Committing the file writes the one clean row and skips the other two.
    """
    base = flow_server(_seed_csv_target)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#csv-kinds .chip", state="attached")
    page.click("#tab-csv")
    page.wait_for_selector("#csv-dropzone", state="visible")
    # By LABEL, not by index: a sixth kind (資金) landed beside this one while this file was
    # being written, and an index would have silently selected the wrong chip.
    page.locator("#csv-kinds .chip", has_text="公司行動").click()
    expect(page.locator("#csv-dz-hint")).to_contain_text("ratio_from")

    with page.expect_download() as dl_info:
        page.click("#csv-template")
    download = dl_info.value
    assert download.suggested_filename == "import_template_corporate_actions.csv"
    path = download.path()
    assert path is not None
    # Read the BYTES, not `read_text`: Python's universal-newline translation would silently
    # rewrite the served CRLF to LF, and CRLF-plus-BOM is exactly what this round trip is
    # about (that is the shape Excel needs, and the shape the paste seam has to survive).
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), raw[:8]         # the Excel BOM
    text = raw.decode("utf-8-sig")
    header = text.split("\r\n")[0]
    assert header == (
        "account,date(YYYY-MM-DD),kind,from_symbol,to_symbol,ratio_to,ratio_from,"
        "cost_carry(選填),note(選填)"
    ), header

    with page.expect_response("**/api/import/preview") as pv:
        page.fill("#csv-paste", text)
    assert pv.value.status == 200
    page.wait_for_selector("#csv-body tr")
    expect(page.locator("#csv-body tr")).to_have_count(3)
    expect(page.locator("#csv-counts")).to_have_text("可寫入 1・警告 0・錯誤 2")
    expect(page.locator("#csv-body tr").nth(1)).to_contain_text("尚未註冊")

    with page.expect_response("**/api/import/commit") as cm:
        page.click("#csv-confirm")
    assert cm.value.status == 200, cm.value.text()
    assert cm.value.json()["written"] == 1

    # The template's clean row is a 10-for-1 SPLIT of 2330 in tw_broker.
    held = _holding(base, "2330")
    assert held is not None
    assert Decimal(held["shares"]) == Decimal("10000")
    assert Decimal(held["original_cost_total"]) == Decimal("500000")

    foot = _open_drawer(page, base, "2330")
    expect(foot).to_contain_text("＋公司行動 9,000")
    expect(foot).to_contain_text("部位摘要 10,000 股")
    expect(foot).to_contain_text("✓ 對帳一致")

    assert not console_errors and not page_errors, (
        f"CSV round trip: console={console_errors!r} page={page_errors!r}"
    )
