"""E2E — DEF-077 (owner ruling ⑥ A, 2026-09-26): the dashboard's 資料新鮮度 panel reads the FX
triangle's three states the way the server judged them.

Real server, real browser, the golden ledger (which reads USD/TWD, USD/MYR and MYR/TWD):

* **日期不同, consistent on the common date** — USD/TWD moved to 2026-06-10 while the other
  two legs are still on 2026-06-09, where 4.4 × 7 = 30.8 closes exactly. The note says
  「日期不同」, names each leg's date and the common date, reads 「（一致）」 and is NOT amber.
  Before DEF-077 this compared 31.5 with 30.8 across the two days and printed an amber
  「超過 0.05%」 with no data error anywhere (verifier J-01).
* **無法比較** — the legs share no date in the lookback: the note says so, neutrally.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from datetime import date
from decimal import Decimal

import pytest
from playwright.sync_api import Page, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.pricing.results import FxRow
from portfolio_dash.pricing.store import upsert_fx
from portfolio_dash.shared.enums import Currency
from tests.conftest import GOLDEN_NOW, _seed_golden
from tests.e2e.conftest import FlowServerFactory


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _seed_usd_twd_a_day_ahead(conn: sqlite3.Connection) -> None:
    _seed_golden(conn)
    upsert_fx(conn, [
        FxRow(base=Currency.USD, quote=Currency.TWD, as_of=date(2026, 6, 9),
              rate=Decimal("30.8"), source="test"),
        FxRow(base=Currency.USD, quote=Currency.TWD, as_of=date(2026, 6, 10),
              rate=Decimal("31.5"), source="test"),
    ], fetched_at=GOLDEN_NOW)
    conn.commit()


def _seed_no_common_date(conn: sqlite3.Connection) -> None:
    _seed_golden(conn)
    conn.execute("DELETE FROM fx_rates WHERE base='MYR' AND quote='TWD'")
    upsert_fx(conn, [FxRow(base=Currency.MYR, quote=Currency.TWD, as_of=date(2026, 4, 1),
                           rate=Decimal("7"), source="test")], fetched_at=GOLDEN_NOW)
    conn.commit()


def _triangle_note(page: Page, base: str) -> tuple[str, str]:
    page.goto(base + "/index.html", wait_until="load")
    page.wait_for_selector(".kpi-card")
    page.locator("#freshness > summary").click()
    note = page.locator("#fresh-notes .fresh-note", has_text="匯率三角一致性")
    expect(note).to_have_count(1)
    color: str = note.evaluate("(n) => n.style.color")
    return note.inner_text(), color


@pytest.mark.e2e
@pytest.mark.parametrize(("seed", "expected", "absent"), [
    (_seed_usd_twd_a_day_ahead,
     ("日期不同", "USD/TWD 2026-06-10", "USD/MYR 2026-06-09", "以共同日期 2026-06-09 比較",
      "（一致）"),
     ("超過 0.05%", "無法比較")),
    (_seed_no_common_date,
     ("無法比較", "沒有共同日期", "日期不同", "MYR/TWD 2026-04-01"),
     ("超過 0.05%", "（一致）")),
], ids=["dates-differ-consistent", "no-common-date"])
def test_the_triangle_note_reads_the_servers_verdict(
    flow_server: FlowServerFactory, fresh_page: Page,
    seed: Callable[[sqlite3.Connection], None], expected: tuple[str, ...],
    absent: tuple[str, ...],
) -> None:
    base = flow_server(seed)
    page = fresh_page
    page_errors: list[str] = []
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    text, color = _triangle_note(page, base)
    for needle in expected:
        assert needle in text, f"missing 「{needle}」 in: {text}"
    for needle in absent:
        assert needle not in text, f"unexpected 「{needle}」 in: {text}"
    assert color == "", f"only a real disagreement is amber; this note is {color!r}: {text}"
    assert not page_errors, page_errors
