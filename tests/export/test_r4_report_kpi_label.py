"""R4 / QA-17 — the printed KPI card must carry the label AI-D41 gave the figure.

``KpiSummary.total_return`` is figure **A**: ``Σ_ccy (realized + unrealized)_native ×
spot_today``. The rate is applied to each currency's GAIN, never to its PRINCIPAL, so it is
NOT the FX-complete lifetime result. AI-D41 (owner ruling 2026-08-24, ``domain-ledger.md``)
renamed it 「資產損益（不含本金匯率）」 for exactly that reason and drew a red line: A, B and
B−A are presented side by side and NEVER summed. ``web/app.js`` renders the new name; the
printed 持倉報告 kept calling the same field 「總報酬」, which reads as one grand total and
invites the reader to add the 換匯損益 card to it — the double count the ruling forbids.

The export builds from the same ``DashboardData``, so it also knows B is a separate figure;
this section prints A only and must say so rather than restoring the misleading label.
"""

import sqlite3
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
from tests.conftest import GOLDEN_NOW, init_golden_base


def _conn() -> sqlite3.Connection:
    """One priced TWD holding — enough for the KPI section to render 總報酬 / A."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_golden_base(conn)
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW,
                                       quote_ccy=Currency.TWD, sector="Semiconductors",
                                       name="TSMC", board="TWSE"))
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=Decimal("1000"), price=Decimal("500"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 5))
    upsert_prices(conn, [
        PriceRow(instrument="2330", market=Market.TW, as_of=date(2026, 6, 9),
                 close=Decimal("600"), source="test"),
    ], fetched_at=GOLDEN_NOW)
    conn.commit()
    return conn


def _doc() -> str:
    conn = _conn()
    try:
        art = build_holdings_report_html(conn, now=GOLDEN_NOW, reporting=Currency.TWD)
    finally:
        conn.close()
    return art.content.decode("utf-8")


def test_kpi_card_uses_the_ai_d41_label() -> None:
    """QA-17: the card for ``total_return`` is 「資產損益（不含本金匯率）」, not 「總報酬」."""
    doc = _doc()
    assert "資產損益（不含本金匯率）" in doc
    assert "總報酬" not in doc


def test_report_states_the_section_is_figure_a_only() -> None:
    """A reader must be told this figure excludes the principal's FX effect and is never
    summed with 換匯損益 — otherwise the honest label alone still invites the double count."""
    doc = _doc()
    assert "本金匯率效果" in doc
    assert "相加" in doc
