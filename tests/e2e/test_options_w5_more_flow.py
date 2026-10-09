"""E2E options W5 — the remaining option surfaces on a seeded option ledger (spec §4.2, §4.3,
§6.2, §8): the assignment's two-leg card, the unset-fee refusal's link, the 交易 tab's delete
warning and the refused-close dialog, the edit dialog in 口數, the 選擇權合約調整 form, the
fee settings' two option fields, the watchlist toggle and the cash statement's wording.

Every page here runs against the REAL ``/api/option-expiry/*`` routes (wired in W7b; the
sidebar badge reads their count on every page).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest
from playwright.sync_api import Page
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory
from tests.e2e.options_seed import OPEN_PUT, seed_option_ledger


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _errors(page: Page) -> list[str]:
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    def _console(m: Any) -> None:
        if getattr(m, "type", None) == "error":
            errors.append(getattr(m, "text", ""))

    page.on("console", _console)
    return errors


def _manual_on(page: Page, base: str, account: str, symbol: str) -> None:
    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector(f"#m-account option[value='{account}']", state="attached")
    page.select_option("#m-account", account)
    page.fill("#m-symbol", symbol)
    page.wait_for_selector("#m-opt-side-field:visible")


@pytest.mark.e2e
def test_an_assignment_previews_and_writes_both_legs(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(seed_option_ledger)
    page = fresh_page
    errors = _errors(page)
    _manual_on(page, base, "schwab", OPEN_PUT)
    page.wait_for_selector("#m-opt-sides button[data-intent='BTC'].active")   # held short
    page.click("#m-opt-closes button[data-close='assign']")
    page.wait_for_selector("#m-stock-price-field:visible")
    page.wait_for_function("() => document.querySelector('#m-contracts').value === '2'")
    page.wait_for_selector("#m-pc-rows .pc-leg[data-role='stock']")
    legs = page.inner_text("#m-pc-rows")
    assert "選擇權腿" in legs and "股票腿" in legs
    assert "被指派：以 110.00 買入 200 股 AAPL" in legs
    assert page.inner_text("#m-pc-label") == "股票腿現金（含費稅）"
    page.wait_for_function("() => document.querySelector('#m-stock-price').value === '110'")
    page.wait_for_function("() => !document.querySelector('#m-confirm').disabled")
    page.click("#m-confirm")
    page.wait_for_selector(".toast-ok")
    assert "、#" in page.inner_text(".toast-ok")          # two rows written: 「（#a、#b）」
    page.wait_for_selector("#tx-body .tx-opt-side:text('被指派')")
    assert not errors, errors


@pytest.mark.e2e
def test_an_unset_option_fee_names_both_repairs(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """moomoo_us has no per-contract rate (OPT-D6): the fee must be supplied — the refusal
    links to the settings panel where the rate is set."""
    base = flow_server(seed_option_ledger)
    page = fresh_page
    errors = _errors(page)
    _manual_on(page, base, "moomoo_my", OPEN_PUT)
    page.click("#m-opt-sides button[data-intent='STO']")
    page.fill("#m-contracts", "1")
    page.fill("#m-price", "2.00")
    page.wait_for_selector("#m-issues .opt-fee-link")
    assert page.get_attribute("#m-issues .opt-fee-link", "href") == "settings.html#accounts"
    assert page.is_disabled("#m-confirm")
    assert not errors, errors


@pytest.mark.e2e
def test_the_ledger_delete_names_the_close_rows_and_lists_a_refusal(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(seed_option_ledger)
    page = fresh_page
    errors = _errors(page)
    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#tx-body .tx-opt-side")
    # the bought call's opening trade — a later 到期作廢 row closes it
    row = page.locator("#tx-body tr.expandable", has_text="140 Call").filter(
        has_text="買進開倉")
    assert row.count() == 1
    assert "1 口" in row.inner_text() and "100 股當量" in row.inner_text()
    row.locator(".btn-row-del").click()
    page.wait_for_selector(".modal-backdrop .modal-body")
    assert "到期作廢" in page.inner_text(".modal-backdrop .modal-body")
    with page.expect_response("**/api/ledgers/transactions/*") as resp:
        page.click(".modal-backdrop .modal-foot .btn-danger")
    assert resp.value.status == 422
    page.wait_for_selector("#refused-close-dialog .opt-refused-item")
    item = page.inner_text("#refused-close-dialog .opt-refused-item")
    assert "140 Call" in item and "到期作廢" in item and "前往該筆交易" in item
    page.click("#refused-close-dialog .modal-foot .btn-primary")
    # nothing was deleted
    assert page.locator("#tx-body tr.expandable", has_text="140 Call").count() == 2
    # The refusal IS the expected answer: Chromium logs every 4xx resource load, so the one
    # 422 this flow provokes is the only console line allowed.
    unexpected = [e for e in errors if "status of 422" not in e]
    assert not unexpected, unexpected


@pytest.mark.e2e
def test_the_edit_dialog_edits_an_option_row_in_contracts(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(seed_option_ledger)
    page = fresh_page
    errors = _errors(page)
    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#tx-body .tx-opt-side")
    row = page.locator("#tx-body tr.expandable", has_text="130 Call")
    row.locator(".wl-actions .btn").first.click()
    page.wait_for_selector("#edit-contracts")
    assert page.input_value("#edit-contracts") == "1"
    assert "此列：買進開倉" in page.inner_text(".modal-backdrop .opt-edit-side")
    page.fill("#edit-contracts", "2")
    page.wait_for_function(
        "() => !document.querySelector('.modal-backdrop .modal-foot .btn-primary').disabled")
    with page.expect_response("**/api/ledgers/transactions/*") as resp:
        page.click(".modal-backdrop .modal-foot .btn-primary")
    assert resp.value.status == 200, resp.value.text()
    page.wait_for_function(
        "() => Array.from(document.querySelectorAll('#tx-body tr.expandable'))"
        ".some((r) => r.textContent.includes('130 Call') && r.textContent.includes('2 口'))")
    assert not errors, errors


@pytest.mark.e2e
def test_the_option_adjust_form_composes_the_destination_and_prints_the_products(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(seed_option_ledger)
    page = fresh_page
    errors = _errors(page)
    page.goto(base + "/trades.html", wait_until="load")
    page.click("#tab-laction")
    page.click("#action-add")
    page.wait_for_selector(".ca-modal .ca-kind")
    page.locator(".ca-modal .ca-kind", has_text="選擇權合約被調整").click()
    page.wait_for_selector(".ca-modal .ca-adj-strike:visible")
    # R3 m3 (spec §6.2: 來源 = a held contract): the source is PICKED from the account's held
    # contracts, named by the server (「…110 Put・賣方 2 口」) — never a free-text box.
    assert page.locator(".ca-modal .ca-sym-field").is_hidden()
    page.wait_for_selector(".ca-modal .ca-acct-select option[value='schwab']", state="attached")
    page.select_option(".ca-modal .ca-acct-select", "schwab")
    src = f".ca-modal .ca-adj-src option[value='{OPEN_PUT}']"
    page.wait_for_selector(src, state="attached")
    assert page.inner_text(src).endswith("110 Put・賣方 2 口")
    labels = page.eval_on_selector_all(".ca-modal .ca-adj-src option",
                                       "ns => ns.map((n) => n.textContent)")
    assert all("・" in t for t in labels if t and not t.startswith("請選擇")), labels
    page.select_option(".ca-modal .ca-adj-src", OPEN_PUT)
    page.wait_for_function(
        "() => document.querySelector('.ca-modal .ca-adj-und').value === 'AAPL'")
    assert page.locator(".ca-modal .ca-int").first.input_value() == "1"   # 來源 fixed at 1 口
    page.locator(".ca-modal .ca-int").nth(1).fill("2")
    page.fill(".ca-modal .ca-adj-strike", "55")
    page.wait_for_function(
        "() => document.querySelector('.ca-modal .ca-adj-name').textContent.includes('55 Put')")
    page.wait_for_selector(".ca-modal .ca-strike")
    assert page.inner_text(".ca-modal .ca-strike") == "履約價×口數：110×1 ＝ 55×2（110 ＝ 110） ✓"
    assert "口 →" in page.inner_text(".ca-modal .ca-ratio")
    assert not errors, errors


@pytest.mark.e2e
def test_the_fee_settings_show_the_two_option_fields(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_golden)
    page = fresh_page
    errors = _errors(page)
    page.goto(base + "/settings.html#accounts", wait_until="load")
    page.wait_for_selector("#fee-rules-wrap .fee-field[data-key='option_per_contract']",
                           state="attached")
    schwab = page.locator("#fee-rules-wrap details", has_text="schwab").first
    schwab.locator("summary").click()
    field = schwab.locator(".fee-field[data-key='option_per_contract'] input")
    assert field.input_value() == "0.65"
    assert field.get_attribute("max") is None                 # a per-contract fee, not a ratio
    moomoo = page.locator("#fee-rules-wrap details", has_text="moomoo_us").first
    moomoo.locator("summary").click()
    unset = moomoo.locator(".fee-field[data-key='option_per_contract'] input")
    assert unset.input_value() == "" and unset.get_attribute("placeholder") == "未設定"
    assert moomoo.locator(".fee-field[data-key='option_reg_per_contract']").count() == 1
    tw = page.locator("#fee-rules-wrap details", has_text="台灣券商規則").first
    assert tw.locator(".fee-field[data-key='option_per_contract']").count() == 0
    assert not errors, errors


@pytest.mark.e2e
def test_the_watchlist_hides_contracts_until_asked(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(seed_option_ledger)
    page = fresh_page
    errors = _errors(page)
    page.goto(base + "/instruments.html", wait_until="load")
    page.wait_for_selector("#inst-body tr")
    assert page.locator("#inst-body tr", has_text=OPEN_PUT).count() == 0
    toggle = page.locator("#toggle-options")
    toggle.wait_for(state="visible")
    assert toggle.inner_text() == "顯示選擇權合約（5）"
    toggle.click()
    page.wait_for_selector(f"#inst-body tr:has-text('{OPEN_PUT}')")
    # R3 m2: a contract is never quoted by design (R2) — no red 缺價 badge, a neutral label
    # with its own class, and no 編輯 (target-price alerts read stocks only).
    row = page.locator("#inst-body tr", has_text=OPEN_PUT)
    assert row.locator(".badge-missing").count() == 0
    label = row.locator(".inst-opt-unquoted")
    assert label.count() == 1 and label.inner_text() == "以最後成交價估值"
    assert row.locator(".wl-actions .btn", has_text="編輯").count() == 0
    # a stock row keeps its 編輯
    aapl = page.locator("#inst-body tr").filter(
        has=page.locator(".sym-code", has_text=re.compile(r"^AAPL$")))
    assert aapl.count() == 1
    assert aapl.locator(".wl-actions .btn", has_text="編輯").count() == 1
    assert not errors, errors


@pytest.mark.e2e
def test_the_cash_statement_says_what_an_option_line_did(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(seed_option_ledger)
    page = fresh_page
    errors = _errors(page)
    page.goto(base + "/cash.html", wait_until="load")
    page.wait_for_selector("#cash-cards .cash-line")
    card = page.locator("#cash-cards .cash-card", has_text="嘉信").first
    card.locator(".cash-line", has_text="USD").first.click()
    page.wait_for_function(
        "() => document.querySelector('#cash-stmt-body').textContent.includes('賣出開倉 2 口')")
    body = page.inner_text("#cash-stmt-body")
    assert "賣出開倉 2 口 AAPL" in body and "買進開倉 1 口" in body
    assert "到期作廢 1 口" in body                    # a close row: no price, no fee
    assert not errors, errors
