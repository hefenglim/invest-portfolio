"""E2E (Playwright, real server + real frontend): the three rulings of 2026-08-15.

* **D44** — a SPLIT whose date is after the owner's target band was set warns once, at entry,
  and offers the restated number as a checkbox that **defaults to OFF**.
* **D47** — an EXCHANGE announces, before saving, that the band will follow the ticker.
* **D48** — a SPINOFF's child is created on save, and the form can seed its first price.

**Why a browser and not just the contract tests.** Every claim below is about rendered state
or about an action taken after a successful save, and neither is visible to a payload
assertion. The load-bearing one is the checkbox's default: "unchecked" IS the ruling — the
system computed what "alert me at 600" becomes after a 10-for-1 and shows it, but declines to
decide whether the owner meant a view about the company or about the share price. A
pre-ticked box is option (a) wearing option (b)'s clothes, and it would pass every contract
test in the repo.

The unticked path is asserted too, and is the stricter of the two: a restate that fires
without being asked for is a silent edit to a number the owner typed.
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
    get_instrument,
    insert_transaction,
    upsert_instrument,
)
from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.pricing.store import upsert_fx, upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import GOLDEN_NOW
from tests.e2e.conftest import FlowServerFactory

BUY_DAY = date(2026, 1, 5)
BAND_DAY = date(2026, 2, 1)     # the band predates the action — D44's own case
ACTION_DAY = date(2026, 3, 16)
PRICE_DAY = date(2026, 6, 9)


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _get_json(base_url: str, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(base_url + path, timeout=10) as r:  # noqa: S310 (loopback)
        data: dict[str, Any] = json.loads(r.read().decode("utf-8"))
        return data


def _band_of(base_url: str, symbol: str) -> tuple[str | None, str | None]:
    """(target_low, target_high) as the API serves them — Decimal strings or null."""
    for row in _get_json(base_url, "/api/instruments")["list"]:
        if row["symbol"] == symbol:
            return row["target_low"], row["target_high"]
    raise AssertionError(f"{symbol} not in /api/instruments")


def _fx(conn: sqlite3.Connection) -> None:
    upsert_fx(conn, [
        FxRow(base=Currency.USD, quote=Currency.TWD, as_of=PRICE_DAY,
              rate=Decimal("33"), source="test"),
        FxRow(base=Currency.MYR, quote=Currency.TWD, as_of=PRICE_DAY,
              rate=Decimal("7"), source="test"),
        FxRow(base=Currency.USD, quote=Currency.MYR, as_of=PRICE_DAY,
              rate=Decimal("4.4"), source="test"),
    ], fetched_at=GOLDEN_NOW)


def _seed_banded_position(conn: sqlite3.Connection) -> None:
    """1,000 shares of 2330, with a target band the owner set BEFORE the action date."""
    seed_accounts(conn)
    upsert_instrument(
        conn,
        Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                   sector="Semiconductors", name="TSMC", board="TWSE",
                   target_low=Decimal("600"), target_high=Decimal("900")),
        today=BAND_DAY,
    )
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=Decimal("1000"), price=Decimal("500"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=BUY_DAY)
    upsert_prices(conn, [PriceRow(instrument="2330", market=Market.TW, as_of=PRICE_DAY,
                                  close=Decimal("600"), source="test")],
                  fetched_at=GOLDEN_NOW)
    _fx(conn)
    conn.commit()


def _open_form_from_door3(page: Page, base: str) -> Locator:
    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#m-account option", state="attached")
    page.click("#tab-laction")
    page.click("#action-add")
    modal = page.locator(".ca-modal")
    expect(modal).to_be_visible()
    return modal


def _kind_button(modal: Locator, index: int) -> Locator:
    """0 = SPLIT · 1 = EXCHANGE · 2 = SPINOFF."""
    return modal.locator(".ca-kind").nth(index)


def _symbol_box(modal: Locator) -> Locator:
    return modal.locator(".ca-grid input").nth(0)


def _date_box(modal: Locator) -> Locator:
    return modal.locator(".ca-grid input").nth(1)


def _ratio_from(modal: Locator) -> Locator:
    return modal.locator(".ca-int").nth(0)


def _ratio_to(modal: Locator) -> Locator:
    return modal.locator(".ca-int").nth(1)


def _to_symbol_box(modal: Locator) -> Locator:
    return modal.locator('.ca-ratio input[placeholder="新代號"]')


def _restate_checkbox(modal: Locator) -> Locator:
    """D44's opt-in. Addressed by its own class, never as "the checkbox in the warning":
    the acknowledgement checkbox lives in a sibling ``.ca-issue`` and an index would silently
    swap the two the next time an issue is added."""
    return modal.locator(".ca-restate input[type=checkbox]")


def _ack_checkbox(modal: Locator) -> Locator:
    return modal.locator(".ca-issue label").filter(
        has_text="我已確認上述警告").locator("input[type=checkbox]")


def _save_button(modal: Locator) -> Locator:
    return modal.locator(".modal-foot .btn-primary")


def _fill_split(modal: Locator, page: Page) -> None:
    """A 10-for-1 on the banded position, previewed."""
    _kind_button(modal, 0).click()
    modal.locator(".ca-grid select").select_option("tw_broker")
    _symbol_box(modal).fill("2330")
    _date_box(modal).fill(ACTION_DAY.isoformat())
    _ratio_from(modal).fill("1")
    with page.expect_response("**/api/ledgers/corporate-actions/preview") as prev:
        _ratio_to(modal).fill("10")
    assert prev.value.status == 200, prev.value.text()


# ============================================================================ D44


@pytest.mark.e2e
def test_the_restate_offer_defaults_to_UNCHECKED_and_leaving_it_alone_changes_nothing(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """★ The ruling, rendered. Default-off means "维持原值" is what happens if the owner
    simply reads the warning and saves — which is the (c) behaviour, deliberately kept as
    the floor. Then the band is asserted UNCHANGED after a real save: a restate that fires
    without being asked for is a silent edit to a number the owner typed, and that is worse
    than the stale band this whole decision is about.
    """
    base = flow_server(_seed_banded_position)
    page = fresh_page
    modal = _open_form_from_door3(page, base)
    _fill_split(modal, page)

    warning = modal.locator(".ca-issue").filter(has_text="目標價設定於")
    expect(warning).to_be_visible()
    expect(warning).to_contain_text("2026-02-01")     # when the stale band was set
    expect(warning).to_contain_text("60")             # 600 ÷ 10, computed for them
    expect(warning).to_contain_text("90")             # 900 ÷ 10
    expect(warning).to_contain_text("系統不會替你決定")

    box = _restate_checkbox(modal)
    expect(box).to_be_visible()
    expect(box).not_to_be_checked()                   # ← the decision, on screen

    _ack_checkbox(modal).check()
    with page.expect_response("**/api/ledgers/corporate-actions") as saved:
        _save_button(modal).click()
    assert saved.value.status == 201, saved.value.text()

    assert _band_of(base, "2330") == ("600", "900")   # untouched, as promised


@pytest.mark.e2e
def test_ticking_the_box_restates_the_band_after_the_action_is_saved(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """The other half: one click and the band is in post-split terms, so ``target_cross``
    compares two numbers in the same denomination again. The write happens AFTER the action
    commits — a band corrected for an event that failed to save would be an alert level
    adjusted for something not in the ledger."""
    base = flow_server(_seed_banded_position)
    page = fresh_page
    modal = _open_form_from_door3(page, base)
    _fill_split(modal, page)

    _restate_checkbox(modal).check()
    _ack_checkbox(modal).check()
    with page.expect_response("**/api/instruments/2330") as put:
        _save_button(modal).click()
    assert put.value.status == 200, put.value.text()

    assert _band_of(base, "2330") == ("60", "90")


# ============================================================================ D47


@pytest.mark.e2e
def test_an_exchange_says_the_band_will_follow_the_ticker_before_saving(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """D47, announced rather than discovered. The values do NOT change — 「其他都不動」 —
    so the sentence has to say the numbers, or "moved" and "restated" are indistinguishable
    to the person reading it."""
    def seed(conn: sqlite3.Connection) -> None:
        _seed_banded_position(conn)
        upsert_instrument(conn, Instrument(symbol="NEWCO", market=Market.TW,
                                           quote_ccy=Currency.TWD, sector="Semiconductors",
                                           name="NewCo", board="TWSE"))
        conn.commit()

    base = flow_server(seed)
    page = fresh_page
    modal = _open_form_from_door3(page, base)
    _kind_button(modal, 1).click()
    modal.locator(".ca-grid select").select_option("tw_broker")
    _symbol_box(modal).fill("2330")
    _date_box(modal).fill(ACTION_DAY.isoformat())
    _ratio_from(modal).fill("1")
    _ratio_to(modal).fill("1")
    with page.expect_response("**/api/ledgers/corporate-actions/preview") as prev:
        _to_symbol_box(modal).fill("NEWCO")
    assert prev.value.status == 200, prev.value.text()

    note = modal.locator(".ca-unblock").filter(has_text="目標價")
    expect(note).to_contain_text("NEWCO")
    expect(note).to_contain_text("600")
    expect(note).to_contain_text("數值不變")

    # NEWCO has no stored price yet, so N3-price warns (soft) — acknowledged, as the owner
    # would. That the band note is NOT an issue box is the point: it reports a consequence
    # of saving, not a problem with the row.
    _ack_checkbox(modal).check()
    with page.expect_response("**/api/ledgers/corporate-actions") as saved:
        _save_button(modal).click()
    assert saved.value.status == 201, saved.value.text()

    assert _band_of(base, "NEWCO") == ("600", "900")
    assert _band_of(base, "2330") == (None, None)     # moved, not copied


# ============================================================================ D48


@pytest.mark.e2e
def test_a_spinoff_creates_its_child_and_takes_its_first_price(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """D48a + D48b in the order the owner meets them: the form says the child will be
    created, the 起始價 box appears only for a SPINOFF, and after saving the child exists,
    is priced, and no longer counts as an unpriced holding.

    The price box earns its place because ``returns.py`` is all-or-nothing on the terminal
    value: one unpriced holding blanks the WHOLE portfolio's XIRR, and a spin-off is
    guaranteed to create one.
    """
    base = flow_server(_seed_banded_position)
    page = fresh_page
    modal = _open_form_from_door3(page, base)

    # SPLIT is the default kind, and the field must NOT be offered there.
    price_field = modal.locator(".field").filter(has_text="子公司起始價")
    expect(price_field).to_be_hidden()

    _kind_button(modal, 2).click()
    expect(price_field).to_be_visible()
    modal.locator(".ca-grid select").select_option("tw_broker")
    _symbol_box(modal).fill("2330")
    _date_box(modal).fill(ACTION_DAY.isoformat())
    _ratio_from(modal).fill("2")
    _ratio_to(modal).fill("1")
    modal.locator(".field").filter(has_text="成本分攤比例").locator("input").fill("0.3")
    price_field.locator("input").fill("123.5")
    with page.expect_response("**/api/ledgers/corporate-actions/preview") as prev:
        _to_symbol_box(modal).fill("SPINCO")
    assert prev.value.status == 200, prev.value.text()

    expect(modal.locator(".ca-issue").filter(has_text="存檔時會自動建立")).to_be_visible()

    _ack_checkbox(modal).check()
    with page.expect_response("**/api/ledgers/corporate-actions") as saved:
        _save_button(modal).click()
    assert saved.value.status == 201, saved.value.text()
    body = saved.value.json()
    assert body["child_priced"] == "SPINCO"
    assert body["unpriced_symbols"] == []          # the warning it answers is withdrawn

    child = [r for r in _get_json(base, "/api/instruments")["list"]
             if r["symbol"] == "SPINCO"]
    assert child, "the child was not registered"
    assert child[0]["ccy"] == "TWD"                # inherited from the parent, not guessed
    assert child[0]["last"] == "123.5"             # the price the owner typed


def test_the_seed_helper_reads_back_what_it_wrote(tmp_path: Any) -> None:
    """A guard on the fixture itself, not on the app: every D44 test above is vacuous if the
    seeded band carries no ``target_set_at``, because the finding would stay silent and the
    checkbox assertions would never run. Cheap, and it fails loudly instead of passing
    emptily."""
    import sqlite3 as _sqlite3

    from portfolio_dash.bootstrap import bootstrap_db
    from portfolio_dash.pricing.schema import create_tables

    conn = _sqlite3.connect(":memory:")
    conn.row_factory = _sqlite3.Row
    bootstrap_db(conn)
    create_tables(conn)
    _seed_banded_position(conn)
    inst = get_instrument(conn, "2330")
    assert inst is not None
    assert (inst.target_low, inst.target_high) == (Decimal("600"), Decimal("900"))
    assert inst.target_set_at == BAND_DAY and BAND_DAY < ACTION_DAY
