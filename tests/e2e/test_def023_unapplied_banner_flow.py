"""E2E (real server + real frontend) — DEF-023, R2 bounce: the dashboard shows the
「公司行動無法套用」 block on a ledger with NO unregistered symbol, and its link lands on the row.

The R2 fix rendered the block only when the ledger ALSO had an unregistered symbol (its call sat
after another banner's early return), so the demo — every symbol registered — never showed it.
The verifier found that in a browser; this test does what the verifier did, black-box: seed the
golden ledger plus one SPLIT the replay refuses (dated before the only 2330 buy — the state a
trade deletion leaves behind), open the dashboard, and read the page.
"""

import sqlite3

from playwright.sync_api import Page, expect

from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory


def _seed_unapplied(conn: sqlite3.Connection) -> None:
    """Golden ledger (every symbol registered) + a SPLIT with no position to apply to."""
    _seed_golden(conn)
    conn.execute(
        "INSERT INTO corporate_actions (account_id,date,kind,from_symbol,to_symbol,"
        "ratio_to,ratio_from,cost_carry,note) VALUES "
        "('tw_broker','2025-12-01','SPLIT','2330','2330','2','1',NULL,NULL)")
    conn.commit()


def test_the_unapplied_block_renders_without_an_unregistered_symbol(
    flow_server: FlowServerFactory, fresh_page: Page,
) -> None:
    base = flow_server(_seed_unapplied)
    page = fresh_page
    page.goto(base + "/index.html")
    banner = page.locator("#unapplied-banner")
    expect(banner).to_be_visible()
    # The precondition that made R2 pass by accident: no unregistered-symbol banner at all.
    expect(page.locator("#unreg-banner")).to_have_count(0)
    expect(banner).to_contain_text("1 筆公司行動無法套用")
    expect(banner).to_contain_text("台灣券商")          # through pdNames, not 「tw_broker」
    expect(banner.locator(".unapplied-meta")).not_to_contain_text("tw_broker")
    link = banner.locator("a", has_text="前往該筆公司行動")
    expect(link).to_have_count(1)
    href = link.get_attribute("href") or ""
    assert href.startswith("trades.html?ledger=action&action_id="), href

    # …and the entry goes where it says: the 公司行動 tab, with that row flashed.
    link.click()
    page.wait_for_url("**/trades.html?ledger=action*")
    expect(page.locator("#action-body tr.ledger-added-row")).to_have_count(1)
    expect(page.locator("#action-body tr.ledger-added-row")).to_contain_text("2330")
