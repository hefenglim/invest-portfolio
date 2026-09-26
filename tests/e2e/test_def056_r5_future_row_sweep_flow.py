"""E2E sweep (DEF-056 R5): no table row on any ledger surface shows a future date unbadged.

Why a sweep. R4's guard for the 「未來日期」 badge was a HAND-PICKED list of tables (the five
ledger tabs and the cash page's two lists) — and the verifier found the one table that list
did not name: the cash page's 現金收支明細, whose top row was a future broker fee under a header
that called a different number 目前餘額. The API had been right all along; the renderer never
read the flag, and a test that asserts only the tables it already knows about cannot notice a
table it does not. This test inverts the question: seed one future row of every kind, open
every view that lists ledger rows (every ledger tab, every cash pool's statement and each
account's all-currency statement, the stock drawer of every symbol with a future row), and
fail on ANY visible table row that prints the future date without the badge. A new table on
those views is covered the day it ships; the owner-ruled exception (the drawer's 交易明細
does not list future rows at all, ruling ③ 2026-09-25) needs no allowlist because it never
prints the date.
"""

import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from playwright.sync_api import Page
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.data_ingestion.store import insert_cash_movement, insert_corporate_action
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Currency
from tests.e2e.conftest import FlowServerFactory
from tests.e2e.test_def056_future_row_badge_flow import _seed_future_rows

D = Decimal
_AHEAD = "2099-01-02"

# Every visible <tr> that prints the future date but carries no badge, with the table it is
# in — evaluated in the page so the check is the same for every table, known or new.
_UNBADGED = """(ahead) => {
  const out = [];
  for (const tr of document.querySelectorAll('tr')) {
    if (!tr.getClientRects().length) continue;            // hidden tab / closed panel
    if (!tr.textContent.includes(ahead)) continue;
    // A badge counts only when it is SEEN: a `display:none` / `visibility:hidden` badge is
    // still in the DOM, and presence alone let a hidden mark pass (R5 verifier's note).
    const badge = tr.querySelector('.ledger-future');
    if (badge && badge.getClientRects().length
        && getComputedStyle(badge).visibility !== 'hidden') continue;
    if (tr.classList.contains('stmt-cut')) continue;       // the cut line names the day, not a row
    const host = tr.closest('[id]');
    out.push((host ? '#' + host.id : '?') + ' :: ' + tr.textContent.trim().slice(0, 90));
  }
  return out;
}"""


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _seed_every_kind(conn: sqlite3.Connection) -> None:
    """R4's six future rows (buy, dividend, conversion, opening, deposit) plus the two kinds
    that seed lacks: a 券商費用 in a TWD pool (the verifier's row) and a corporate action."""
    _seed_future_rows(conn)
    ahead = date.fromisoformat(_AHEAD)
    insert_cash_movement(conn, account_id="tw_broker", move_date=ahead,
                         kind="BROKER_FEE", ccy=Currency.TWD, amount=D("100"))
    insert_corporate_action(conn, account_id="schwab", action_date=ahead,
                            kind=CorporateActionKind.SPLIT, from_symbol="AAPL",
                            to_symbol="AAPL", ratio_to=D("2"), ratio_from=D("1"))
    conn.commit()


def _errors(page: Page) -> list[str]:
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    def _console(m: Any) -> None:
        if getattr(m, "type", None) == "error":
            errors.append(getattr(m, "text", ""))

    page.on("console", _console)
    return errors


def _sweep(page: Page, view: str, seen: dict[str, int], bad: list[str]) -> None:
    rows = page.locator("tr", has_text=_AHEAD)
    seen[view] = rows.count()
    bad.extend(f"{view}: {row}" for row in page.evaluate(_UNBADGED, _AHEAD))


@pytest.mark.e2e
def test_no_ledger_surface_prints_a_future_date_without_the_badge(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_every_kind)
    page = fresh_page
    errors = _errors(page)
    seen: dict[str, int] = {}
    bad: list[str] = []

    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#tx-body tr.expandable")
    _sweep(page, "trades:tx", seen, bad)
    for tab, tbody in (("#tab-ldiv", "#div-body"), ("#tab-lfx", "#fx-body"),
                       ("#tab-lopen", "#open-body"), ("#tab-laction", "#action-body"),
                       ("#tab-lcash", "#cash-body")):
        page.click(tab)
        page.wait_for_selector(f"{tbody} tr")
        _sweep(page, "trades" + tab, seen, bad)

    for hash_ in ("#flows", "#fx"):
        page.goto(base + "/cash.html" + hash_, wait_until="load")
        page.wait_for_selector("#cm-body tr" if hash_ == "#flows"
                               else "#cfx-ledger-body tr td.num")
        _sweep(page, "cash" + hash_, seen, bad)
    # Every account's all-currency statement and every pool's statement. The header names
    # the scope once the statement has rendered, so each sweep waits for ITS header.
    page.goto(base + "/cash.html", wait_until="load")
    page.wait_for_selector("#cash-cards .cash-line")
    cards = page.locator("#cash-cards .cash-card")
    for c in range(cards.count()):
        acct = cards.nth(c).locator(".acct").inner_text()
        scopes = [(cards.nth(c).locator(".acct"), acct + "・全部幣別")]
        lines = cards.nth(c).locator(".cash-line")
        for i in range(lines.count()):
            ccy = lines.nth(i).locator(".ccy").inner_text()
            scopes.append((lines.nth(i), acct + "・" + ccy + "　"))
        for target, head in scopes:
            target.click()
            page.wait_for_function(
                "(h) => document.querySelector('#cash-stmt-sub').textContent.startsWith(h)",
                arg=head)
            _sweep(page, f"cash:statement[{head.strip()}]", seen, bad)

    for symbol in ("2330", "AAPL"):
        page.goto(base + "/index.html", wait_until="load")
        page.wait_for_selector(".kpi-card")
        with page.expect_response(f"**/api/symbol/{symbol}/detail"):
            page.evaluate("(s) => window.pdOpenSymbol(s)", symbol)
        page.wait_for_selector(".sd-drawer .sd-signals")
        _sweep(page, f"drawer:{symbol}", seen, bad)

    assert not bad, "future rows without the 未來日期 badge:\n" + "\n".join(bad)
    # The sweep saw the future rows it was seeded with (a sweep that sees nothing proves
    # nothing): every ledger tab, both cash lists, at least one statement, the dividend
    # history in 2330's drawer.
    for view in ("trades:tx", "trades#tab-ldiv", "trades#tab-lfx", "trades#tab-lopen",
                 "trades#tab-laction", "trades#tab-lcash", "cash#flows", "cash#fx",
                 "drawer:2330"):
        assert seen.get(view, 0) >= 1, (view, seen)
    assert any(n >= 1 for v, n in seen.items() if v.startswith("cash:statement")), seen
    assert not errors, errors
