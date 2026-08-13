"""E2E (Playwright, real server + real frontend): the cash-movement KINDS, on screen.

``portfolio_dash/shared/cash_kinds.py`` is the authoritative table, and three kinds joined it
with the broker-statement importer (2026-08-13): ``INTEREST``, ``INTEREST_EXPENSE`` and
``BROKER_FEE``. Every one of them had backend coverage (``tests/data_ingestion/
test_cash_import.py``, ``tests/contract/test_cash_import_api.py``) and NOTHING that drove the
page — so the four things that can only go wrong in the browser had no guard at all:

1. **the door** — a kind the ledger stores but the form cannot select is a kind the owner can
   only reach through a CSV file. ``web/cash.js``'s ``KIND_BUTTONS`` table binds each
   segmented button to its stored kind; a half-registered kind leaves a button that switches
   nothing, or lights up nothing.
2. **the direction** — both direction predicates in the frontend used to read
   ``kind === 'withdraw'``, which paints a broker fee with the *inbound* chip and adds it to
   the pool. That is the same silent mis-signing ``cash_kinds.py``'s module docstring exists
   to prevent, one layer up, and it is visible ONLY on screen.
3. **the acquisition-cost field** — ``acq_home_amount`` belongs to a *funding* flow
   (``ACQUIRING_KINDS``). Offering it on interest invites a cost basis the FX pool will
   ignore; withholding it from a foreign deposit loses the basis outright (spec F1).
4. **the edit dialog** — an imported row whose kind the dialog does not offer is rewritten to
   the dialog's FIRST option by a user who only opened it to fix a note. A money-of-record
   mutation with no user intent behind it.

Every test drives the REAL stack (fresh uvicorn subprocess + on-disk SQLite + the served
``web/``) and asserts ZERO console / page errors, matching this directory's convention.
Browser contexts come from the shared ``fresh_page`` fixture, which installs issue #67's
third-party stub (``tests/e2e/test_thirdparty_isolation.py`` fails the build otherwise).
"""

import json
import sqlite3
import urllib.request
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from playwright.sync_api import FilePayload, Page, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import insert_cash_movement
from portfolio_dash.pricing.results import FxRow
from portfolio_dash.pricing.store import upsert_fx
from portfolio_dash.shared.enums import Currency
from tests.conftest import GOLDEN_NOW
from tests.e2e.conftest import FlowServerFactory

#: The TWD pool every debit/credit in this file moves. Round, and far from zero, so no
#: assertion here can be satisfied by an overdraft guard firing instead of the arithmetic.
TW_OPENING = Decimal("1000000")


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


