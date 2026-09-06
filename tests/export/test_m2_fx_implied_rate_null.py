"""QA-10 (print report side) — a conversion that received nothing renders 「—」, not a crash.

``StoredFxConversion.implied_rate`` now answers ``None`` for ``to_amount == 0`` instead of
raising ``decimal.DivisionByZero``. The 換匯紀錄 section interpolated it into a sentence
(「1 USD = <rate> TWD」), so it had the same exposure the JSON route had: one hand-edited row
took the whole 帳本報告 down. The cell now says 「—」 outright — 「1 USD = — TWD」 reads as a
missing digit inside a real claim, which is worse than saying nothing.
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


def test_a_zero_to_amount_row_renders_an_em_dash_and_the_report_still_builds() -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO fx_conversions (account_id, date, from_ccy, from_amount, to_ccy,"
        " to_amount) VALUES ('schwab','2026-01-05','TWD','320000','USD','0')")
    conn.commit()
    section = _fx_section(build_ledgers_report_html(
        conn, now=GOLDEN_NOW, frm=None, to=None).content.decode("utf-8"))
    assert "—" in section
    assert "1 USD = — TWD" not in section
    conn.close()


def test_an_ordinary_conversion_still_states_its_rate() -> None:
    """Control: an ordinary conversion still states its rate — at 4 dp since M3-08 (it read
    「32」 here because the rate went through the TWD 0-dp money format; a rate is not money —
    see test_m3_08_fx_rate_is_not_money)."""
    conn = _conn()
    insert_fx_conversion(conn, account_id="schwab", date=date(2026, 2, 2),
                         from_ccy=Currency.TWD, from_amount=Decimal("64000"),
                         to_ccy=Currency.USD, to_amount=Decimal("2000"))
    section = _fx_section(build_ledgers_report_html(
        conn, now=GOLDEN_NOW, frm=None, to=None).content.decode("utf-8"))
    assert "1 USD = 32.0000 TWD" in section
    conn.close()
