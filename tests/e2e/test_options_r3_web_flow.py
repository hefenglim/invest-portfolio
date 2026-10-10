"""E2E options — final review R3, web findings (spec §7.1, §7.2, §8, §6.2).

* **M1** — 「改登錄被指派／履約」 (the inbox) and 「登錄到期作廢」 (the dashboard) land on the
  manual form for the CONTRACT's account. The form used to boot under the remembered account,
  read ``?symbol=`` there, and then set the contract's account with ``.value =`` — no change
  event, so the target account's holdings were never loaded: the side hint read 「此帳戶目前沒有
  這個合約的部位」, a LONG contract was sent as ``assigned`` and refused, and 確認寫入 stayed
  disabled. Every flow here starts with a remembered account that is NOT the contract's.
* **m1** — the stock drawer's 「→ 選擇權區」, followed from another page, lands ON the section
  (it is created after the dashboard fetch, so the browser's own hash scroll found nothing).
* **n1** — a blank fee field sends ``null`` (revert to the rule set's default), never ``''``,
  which the server refused with 400 and failed the whole card's save.
* **item 8** — a ``txn_id`` in the URL is validated before it reaches a CSS selector.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from playwright.sync_api import Page, Request, Route
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.data_ingestion.register import register_option_contract
from portfolio_dash.data_ingestion.store import insert_transaction
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.options import OptionTerms, option_symbol
from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory
from tests.e2e.options_seed import OPEN_PUT, PAST_PUT, seed_option_ledger

_SWEEP_DAY = date(2099, 1, 2)   # the DEF-056 sweep's seeded day — never an expiry here
_PAST_CALL_EXPIRY = date.today() - timedelta(days=3)
assert _PAST_CALL_EXPIRY != _SWEEP_DAY
PAST_CALL = option_symbol(OptionTerms(underlying="AAPL", expiry=_PAST_CALL_EXPIRY,
                                      strike=Decimal("160"), right="C"))
_PAST_PUT_EXPIRY = date.today() - timedelta(days=5)   # options_seed.PAST_PUT

#: The account the form remembers — deliberately NOT the contracts' (schwab).
_REMEMBER_TW = "try { localStorage.setItem('pd_last_account', 'tw_broker'); } catch (e) {}"


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _seed(conn: sqlite3.Connection) -> None:
    """W5's option ledger (``PAST_PUT``: a 1-contract short past its expiry) + a 1-contract
    LONG call that expired three days ago — one inbox item of each direction."""
    seed_option_ledger(conn)
    register_option_contract(conn, PAST_CALL)
    insert_transaction(conn, account_id="schwab", symbol=PAST_CALL, side=Side.BUY,
                       quantity=Decimal("100"), price=Decimal("1.10"), fees=Decimal("0.65"),
                       tax=Decimal("0"), trade_date=date.today() - timedelta(days=12))
    conn.commit()


def _errors(page: Page) -> list[str]:
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    def _console(m: Any) -> None:
        if getattr(m, "type", None) == "error":
            errors.append(getattr(m, "text", ""))

    page.on("console", _console)
    return errors


def _previews(page: Page) -> list[dict[str, Any]]:
    bodies: list[dict[str, Any]] = []

    def _on(req: Request) -> None:
        if req.url.endswith("/api/input/manual/preview"):
            bodies.append(req.post_data_json or {})

    page.on("request", _on)
    return bodies


def _overrides(req: Request) -> dict[str, Any]:
    body = req.post_data_json
    assert isinstance(body, dict)
    out = body["overrides"]
    assert isinstance(out, dict)
    return out


def _close_rows(page: Page, base: str) -> dict[str, dict[str, Any]]:
    rows = page.request.get(base + "/api/ledgers/transactions?limit=500").json()["rows"]
    return {r["symbol"]: r for r in rows if r.get("close_reason")}


def _form_ready(page: Page, hint: str, contracts: str) -> None:
    """The prefilled form shows the CONTRACT account's position, and 確認寫入 is offered."""
    page.wait_for_function(
        "(h) => document.querySelector('#m-opt-side-hint').textContent === h", arg=hint)
    assert page.input_value("#m-account") == "schwab"
    page.wait_for_function(
        "(t) => Array.from(document.querySelectorAll('#m-contracts-hint button'))"
        ".some((b) => b.textContent === t)", arg=f"可平倉 {contracts} 口")
    try:
        page.wait_for_function("() => !document.querySelector('#m-confirm').disabled",
                               timeout=15000)
    except Exception as exc:  # noqa: BLE001 (diagnostic)
        raise AssertionError(page.inner_text("#m-issues") + " || "
                             + page.inner_text("#m-pc-rows")) from exc