def _movements(base_url: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = _get_json(base_url, "/api/cash")["movements"]["rows"]
    return rows


def _sink(page: Page) -> tuple[list[str], list[str]]:
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(getattr(m, "text", ""))
            if getattr(m, "type", None) == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    return console_errors, page_errors


def _fx(conn: sqlite3.Connection) -> None:
    """Rates for the 合併現金 total + the 取得成本 reference prefill."""
    upsert_fx(conn, [
        FxRow(base=Currency.USD, quote=Currency.TWD, as_of=date(2026, 6, 9),
              rate=Decimal("33"), source="test"),
        FxRow(base=Currency.MYR, quote=Currency.TWD, as_of=date(2026, 6, 9),
              rate=Decimal("7"), source="test"),
        FxRow(base=Currency.USD, quote=Currency.MYR, as_of=date(2026, 6, 9),
              rate=Decimal("4.4"), source="test"),
    ], fetched_at=GOLDEN_NOW)


def _seed_funded(conn: sqlite3.Connection) -> None:
    """Accounts with POSITIVE cash pools and no trades.

    Positive matters twice: a debit kind must be able to reduce a pool without the reduction
    being confused for an overdraft guard, and the movement PUT refuses an edit that would
    leave any touched pool negative — so on the golden ledger (whose tw_broker pool is
    −500,000 from an unfunded buy) the edit-dialog test could never reach its own subject.
    """
    seed_accounts(conn)
    insert_cash_movement(conn, account_id="tw_broker", move_date=date(2026, 1, 2),
                         kind="DEPOSIT", ccy=Currency.TWD, amount=TW_OPENING,
                         note="期初資金")
    insert_cash_movement(conn, account_id="schwab", move_date=date(2026, 1, 2),
                         kind="DEPOSIT", ccy=Currency.USD, amount=Decimal("50000"),
                         acq_home_amount=Decimal("1600000"), note="opening USD")
    _fx(conn)
    conn.commit()


def _seed_funded_with_rebate(conn: sqlite3.Connection) -> None:
    """…plus a FOREIGN 折讓款 on schwab — the third acquiring kind, which the manual form
    has no button for, so the edit dialog is the only surface that can be asserted on."""
    _seed_funded(conn)
    insert_cash_movement(conn, account_id="schwab", move_date=date(2026, 2, 3),
                         kind="REBATE", ccy=Currency.USD, amount=Decimal("120"),
                         acq_home_amount=Decimal("3960"), note="手續費折讓")
    conn.commit()


def _open_flows(page: Page, base: str, account: str) -> None:
    """Open 資金管理 › 出金入金 with *account* selected (the form is hidden on #pools)."""
    page.goto(base + "/cash.html#flows", wait_until="load")
    page.wait_for_selector("#cm-account option", state="attached")
    page.select_option("#cm-account", account)
    page.wait_for_selector("#cm-confirm", state="visible")


def _submit_movement(page: Page, button: str, amount: str, note: str) -> None:
    """Pick a kind via its segmented button, type an amount, write it, wait for the reload.

    Asserts the button actually LIT UP: ``setKind`` both stores the kind and toggles
    ``.active`` from one table, so a button bound for one and not the other is a real (and
    otherwise invisible) half-registration.
    """
    page.click(button)
    expect(page.locator(button)).to_have_class("active")
    page.fill("#cm-amount", amount)
    page.fill("#cm-note", note)
    page.click("#cm-confirm")
    # Wait on the LEDGER, not on the cleared amount: cash.js clears the form *before*
    # awaiting boot(), so the input is empty while the table is still the previous render.
    expect(page.locator("#cm-body tr", has_text=note)).to_have_count(1)


# ----------------------------------------------------------------------------- the door

@pytest.mark.e2e
def test_new_cash_kinds_are_selectable_and_land_in_the_ledger(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """利息 / 融資利息 / 券商費用 can each be picked and written, and the ledger names them.

    Breaks if: a segmented button is dropped from ``web/cash.html``; its ``KIND_BUTTONS``
    entry is missing (the click stores nothing / lights nothing); the stored spelling drifts
    from ``CashKind``; or the write path rejects the kind.
    """
    base = flow_server(_seed_funded)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    _open_flows(page, base, "tw_broker")
    page.select_option("#cm-ccy", "TWD")
    page.fill("#cm-date", "2026-06-30")

    _submit_movement(page, "#cm-kind-int", "10", "月結利息")
    _submit_movement(page, "#cm-kind-intexp", "20", "融資利息")
    _submit_movement(page, "#cm-kind-fee", "30", "帳管費")

    # On screen, in the 入金／出金紀錄 table — each under its OWN label. Compared as a whole
    # SET of exact strings: 「利息」 is a substring of 「融資利息」, so a substring match would
    # pass even if margin interest were mislabelled as plain interest — which is precisely
    # the collapse ``CashKind``'s docstring refuses ("would display 融資利息 as 「券商費用」").
    chips = page.locator("#cm-body .dir-chip").all_inner_texts()
    assert sorted(chips) == sorted(
        ["利息", "融資利息", "券商費用", "入金", "入金"])  # + the two seeded openings

    # …and in the ledger, under the canonical stored spellings.
    stored = {m["note"]: (m["kind"], m["amount"]) for m in _movements(base)}
    assert stored["月結利息"] == ("interest", "10")
    assert stored["融資利息"] == ("interest_expense", "20")
    assert stored["帳管費"] == ("broker_fee", "30")

    assert console_errors == [] and page_errors == []


# ------------------------------------------------------------------------ the direction

@pytest.mark.e2e
def test_a_fee_is_rendered_and_counted_as_a_debit_not_a_credit(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """券商費用 / 融資利息 must read as OUTBOUND everywhere the page shows a direction.

    Three independent surfaces, because the defect this guards (both predicates written as
    ``kind === 'withdraw'``) hits them one at a time:

    * the 紀錄 table's direction chip — ``dir-sell`` (outbound), never ``dir-buy``;
    * the 現金收支明細 delta — a negative amount carrying ``sign-down``;
    * the 賬戶現金 card — the balance actually FELL.

    Breaks if ``DEBIT_KINDS`` loses a kind: the chip flips, the delta prints ``+30`` and the
    card reads 1,000,060 instead of 999,980.
    """
    base = flow_server(_seed_funded)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    _open_flows(page, base, "tw_broker")
    page.select_option("#cm-ccy", "TWD")
    page.fill("#cm-date", "2026-06-30")
    _submit_movement(page, "#cm-kind-int", "10", "月結利息")
    _submit_movement(page, "#cm-kind-fee", "30", "帳管費")

    # (1) the direction chip in 入金／出金紀錄.
    fee_chip = page.locator("#cm-body tr", has_text="帳管費").locator(".dir-chip")
    expect(fee_chip).to_have_class("dir-chip dir-sell")
    interest_chip = page.locator("#cm-body tr", has_text="月結利息").locator(".dir-chip")
    expect(interest_chip).to_have_class("dir-chip dir-buy")

    # (2) the running statement: the fee is a NEGATIVE delta, the interest a positive one.
    page.click(".cash-tab[data-tab='pools']")
    card = page.locator(".cash-card", has_text="TW Broker")
    card.locator(".cash-line", has_text="TWD").click()
    page.wait_for_selector("#cash-stmt-body tr")
    fee_delta = page.locator("#cash-stmt-body tr", has_text="帳管費").locator("td").nth(3)
    expect(fee_delta).to_have_text("−30")          # U+2212, format.js's MINUS
    expect(fee_delta).to_have_class("num sign-down")
    int_delta = page.locator("#cash-stmt-body tr", has_text="月結利息").locator("td").nth(3)
    expect(int_delta).to_have_text("+10")

    # (3) the pool itself: 1,000,000 + 10 − 30. A credited fee would read 1,000,040.
    expect(card.locator(".cash-line", has_text="TWD").locator(".amt")).to_have_text("999,980")

    assert console_errors == [] and page_errors == []


# ------------------------------------------------------------- the acquisition-cost field

@pytest.mark.e2e
def test_acquisition_cost_is_offered_only_to_an_acquiring_kind(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """取得成本 belongs to a FUNDING flow, and to no other kind.

    Run on schwab/USD, whose currency differs from the account's 資金幣別 (TWD) — the only
    situation in which the field can appear at all. Breaks if ``ACQUIRING_KINDS`` gains a
    non-funding kind (a cost basis the FX pool will never read) or loses a funding one (spec
    F1's basis silently unrecordable through the form).
    """
    base = flow_server(_seed_funded)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    _open_flows(page, base, "schwab")
    page.wait_for_function(
        "() => Array.from(document.querySelectorAll('#cm-ccy option'))"
        ".some((o) => o.value === 'USD')")
    page.select_option("#cm-ccy", "USD")
    page.fill("#cm-date", "2026-06-09")
    page.dispatch_event("#cm-date", "change")

    field = page.locator("#cm-acq-field")
    # to_be_hidden() is also satisfied by a MISSING element, so pin its existence first —
    # otherwise a renamed id would turn the whole negative half of this test into a no-op.
    expect(field).to_have_count(1)
    for button in ("#cm-kind-in", "#cm-kind-open"):     # 入金 / 期初資金 — acquisitions
        page.click(button)
        expect(field).to_be_visible()
    for button in ("#cm-kind-out", "#cm-kind-int", "#cm-kind-intexp", "#cm-kind-fee"):
        page.click(button)
        expect(field).to_be_hidden()

    # A blanked field must not smuggle a stale cost through: syncAcqField clears the input.
    page.click("#cm-kind-in")
    expect(field).to_be_visible()
    page.fill("#cm-acq", "31.5")
    page.click("#cm-kind-fee")
    assert page.input_value("#cm-acq") == ""

    assert console_errors == [] and page_errors == []


@pytest.mark.e2e
def test_edit_dialog_offers_the_cost_field_on_a_foreign_rebate(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """REBATE is the third acquiring kind and the form has NO button for it — the edit
    dialog is its only manual surface, so that is where its 取得成本 field is asserted.

    The dialog also LOCKS a 折讓款's kind/date (the rebate inbox's suppression key). Breaks if
    ``ACQUIRING_KINDS`` drops ``rebate`` (an unrelated amount edit then NULLs a recorded cost
    basis — data loss, not a display bug) or if the lock is lifted.
    """
    base = flow_server(_seed_funded_with_rebate)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    _open_flows(page, base, "schwab")
    row = page.locator("#cm-body tr", has_text="折讓款")
    expect(row).to_have_count(1)
    row.locator("button", has_text="編輯").click()
    modal = page.locator(".modal-backdrop .modal")
    expect(modal).to_be_visible()

    # The acquisition cost is present and pre-filled with the STORED HOME AMOUNT (F1).
    acq = modal.locator(".field", has_text="取得成本").locator("input")
    expect(acq).to_have_value("3960")
    # …and the rebate's kind/date are locked rather than editable.
    expect(modal.locator(".field", has_text="方向").locator("input")).to_be_disabled()

    assert console_errors == [] and page_errors == []


# -------------------------------------------------------------------- the edit dialog

@pytest.mark.e2e
def test_editing_an_imported_movement_does_not_rewrite_its_kind(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """Import a 券商費用 through the CSV door, edit only its NOTE, and it stays a 券商費用.

    The whole chain in one browser: the 資金 CSV kind chip → upload → preview → commit →
    the 資金管理 ledger → the edit dialog → the PUT.

    Breaks if ``EDITABLE_KINDS`` does not offer the imported row's kind: no ``<option>``
    matches, the ``<select>`` falls back to its first entry (入金), and saving a note turns a
    debit into a credit — the ledger moves by 2 × the amount and nobody typed a number.
    """
    base = flow_server(_seed_funded)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    # --- the CSV door -------------------------------------------------------------------
    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#csv-kinds .chip", state="attached")
    page.click("#tab-csv")
    page.wait_for_selector("#csv-dropzone", state="visible")
    page.locator("#csv-kinds .chip", has_text="資金").click()

    upload = (
        "account,date,kind,ccy,amount,acq_home_amount,note\r\n"
        "tw_broker,2026-06-30,BROKER_FEE,TWD,30,,帳管費\r\n"
    ).encode()   # non-ASCII note -> a str the browser reads back as UTF-8, not a b"" literal
    with page.expect_response("**/api/import/preview") as pv:
        page.set_input_files("#csv-file-input", files=[
            FilePayload(name="import_cash.csv", mimeType="text/csv", buffer=upload)])
    assert pv.value.status == 200
    page.wait_for_function(
        "() => { const b = document.querySelector('#csv-confirm'); return b && !b.disabled; }")
    with page.expect_response("**/api/import/commit") as cm:
        page.click("#csv-confirm")
    assert cm.value.status == 200 and cm.value.json()["written"] == 1

    imported = next(m for m in _movements(base) if m["note"] == "帳管費")
    assert imported["kind"] == "broker_fee"

    # --- the edit dialog ----------------------------------------------------------------
    _open_flows(page, base, "tw_broker")
    row = page.locator("#cm-body tr", has_text="券商費用")
    expect(row).to_have_count(1)
    row.locator("button", has_text="編輯").click()
    modal = page.locator(".modal-backdrop .modal")
    expect(modal).to_be_visible()

    # The dialog must OPEN on the row's own kind — not on whatever option happens to be first.
    kind_select = modal.locator(".field", has_text="方向").locator("select")
    expect(kind_select).to_have_value("broker_fee")

    modal.locator(".field", has_text="備註").locator("input").fill("帳管費（更正備註）")
    modal.locator("button", has_text="儲存").click()
    page.wait_for_selector(".toast-ok")

    after = next(m for m in _movements(base) if m["id"] == imported["id"])
    assert after["note"] == "帳管費（更正備註）"       # the edit the user asked for
    assert after["kind"] == "broker_fee"                # …and nothing else moved
    assert after["amount"] == imported["amount"]

    # The pool still shows the fee as a debit after the round trip.
    page.click(".cash-tab[data-tab='pools']")
    card = page.locator(".cash-card", has_text="TW Broker")
    expect(card.locator(".cash-line", has_text="TWD").locator(".amt")).to_have_text("999,970")

    assert console_errors == [] and page_errors == []
