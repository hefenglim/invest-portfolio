"""E2E M6-04: the 觀察清單 table has SIX ways to be empty, and only some of them may say so.

``web/instruments.js::render()`` cleared ``<tbody>`` and re-filled it with whatever survived
the archived filter and the search box — with no branch for "nothing survived". Typing a
symbol that does not exist left the header row over 0px of nothing (measured: ``rows:0,
tbody_h:0, wrap_h:37.5, any_empty_state:false``), which is indistinguishable from a broken
page. But the same function reaches "no rows" from six different states, and a single
「尚無標的」 would be a lie in two of them:

  1. before the first fetch resolves (``render()`` runs at script start)     -> unknown
  2. the fetch failed (``D = {list: []}`` + a fail toast)                      -> unknown
  3. the list is genuinely empty (fresh install)                              -> 尚無標的
  4. every row is archived and the toggle is off                              -> hidden rows
  5. the search matches nothing                                               -> no match
  6. the search matches ONLY hidden archived rows                             -> hidden rows

Owner ruling: TWO texts — 「無資料」 vs 「篩選無結果（含封存提示）」. 1 and 2 draw nothing
(the list is not empty, it is not known yet — and the fail toast already says why); 3 says
尚無標的; 4/5/6 say 無符合的標的, and whenever the hidden 已移除／封存 rows hold the hits the
note says how many and names the toggle that reveals them.

Two servers: one seeded with NO instruments (cases 1-3, the fetch held then failed through a
Playwright route) and one with two live rows + one archived (cases 5, 6, then 4 by archiving
the live rows through the real PUT …/archive door and reloading).
"""

import json
import sqlite3
import urllib.request
from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, Route, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import set_instrument_archived, upsert_instrument
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from tests.e2e.conftest import FlowServerFactory

_NO_DATA = "尚無標的"
_TOGGLE = "顯示已移除／封存"


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _seed_none(conn: sqlite3.Connection) -> None:
    """Accounts only — a fresh install has registered nothing yet."""
    seed_accounts(conn)
    conn.commit()


def _seed_mixed(conn: sqlite3.Connection) -> None:
    """Two live watch rows + one archived row whose code matches nothing else."""
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Semi", name="TSMC", board="TWSE"))
    upsert_instrument(conn, Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
                                       sector="Tech", name="Apple"))
    upsert_instrument(conn, Instrument(symbol="ARCHD", market=Market.US, quote_ccy=Currency.USD,
                                       sector="Tech", name="Archived Co"))
    assert set_instrument_archived(conn, "ARCHD", True)
    conn.commit()


def _note(page: Page) -> str | None:
    node = page.query_selector("#inst-empty")
    return node.inner_text().strip() if node else None