@pytest.mark.e2e
def test_the_inbox_deep_link_prefills_the_contracts_account_both_directions(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed)
    page = fresh_page
    page.add_init_script(_REMEMBER_TW)
    errors = _errors(page)
    previews = _previews(page)

    # A LONG call → 履約 (exercised): the stock leg is a purchase at the strike.
    page.goto(base + "/dividend-inbox.html", wait_until="load")
    page.wait_for_selector("#expiry-section:not([hidden]) .xp-item")
    call = page.locator("#expiry-list .xp-item", has_text="買方 1 口")
    with page.expect_navigation():
        call.locator(".xp-assign").click()
    _form_ready(page, "目前持有買方部位 1 口", "1")
    assert page.input_value("#m-symbol") == PAST_CALL
    page.wait_for_function(
        "() => document.querySelector('#m-pc-label').textContent.startsWith('股票腿現金')")
    assert previews and previews[-1].get("close_reason") == "exercised", previews[-1:]
    page.click("#m-confirm")
    page.wait_for_selector(".toast-ok")
    row = _close_rows(page, base)[PAST_CALL]
    assert (row["side"], row["close_reason"], row["date"]) == (
        "sell", "exercised", _PAST_CALL_EXPIRY.isoformat())

    # A SHORT put → 被指派 (assigned).
    previews.clear()
    page.goto(base + "/dividend-inbox.html", wait_until="load")
    page.wait_for_selector("#expiry-section:not([hidden]) .xp-item")
    put = page.locator("#expiry-list .xp-item", has_text="賣方 1 口")
    with page.expect_navigation():
        put.locator(".xp-assign").click()
    _form_ready(page, "目前持有賣方部位 1 口", "1")
    assert page.input_value("#m-symbol") == PAST_PUT
    assert previews and previews[-1].get("close_reason") == "assigned", previews[-1:]
    page.click("#m-confirm")
    page.wait_for_selector(".toast-ok")
    row = _close_rows(page, base)[PAST_PUT]
    assert (row["side"], row["close_reason"]) == ("buy", "assigned")
    assert not errors, errors


