"""QA-03: 幣別配置 must print REPORTING-currency values under a reporting-currency header.

``CombinedView.by_currency_value`` is NATIVE (``portfolio/allocation.py`` accumulates each
holding's ``market_value`` in its own quote currency). ``portfolio/results.py`` says of the
sibling field: "anything that ranks or weights currencies must read THIS field"
(``by_currency_reporting``) — and ``strategy/alerts.py::currency_weights`` already obeys.
The 持倉報告's currency block did not: it iterated the NATIVE dict and printed every amount
under a 「市值（<報告幣>）」 header, beside a 產業配置 table whose identical header carried
genuinely converted numbers.

Measured (QA evidence ``v2_holdings_report.md``): 1,000 × 2330 @ TWD 600 plus 100 × AAPL @
USD 120 at USD/TWD 33, reporting TWD. The report's own header says 投資組合總市值 996,000
TWD and the 產業配置 row says 996,000 — while the 幣別配置 rows said TWD 600,000 and USD
**12,000**, summing to 612,000. The USD row was short by a factor of 33 and nothing on the
page said so.

Two branches are pinned here, because the field is additive with an empty default:
* the normal one — the reporting leg is present, so it is what gets printed;
* the legacy one — a ``CombinedView`` built before the reporting leg existed has an EMPTY
  ``by_currency_reporting``. Printing nothing would lose the section; printing the native
  amounts under a reporting header is the very defect above. So the native amounts are
  printed under a 「市值（原幣）」 header instead — labelled for what they are.
"""

import re
import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.export import holdings_report as report_mod
from portfolio_dash.export.holdings_report import build_holdings_report_html
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.portfolio.dashboard_models import DashboardData
from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.pricing.store import upsert_fx, upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import GOLDEN_NOW, init_golden_base

_TAG = re.compile(r"<[^>]+>")
_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)


def _seed(conn: sqlite3.Connection) -> None:
    """The QA-03 scenario: TWD 600,000 of 2330 + USD 12,000 of AAPL at USD/TWD 33.

    Reporting TWD, so the honest currency split is TWD 600,000 / USD 396,000 = 996,000 —
    which is also the KPI 總市值 and the single 產業配置 row.
    """
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Semiconductors", name="TSMC", board="TWSE"))
    upsert_instrument(conn, Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
                                       sector="Tech", name="Apple"))
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=Decimal("1000"), price=Decimal("600"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 5))
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("100"), price=Decimal("100"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 10))
    upsert_prices(conn, [
        PriceRow(instrument="2330", market=Market.TW, as_of=date(2026, 6, 9),
                 close=Decimal("600"), source="test"),
        PriceRow(instrument="AAPL", market=Market.US, as_of=date(2026, 6, 9),
                 close=Decimal("120"), source="test"),
    ], fetched_at=GOLDEN_NOW)
    upsert_fx(conn, [
        FxRow(base=Currency.USD, quote=Currency.TWD, as_of=date(2026, 6, 9),
              rate=Decimal("33"), source="test"),
        FxRow(base=Currency.MYR, quote=Currency.TWD, as_of=date(2026, 6, 9),
              rate=Decimal("7"), source="test"),
        FxRow(base=Currency.USD, quote=Currency.MYR, as_of=date(2026, 6, 9),
              rate=Decimal("4.5"), source="test"),
    ], fetched_at=GOLDEN_NOW)
    conn.commit()


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_golden_base(conn)
    _seed(conn)
    return conn


def _section(doc: str, heading: str) -> str:
    """The markup from *heading* to the end of the table that follows it."""
    start = doc.index(heading)
    end = doc.index("</table>", start)
    return doc[start:end]


def _cells(fragment: str, part: str) -> list[list[str]]:
    """``<thead>``/``<tbody>`` rows of *fragment*'s first table, as plain-text cells."""
    inner = fragment[fragment.index(f"<{part}>") + len(part) + 2:fragment.index(f"</{part}>")]
    out: list[list[str]] = []
    for tr in inner.split("</tr>"):
        cells = _CELL.findall(tr)
        if cells:
            out.append([_TAG.sub("", c).strip() for c in cells])
    return out


def _amount(text: str) -> Decimal:
    return Decimal(text.replace(",", ""))


def test_currency_allocation_prints_reporting_values_summing_to_the_header_total() -> None:
    """The USD row is 396,000 (12,000 × 33), and the rows sum to the report's own total."""
    conn = _conn()
    try:
        doc = build_holdings_report_html(
            conn, now=GOLDEN_NOW, reporting=Currency.TWD).content.decode("utf-8")
    finally:
        conn.close()

    block = _section(doc, "幣別配置")
    rows = {r[0]: r[1] for r in _cells(block, "tbody")}
    assert rows == {"TWD": "600,000", "USD": "396,000"}, rows
    # The defect's signature: the NATIVE 12,000 must not appear in this table at all.
    assert "12,000" not in block

    # Reconciliation — the currency split is a partition of the portfolio, so it must add up
    # to the same 996,000 the header, the KPI 總市值 and the 產業配置 row all report.
    assert sum((_amount(v) for v in rows.values()), Decimal("0")) == Decimal("996000")
    assert "投資組合總市值 996,000 TWD" in doc
    assert _cells(_section(doc, "產業配置"), "tbody") == [
        ["Information Technology", "996,000", "100.00%"]]


def test_currency_allocation_header_names_the_reporting_currency() -> None:
    conn = _conn()
    try:
        doc = build_holdings_report_html(
            conn, now=GOLDEN_NOW, reporting=Currency.TWD).content.decode("utf-8")
    finally:
        conn.close()
    block = _section(doc, "幣別配置")
    assert _cells(block, "thead") == [["幣別", "市值（TWD）"]]
    # A converted column says so, exactly as the 持倉明細表 note does.
    assert "依生成當下匯率換算" in doc[doc.index("幣別配置"):]


def test_legacy_view_without_the_reporting_leg_prints_native_under_a_native_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``by_currency_reporting`` is additive with an empty default (``results.py``).

    A ``CombinedView`` constructed before it existed carries only native amounts. The
    section must then be labelled 原幣 — never a native number under a reporting header,
    which is the defect this file exists for.
    """
    conn = _conn()
    try:
        real = build_dashboard(conn, now=GOLDEN_NOW, reporting=Currency.TWD)
        assert real.currency_view is not None
        legacy = real.currency_view.model_copy(update={"by_currency_reporting": {}})
        patched = real.model_copy(update={"currency_view": legacy})

        def _fake(_c: sqlite3.Connection, *, now: object, reporting: object) -> DashboardData:
            return patched

        monkeypatch.setattr(report_mod, "build_dashboard", _fake)
        doc = build_holdings_report_html(
            conn, now=GOLDEN_NOW, reporting=Currency.TWD).content.decode("utf-8")
    finally:
        conn.close()

    block = _section(doc, "幣別配置")
    assert _cells(block, "thead") == [["幣別", "市值（原幣）"]]
    # Each amount in ITS OWN currency's minor unit: TWD is 0 dp, USD is 2 dp.
    assert {r[0]: r[1] for r in _cells(block, "tbody")} == {"TWD": "600,000", "USD": "12,000.00"}
