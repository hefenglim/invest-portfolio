"""QA-11: the 持倉明細表 合計 row must not print 0.00% when NO row had a weight.

``_holdings_table_html`` starts ``total_weight`` at ``Decimal(0)`` and adds only the
non-``None`` weights. When every row's weight is ``None`` — the reporting blend cannot be
formed because a currency has no rate at all — the accumulator is still 0, so the TOTAL row
printed a confident ``0.00%`` directly beneath a column of honest 「—」 cells.

That is the report contradicting its own nature statement ("市價或匯率不可得時該欄以「—」
標示，不臆測") and its own footer note ("缺價或缺匯率的欄位以「—」標示，並排除於合計之外"):
a total of nothing is not zero, it is unknown. Measured in the QA evidence
(``v6_low.py`` F-EXP-4 / ``v6_report.html``): two holdings, one USD, no USD/TWD rate → both
權重 cells 「—」 and the 合計 cell ``0.00%``.

The other direction is pinned too: a normally priced portfolio still prints the summed
weight, so the fix cannot degrade into "always 「—」".
"""

import re
import sqlite3
from collections.abc import Callable
from datetime import date
from decimal import Decimal

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.export.holdings_report import build_holdings_report_html
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import GOLDEN_NOW, _seed_golden, init_golden_base

_TAG = re.compile(r"<[^>]+>")
_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)
_NULL = "—"

# Column order of the 持倉明細表 (see ``_holdings_table_html``); the TOTAL row's first cell
# spans the first four, so its weight is at index 2 while a data row's is at index 5.
_ROW_WEIGHT = 5
_TOTAL_WEIGHT = 2


def _seed_unpriceable_blend(conn: sqlite3.Connection) -> None:
    """Two holdings, one of them USD — and NO USD/TWD rate anywhere in the database.

    The reporting-currency total therefore cannot be formed, so ``build_dashboard`` emits
    ``weight=None`` for BOTH rows (a weight is a share of a total that does not exist).
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
    conn.commit()


def _doc(seed: Callable[[sqlite3.Connection], None]) -> str:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_golden_base(conn)
    seed(conn)
    conn.commit()
    try:
        return build_holdings_report_html(
            conn, now=GOLDEN_NOW, reporting=Currency.TWD).content.decode("utf-8")
    finally:
        conn.close()


def _holdings_rows(doc: str) -> list[list[str]]:
    start = doc.index("持倉明細表")
    body = doc[doc.index("<tbody>", start) + 7:doc.index("</tbody>", start)]
    out: list[list[str]] = []
    for tr in body.split("</tr>"):
        cells = _CELL.findall(tr)
        if cells:
            out.append([_TAG.sub("", c).strip() for c in cells])
    return out


def test_total_weight_is_unknown_when_no_row_contributed_one() -> None:
    rows = _holdings_rows(_doc(_seed_unpriceable_blend))
    data_rows, total = rows[:-1], rows[-1]
    assert len(data_rows) == 2
    assert [r[_ROW_WEIGHT] for r in data_rows] == [_NULL, _NULL], data_rows
    assert total[0].startswith("合計"), total
    assert total[_TOTAL_WEIGHT] == _NULL, (
        f"the 合計 row printed {total[_TOTAL_WEIGHT]!r} while every row said 「—」")


def test_a_priced_portfolio_still_prints_the_summed_weight() -> None:
    """The golden seed: 600,000 TWD of 2330 + 10 × AAPL @ 120 × 33 = 39,600 TWD.

    Weights 600,000/639,600 and 39,600/639,600 sum to exactly 1 at the Decimal context's
    28 digits, so the 合計 cell reads 100.00% — a number, not 「—」.
    """
    rows = _holdings_rows(_doc(_seed_golden))
    total = rows[-1]
    assert total[0].startswith("合計"), total
    assert total[_TOTAL_WEIGHT] == "100.00%", total
