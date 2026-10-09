"""E2E M6-01 / M6-02: the 股利收件匣's own actions must keep the sidebar honest and its
undo strip readable.

**M6-01.** ``shell.js`` exposes ``window.pdRefreshInboxBadge`` and its comment names the three
callers it exists for — 「a rebate confirm/skip, a dividend commit, an inbox action」. Two of
the three call it (``rebate-inbox.js``); the dividend inbox never did. So confirming a
dividend moved the panel badge 4 -> 3 and left the sidebar reading 4 with the title 「4 筆待確認
（配息 4・折讓款 0）」 — indefinitely, because nothing was scheduled to correct it. The network
log after a confirm carried no ``/count`` request at all: not a race, an omission.

**M6-02.** 全部確認 on a 2330 group books the cash dividend AND the 配股, and both landed in the
「本次已入帳」 undo strip as 「2330・<same date>・Net …」 — the stock row reading 「Net 0 TWD」,
which is what the ledger honestly holds for it (``gross=net=0, reinvest_shares=200``). Two rows
with the same code and the same date, each next to a 復原 button that DELETES a ledger row: the
numbers were right and the labels could not tell the owner which button removed which entry.

Seeded so both findings share one action: ONE TW event carrying both a cash and a stock
distribution produces exactly the two same-symbol/same-date items the strip has to
distinguish, and confirming them is also the badge-changing act.
"""

import json
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime
from datetime import date as ddate
from decimal import Decimal
from typing import Any

import pytest
from playwright.sync_api import Page
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.pricing.results import DividendEvent
from portfolio_dash.pricing.store import upsert_dividend_events
from portfolio_dash.shared.enums import Currency, Market
from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory

_FETCHED = datetime(2026, 6, 11, tzinfo=UTC)
_EX = "2026-06-10"


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _seed_inbox(conn: Any) -> None:
    """Golden (tw_broker holds 1000 × 2330) + one event with BOTH distributions.

    現金 7 元/股 × 1,000 = 7,000 TWD, and 配股 2 元 (面額制) → 1,000 × 2/10 = 200 股.
    """
    _seed_golden(conn)
    upsert_dividend_events(conn, [DividendEvent(
        instrument="2330", market=Market.TW, ex_date=ddate.fromisoformat(_EX),
        cash_amount=Decimal("7"), stock_amount=Decimal("2"),
        currency=Currency.TWD, source="finmind")], fetched_at=_FETCHED)
    conn.commit()


def _count(base: str, path: str) -> int:
    with urllib.request.urlopen(base + path, timeout=5) as r:  # noqa: S310 (loopback)
        value: int = json.loads(r.read().decode("utf-8"))["count"]
        return value


def _badge(page: Page) -> str:
    node = page.query_selector(".sb-badge-alert")
    return node.inner_text().strip() if node else "0"


@pytest.mark.e2e
def test_dividend_inbox_action_refreshes_the_sidebar_badge_and_labels_the_strip(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base = flow_server(_seed_inbox)
    page = fresh_page
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(getattr(m, "text", ""))
            if getattr(m, "type", None) == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    dividends_before = _count(base, "/api/dividend-inbox/count")
    assert dividends_before == 2, "seed must produce exactly the cash + 配股 pair"
    rebates = _count(base, "/api/rebates/count")

    page.goto(base + "/dividend-inbox.html", wait_until="load")
    page.wait_for_selector("#inbox-list .inbox-group")
    page.wait_for_function(
        f"() => document.querySelector('#inbox-count').textContent === '{dividends_before}'")
    page.wait_for_function(
        "(n) => { const b = document.querySelector('.sb-badge-alert');"
        " return (b ? b.textContent.trim() : '0') === String(n); }",
        arg=dividends_before + rebates)

    # ---- M6-01: 全部確認 (both items) must move the SIDEBAR badge, not only the panel ----
    with page.expect_response("**/api/dividend-inbox/confirm") as confirmed:
        page.click("#inbox-list .inbox-group-head .btn")
        page.wait_for_selector(".modal-backdrop .modal-foot .btn-primary")
        page.click(".modal-backdrop .modal-foot .btn-primary")
    assert confirmed.value.status == 200, confirmed.value.status
    page.wait_for_selector("#inbox-list .inbox-note")          # panel emptied
    assert _count(base, "/api/dividend-inbox/count") == 0      # server agrees

    page.wait_for_function(
        "(n) => { const b = document.querySelector('.sb-badge-alert');"
        " return (b ? b.textContent.trim() : '0') === String(n); }",
        arg=rebates, timeout=8000)
    if rebates:
        assert "配息 0" in (page.get_attribute(".sb-badge-alert", "title") or "")

    # ---- M6-02: the undo strip must say WHICH of the two rows each 復原 deletes ---------
    page.wait_for_selector("#inbox-confirmed-strip.show .icf-row")
    labels = [page.inner_text(f"#inbox-confirmed-strip .icf-row:nth-child({i}) .icf-label")
              for i in (2, 3)]  # child 1 is the .icf-head
    assert len({label for label in labels}) == 2, (
        f"two 復原 buttons, indistinguishable labels: {labels!r}")
    joined = "　".join(labels)
    assert "7,000" in joined, joined                 # the cash dividend
    assert "200" in joined and "股" in joined, joined  # the 配股, by shares — not 「Net 0」
    assert "Net 0" not in joined, (
        f"the 配股 row is still labelled by a money figure it does not have: {joined!r}")

    # ---- counter-evidence: 復原 still deletes, and the badge follows THAT too ------------
    page.click("#inbox-confirmed-strip .icf-row:nth-child(2) .btn")
    # delDividendWithGuard opens confirmDialog with danger:true -> the confirm is .btn-danger
    page.wait_for_selector(".modal-backdrop .modal-foot .btn-danger")
    with page.expect_response("**/api/ledgers/dividends/**"):
        page.click(".modal-backdrop .modal-foot .btn-danger")
    page.wait_for_function(
        "(n) => { const b = document.querySelector('.sb-badge-alert');"
        " return (b ? b.textContent.trim() : '0') === String(n); }",
        arg=rebates + 1, timeout=8000)

    assert not console_errors and not page_errors, (
        f"inbox badge/strip flow: console={console_errors!r} page={page_errors!r}")
