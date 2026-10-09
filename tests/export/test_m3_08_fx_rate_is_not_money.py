"""M3-08 (print report side) — the 換匯紀錄 隱含匯率 is a RATE, formatted at 4 dp, not money.

``export/ledgers_report.py`` printed the implied rate through ``_fmt_amount(rate, from_ccy)``
— the *from* currency's minor unit — so a 165,000 TWD -> 5,892.86 USD conversion, whose
implied rate is 27.99998642…, came out as 「1 USD = 28 TWD」 (TWD is a 0-dp currency) and a
MYR leg was cut to the sen. ``data-and-pricing.md``: 「Rates are NOT money; the 2-dp rule
never applies to them … high precision 4–6 dp」. Owner ruling (b)+(4), 2026-09-06: the
report uses a rate format at 4 dp.
"""

import sqlite3
from datetime import date
from decimal import Decimal

from portfolio_dash.data_ingestion.store import insert_fx_conversion
from portfolio_dash.export.ledgers_report import build_ledgers_report_html
from portfolio_dash.shared.enums import Currency
from tests.conftest import GOLDEN_NOW, _seed_golden, init_golden_base


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_golden_base(conn)
    _seed_golden(conn)
    return conn


def _fx_section(html: str) -> str:
    start = html.index("換匯紀錄")
    return html[start:html.index("</section>", start)]


def test_a_twd_leg_is_not_rounded_to_whole_dollars() -> None:
    """165,000 TWD -> 5,892.86 USD: 27.99998642… must read 28.0000, never the 0-dp 「28」."""
    conn = _conn()
    insert_fx_conversion(conn, account_id="schwab", date=date(2026, 2, 3),
                         from_ccy=Currency.TWD, from_amount=Decimal("165000"),
                         to_ccy=Currency.USD, to_amount=Decimal("5892.86"))
    section = _fx_section(build_ledgers_report_html(
        conn, now=GOLDEN_NOW, frm=None, to=None).content.decode("utf-8"))
    assert "1 USD = 28.0000 TWD" in section, section
    assert "1 USD = 28 TWD" not in section
    conn.close()


def test_a_myr_leg_keeps_the_digits_the_sen_would_drop() -> None:
    """4,563.20 MYR -> 1,000 USD: 4.5632 — the money format would have printed 4.56."""
    conn = _conn()
    insert_fx_conversion(conn, account_id="moomoo_my", date=date(2026, 2, 4),
                         from_ccy=Currency.MYR, from_amount=Decimal("4563.20"),
                         to_ccy=Currency.USD, to_amount=Decimal("1000"))
    section = _fx_section(build_ledgers_report_html(
        conn, now=GOLDEN_NOW, frm=None, to=None).content.decode("utf-8"))
    assert "1 USD = 4.5632 MYR" in section, section
    assert "1 USD = 4.56 MYR" not in section
    conn.close()


def test_the_golden_conversion_states_its_rate_at_four_places() -> None:
    """Control: the golden 32,000 TWD -> 1,000 USD row reads 32.0000 (was 「32」)."""
    conn = _conn()
    section = _fx_section(build_ledgers_report_html(
        conn, now=GOLDEN_NOW, frm=None, to=None).content.decode("utf-8"))
    assert "1 USD = 32.0000 TWD" in section, section
    conn.close()