def _archive(base: str, symbol: str) -> None:
    req = urllib.request.Request(
        base + f"/api/instruments/{symbol}/archive",
        data=json.dumps({"archived": True}).encode("utf-8"), method="PUT",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310 (loopback)
        assert r.status == 200, r.status


def _watch(page: Page) -> tuple[list[str], list[str]]:
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(getattr(m, "text", ""))
            if getattr(m, "type", None) == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    return console_errors, page_errors


@pytest.mark.e2e
def test_an_unknown_list_draws_nothing_and_an_empty_one_says_so(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """Cases 1, 2, 3 — the two that must NOT say 尚無標的, then the one that must."""
    base = flow_server(_seed_none)
    page = fresh_page
    console_errors, page_errors = _watch(page)

    held: list[Route] = []
    page.route("**/api/instruments", lambda r: held.append(r))   # hold, do not answer
    with page.expect_request("**/api/instruments"):
        page.goto(base + "/instruments.html", wait_until="load")
    page.wait_for_function(
        "() => document.readyState === 'complete' && !!document.querySelector('#inst-body')")
    assert held, "the list request was not intercepted"

    # (1) pre-fetch: the table is blank because the answer is not in yet — not because
    #     there is nothing to show.
    assert _note(page) is None, f"case 1 drew an empty state before the fetch: {_note(page)!r}"
    assert _NO_DATA not in page.inner_text("body"), "case 1 claims 尚無標的 while loading"

    # (2) fetch failed: the fail toast says why; the table must stay blank, not lie.
    held[0].fulfill(status=500, content_type="application/json", body='{"detail":"boom"}')
    page.wait_for_selector(".toast-host .toast-fail")           # render() ran before the toast
    assert "標的清單載入失敗" in page.inner_text(".toast-host")
    assert _note(page) is None, f"case 2 drew an empty state after a failed fetch: {_note(page)!r}"
    assert _NO_DATA not in page.inner_text("body"), "case 2 claims 尚無標的 after a failed fetch"

    # (3) genuinely empty: now — and only now — say so, without the archived hint.
    page.unroute("**/api/instruments")
    page.reload(wait_until="load")
    page.wait_for_selector("#inst-empty")
    text3 = _note(page) or ""
    assert _NO_DATA in text3, text3
    assert _TOGGLE not in text3, f"nothing is archived, so nothing to reveal: {text3!r}"
    expect(page.locator("#toggle-archived")).to_be_hidden()
    assert page.evaluate("() => !document.querySelector('#inst-body #inst-empty')"), (
        "the placeholder must be a sibling of the table, never a child of <tbody>")

    real_console = [e for e in console_errors if "status of 500" not in e]  # the forced 500
    assert not real_console and not page_errors, (
        f"console={real_console!r} page={page_errors!r}")


@pytest.mark.e2e
def test_no_match_and_hidden_archived_hits_are_told_apart(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """Cases 5, 6, 4 — three different sentences, none of them 尚無標的."""
    base = flow_server(_seed_mixed)
    page = fresh_page
    console_errors, page_errors = _watch(page)

    page.goto(base + "/instruments.html", wait_until="load")
    page.wait_for_selector("#inst-body tr")
    expect(page.locator("#inst-body tr")).to_have_count(2)
    assert _note(page) is None, "rows are visible — no empty state may be drawn"

    # (5) the search matches nothing, visible or hidden -> name the query, no toggle hint
    #     (ARCHD does not match, so revealing archived rows would not help).
    page.fill("#inst-search", "zzzz-no-such-symbol")
    page.wait_for_selector("#inst-empty")
    expect(page.locator("#inst-body tr")).to_have_count(0)
    text5 = _note(page) or ""
    assert "zzzz-no-such-symbol" in text5, text5
    assert _NO_DATA not in text5, f"case 5 claims 尚無標的 with 3 registered rows: {text5!r}"
    assert _TOGGLE not in text5, f"case 5 points at a toggle that would reveal nothing: {text5!r}"

    # (6) the search matches ONLY the hidden archived row -> say so and name the toggle.
    page.fill("#inst-search", "ARCHD")
    page.wait_for_function(
        "() => { const n = document.querySelector('#inst-empty');"
        " return !!n && n.textContent.includes('已移除／封存'); }")
    text6 = _note(page) or ""
    assert _TOGGLE in text6 and "1 筆" in text6, text6
    assert _NO_DATA not in text6, text6
    page.click("#toggle-archived")                              # the hint must be true
    expect(page.locator("#inst-body tr.inst-archived")).to_have_count(1)
    expect(page.locator("#inst-empty")).to_have_count(0)
    page.click("#toggle-archived")
    page.wait_for_selector("#inst-empty")

    # (4) everything archived, no query -> the archived hint carries the full count.
    page.fill("#inst-search", "")
    expect(page.locator("#inst-body tr")).to_have_count(2)
    _archive(base, "2330")
    _archive(base, "AAPL")
    page.reload(wait_until="load")
    page.wait_for_selector("#inst-empty")
    expect(page.locator("#inst-body tr")).to_have_count(0)
    text4 = _note(page) or ""
    assert _TOGGLE in text4 and "3 筆" in text4, text4
    assert _NO_DATA not in text4, f"case 4 claims 尚無標的 with 3 archived rows: {text4!r}"
    expect(page.locator("#toggle-archived")).to_have_text("顯示已移除／封存 (3)")
    page.click("#toggle-archived")
    expect(page.locator("#inst-body tr")).to_have_count(3)
    expect(page.locator("#inst-empty")).to_have_count(0)

    assert len({text4, text5, text6}) == 3, (
        f"cases 4/5/6 must read differently: {text4!r} / {text5!r} / {text6!r}")
    assert not console_errors and not page_errors, (
        f"console={console_errors!r} page={page_errors!r}")