@pytest.mark.e2e
def test_the_dashboard_expiry_button_prefills_the_contracts_account(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(seed_option_ledger)
    page = fresh_page
    page.add_init_script(_REMEMBER_TW)
    errors = _errors(page)
    previews = _previews(page)
    page.goto(base + "/index.html", wait_until="load")
    page.wait_for_selector("#opt-open-body .opt-close-btn")
    with page.expect_navigation():
        page.click("#opt-open-body .opt-close-btn")
    page.wait_for_selector("#m-opt-closes button[data-close='expired'].active")
    _form_ready(page, "目前持有賣方部位 1 口", "1")
    assert page.input_value("#m-symbol") == PAST_PUT
    assert page.input_value("#m-date") == _PAST_PUT_EXPIRY.isoformat()
    assert previews and previews[-1].get("account_id") == "schwab"
    assert previews[-1].get("close_reason") == "expired"
    page.click("#m-confirm")
    page.wait_for_selector(".toast-ok")
    row = _close_rows(page, base)[PAST_PUT]
    assert (row["side"], row["close_reason"]) == ("buy", "expired")
    assert not errors, errors


@pytest.mark.e2e
def test_the_drawer_link_lands_on_the_options_section_from_another_page(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(seed_option_ledger)
    page = fresh_page
    errors = _errors(page)
    page.goto(base + "/trades.html", wait_until="load")
    with page.expect_response("**/api/symbol/AAPL/detail"):
        page.evaluate("(s) => window.pdOpenSymbol(s)", "AAPL")
    page.wait_for_selector(".sd-drawer .sd-opt-line a")
    with page.expect_navigation():
        page.click(".sd-drawer .sd-opt-line a")
    page.wait_for_selector("#options-panel")
    aligned = ("() => { const s = document.getElementById('options-panel');"
               " return window.scrollY > 0 && Math.abs(s.getBoundingClientRect().top) < 4; }")
    page.wait_for_function(aligned, timeout=10000)
    page.wait_for_timeout(1500)            # the rest of the dashboard has painted by now
    assert page.evaluate(aligned), page.evaluate(
        "() => [window.scrollY, document.getElementById('options-panel')"
        ".getBoundingClientRect().top]")
    assert not errors, errors


@pytest.mark.e2e
def test_a_blank_fee_field_reverts_instead_of_failing_the_card(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_golden)
    page = fresh_page
    errors = _errors(page)
    page.goto(base + "/settings.html#accounts", wait_until="load")
    page.wait_for_selector("#fee-rules-wrap .fee-field[data-key='option_per_contract']",
                           state="attached")

    # moomoo_us: default 0.8633 (owner ruling 2026-10-10) — set a rate, then blank it: back
    # to the default.
    moomoo = page.locator("#fee-rules-wrap details", has_text="moomoo_us").first
    moomoo.locator("summary").click()
    field = moomoo.locator(".fee-field[data-key='option_per_contract'] input")
    field.fill("0.5")
    with page.expect_response("**/api/fee-rules/moomoo_us") as resp:
        moomoo.locator("[data-save='moomoo_us']").click()
    assert resp.value.status == 200
    moomoo = page.locator("#fee-rules-wrap details", has_text="moomoo_us").first
    field = moomoo.locator(".fee-field[data-key='option_per_contract'] input")
    page.wait_for_function(
        "() => document.querySelector(\"details [data-save='moomoo_us']\").closest('details')"
        ".querySelector(\".fee-field[data-key='option_per_contract'] input\").value === '0.5'")
    field.fill("")
    with page.expect_response("**/api/fee-rules/moomoo_us") as resp:
        moomoo.locator("[data-save='moomoo_us']").click()
    assert resp.value.status == 200, resp.value.text()
    sent = _overrides(resp.value.request)
    assert sent["option_per_contract"] is None
    moomoo = page.locator("#fee-rules-wrap details", has_text="moomoo_us").first
    field = moomoo.locator(".fee-field[data-key='option_per_contract'] input")
    assert field.input_value() == "0.8633"
    assert field.get_attribute("placeholder") == "預設 0.8633"

    # schwab: default 0.65 — a blank field reverts to it; the save never fails the card.
    schwab = page.locator("#fee-rules-wrap details", has_text="schwab").first
    schwab.locator("summary").click()
    schwab.locator(".fee-field[data-key='option_per_contract'] input").fill("")
    schwab.locator(".fee-field[data-key='sec_rate'] input").fill("")
    with page.expect_response("**/api/fee-rules/schwab") as resp:
        schwab.locator("[data-save='schwab']").click()
    assert resp.value.status == 200, resp.value.text()
    sent = _overrides(resp.value.request)
    assert sent["option_per_contract"] is None and sent["sec_rate"] is None
    schwab = page.locator("#fee-rules-wrap details", has_text="schwab").first
    assert schwab.locator(
        ".fee-field[data-key='option_per_contract'] input").input_value() == "0.65"
    assert page.locator(".toast-fail").count() == 0
    assert not errors, errors


@pytest.mark.e2e
def test_a_close_row_clears_the_fee_rule_line(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """A close row carries no fee: the server answers ``fee_rule_label: null`` (fixer-2 round)
    and the form clears the line instead of keeping the opening trade's 「每口 0.65…」. The
    preview is passed through with the label nulled on a close body, so the page's half of
    the contract is pinned whichever server build answers."""
    base = flow_server(seed_option_ledger)
    page = fresh_page
    errors = _errors(page)

    def _null_close_label(route: Route) -> None:
        resp = route.fetch()
        body = resp.json()
        sent = route.request.post_data_json or {}
        if sent.get("close_reason"):
            body["fee_rule_label"] = None
        route.fulfill(response=resp, json=body)

    page.route("**/api/input/manual/preview", _null_close_label)
    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#m-account option[value='schwab']", state="attached")
    page.select_option("#m-account", "schwab")
    page.fill("#m-symbol", OPEN_PUT)
    page.wait_for_selector("#m-opt-side-field:visible")
    page.click("#m-opt-sides button[data-intent='STO']")
    page.fill("#m-contracts", "1")
    page.fill("#m-price", "2.00")
    page.wait_for_function(
        "() => document.querySelector('#m-fee-rule').textContent.includes('每口')")
    with page.expect_response(
            lambda r: r.url.endswith("/api/input/manual/preview")
            and bool((r.request.post_data_json or {}).get("close_reason"))):
        page.click("#m-opt-closes button[data-close='expired']")
    page.wait_for_function("() => document.querySelector('#m-fee-rule').textContent === ''")
    assert not errors, errors


_ORPHAN = "AAPL261218P00150000"


def _seed_orphan(conn: sqlite3.Connection) -> None:
    """The golden (stock-only) ledger + a registered contract with no ledger row."""
    _seed_golden(conn)
    register_option_contract(conn, _ORPHAN)
    conn.commit()


@pytest.mark.e2e
def test_a_registered_contract_with_no_rows_opens_the_contract_drawer(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """R3 n3 — reached through the watchlist's toggle, it got the STOCK drawer."""
    base = flow_server(_seed_orphan)
    page = fresh_page
    errors = _errors(page)
    page.goto(base + "/instruments.html", wait_until="load")
    page.wait_for_selector("#inst-body tr")
    page.click("#toggle-options")
    row = page.locator("#inst-body tr", has_text=_ORPHAN)
    row.wait_for()
    with page.expect_response(f"**/api/symbol/{_ORPHAN}/detail"):
        row.locator(".sym-cell").click()
    page.wait_for_selector(".sd-drawer .sd-contract")
    body = page.inner_text(".sd-drawer")
    assert "選擇權不提供試算" in body and "此合約在帳本中沒有任何交易紀錄" in body
    assert "價格與成本" not in body and "配息史" not in body
    assert page.inner_text(".sd-head .sym-code") == "AAPL 2026-12-18 150 Put"
    assert not errors, errors


@pytest.mark.e2e
def test_a_malformed_txn_id_in_the_url_is_not_a_selector(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(seed_option_ledger)
    page = fresh_page
    errors = _errors(page)
    page.goto(base + '/trades.html?ledger=tx&txn_id=1%22%5D%2C*%5Bx%3D%22', wait_until="load")
    page.wait_for_function(
        "() => Array.from(document.querySelectorAll('.toast .msg'))"
        ".some((n) => n.textContent === '找不到該筆交易列')")
    assert page.locator("#tx-body tr.ledger-added-row").count() == 0
    assert not errors, errors
