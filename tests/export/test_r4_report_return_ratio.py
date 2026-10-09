"""R4 / QA-04 — the printed 報酬率 must be the SAME number the dashboard shows.

``export/holdings_report.py`` re-derived a per-holding return as
``unrealized_pnl / adjusted_cost_total`` while ``portfolio/dashboard.py`` publishes
``HoldingRow.unrealized_pct = unrealized_pnl / abs(original_cost_total)`` (audit H1).
Cash dividends drive the ADJUSTED total toward zero, so the printed denominator collapses
and the printed percentage explodes away from the on-screen one — two definitions of 報酬率
in one app, on the same holding, at the same instant.

These tests never re-derive money: they compare the string rendered into the report's
報酬率 column against the server-authoritative ``unrealized_pct`` formatted by the report's
own ``_fmt_pct``. Architecture rule: the export layer READS computed results.
"""

import re
import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_dividend,
    insert_transaction,
    upsert_instrument,
)
from portfolio_dash.export.holdings_report import build_holdings_report_html
from portfolio_dash.export.report_html import _fmt_pct
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import GOLDEN_NOW, init_golden_base

# The blueprint's measured scenario: buy 100 @ 100 with a 20 fee (original cost 10,020),
# price 150. Sweeping the cumulative cash dividend collapses the ADJUSTED cost toward zero.
_SHARES = Decimal("100")
_BUY_PRICE = Decimal("100")
_BUY_FEE = Decimal("20")
_MARKET_PRICE = Decimal("150")


def _conn(dividend: Decimal) -> sqlite3.Connection:
    """A single TWD holding whose cumulative cash dividend is *dividend* (may be 0)."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_golden_base(conn)
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW,
                                       quote_ccy=Currency.TWD, sector="Semiconductors",
                                       name="TSMC", board="TWSE"))
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=_SHARES, price=_BUY_PRICE, fees=_BUY_FEE, tax=Decimal("0"),
                       trade_date=date(2026, 1, 5))
    if dividend > 0:
        insert_dividend(conn, account_id="tw_broker", symbol="2330",
                        div_date=date(2026, 3, 1), div_type="CASH", gross=dividend,
                        withholding=Decimal("0"), net=dividend)
    upsert_prices(conn, [
        PriceRow(instrument="2330", market=Market.TW, as_of=date(2026, 6, 9),
                 close=_MARKET_PRICE, source="test"),
    ], fetched_at=GOLDEN_NOW)
    conn.commit()
    return conn


def _printed_return_cell(doc: str) -> str:
    """The 報酬率 cell of the single holding row — the LAST cell of the data row."""
    row = re.search(r"<tr><td class=\"l\"><span class=\"sym-code\">2330.*?</tr>", doc, re.S)
    assert row is not None, "the 持倉明細表 data row for 2330 was not rendered"
    cells: list[str] = re.findall(r"<td[^>]*>(.*?)</td>", row.group(0), re.S)
    assert len(cells) == 9, f"expected 9 columns in the holdings row, got {len(cells)}"
    return cells[-1]


@pytest.mark.parametrize("dividend", [
    Decimal("0"), Decimal("3000"), Decimal("6000"), Decimal("9000"), Decimal("10020"),
])
def test_printed_return_equals_dashboard_unrealized_pct(dividend: Decimal) -> None:
    """QA-04: across the dividend sweep the printed 報酬率 IS ``unrealized_pct``."""
    conn = _conn(dividend)
    try:
        data = build_dashboard(conn, now=GOLDEN_NOW, reporting=Currency.TWD)
        art = build_holdings_report_html(conn, now=GOLDEN_NOW, reporting=Currency.TWD)
    finally:
        conn.close()
    holding = next(h for h in data.holdings if h.symbol == "2330")
    doc = art.content.decode("utf-8")
    assert _printed_return_cell(doc) == _fmt_pct(holding.unrealized_pct), (
        f"cumulative dividend {dividend}: the printed 報酬率 disagrees with the "
        f"dashboard's unrealized_pct ({holding.unrealized_pct})"
    )


def test_the_measured_divergence_pair_is_closed() -> None:
    """QA-04's worst measured pair: 139.52% on screen vs 1,370.59% in print (gap 1,231 pp).

    original 10,020 · adjusted 1,020 · unrealized 13,980 ->
      13,980 / 10,020 = 139.52%   (dashboard, ORIGINAL cost — audit H1)
      13,980 /  1,020 = 1,370.59% (the print's re-derivation over ADJUSTED cost)
    """
    conn = _conn(Decimal("9000"))
    try:
        art = build_holdings_report_html(conn, now=GOLDEN_NOW, reporting=Currency.TWD)
    finally:
        conn.close()
    doc = art.content.decode("utf-8")
    assert _printed_return_cell(doc) == "139.52%"
    assert "1,370.59%" not in doc


def test_a_fully_recovered_position_still_prints_a_return() -> None:
    """At dividend == original cost the adjusted total is 0, so the old guard printed 「—」.

    ``unrealized_pct`` divides by the ORIGINAL cost, which is a sum of non-negative costs
    and can never be zero here, so the honest figure (149.70%) is printed instead of a
    blank that reads as "unknown".
    """
    conn = _conn(Decimal("10020"))
    try:
        art = build_holdings_report_html(conn, now=GOLDEN_NOW, reporting=Currency.TWD)
    finally:
        conn.close()
    assert _printed_return_cell(art.content.decode("utf-8")) == "149.70%"


def test_footnote_states_the_denominator_actually_used() -> None:
    """The footer must not advertise a denominator the column no longer uses."""
    conn = _conn(Decimal("3000"))
    try:
        art = build_holdings_report_html(conn, now=GOLDEN_NOW, reporting=Currency.TWD)
    finally:
        conn.close()
    doc = art.content.decode("utf-8")
    assert "報酬率＝未實現損益 ÷ 原始投入成本" in doc
    assert "報酬率＝未實現損益 ÷ 調整後成本" not in doc
