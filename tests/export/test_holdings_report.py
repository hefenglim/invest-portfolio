"""Unit tests for the 持倉報告 HTML builder (export.holdings_report).

Drives ``build_holdings_report_html`` directly over the golden seed (2330 in tw_broker,
AAPL in schwab). Numbers of record come from build_dashboard; this module only formats
them, so the tests assert section structure, TOTAL-vs-rows reconciliation, XSS escaping,
and self-containment — never re-deriving money.
"""

import sqlite3

from portfolio_dash.data_ingestion.store import upsert_instrument
from portfolio_dash.export.artifact import ExportArtifact
from portfolio_dash.export.holdings_report import build_holdings_report_html
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from tests.conftest import GOLDEN_NOW, _seed_golden, init_golden_base


def _golden_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_golden_base(conn)
    _seed_golden(conn)
    conn.commit()
    return conn


def _build(conn: sqlite3.Connection) -> ExportArtifact:
    return build_holdings_report_html(conn, now=GOLDEN_NOW, reporting=Currency.TWD)


def test_filename_and_media_type() -> None:
    conn = _golden_conn()
    try:
        art = _build(conn)
    finally:
        conn.close()
    assert isinstance(art, ExportArtifact)
    # GOLDEN_NOW = 2026-06-11 14:30 Asia/Taipei -> minute-precision generation stamp.
    assert art.filename == "holdings-report-20260611-1430.html"
    assert art.media_type == "text/html; charset=utf-8"


def test_document_structure_all_sections_present() -> None:
    conn = _golden_conn()
    try:
        art = _build(conn)
    finally:
        conn.close()
    doc = art.content.decode("utf-8")
    assert doc.lstrip().startswith("<!doctype html>")
    assert "<title>持倉報告</title>" in doc
    for heading in ("KPI 摘要", "持倉明細表", "配置"):
        assert heading in doc
    # header nature statement + version stamp.
    assert "數字以生成當下之市價與匯率計算" in doc
    assert "portfolio-dash v" in doc
    # both holdings appear.
    assert "2330" in doc and "TSMC" in doc
    assert "AAPL" in doc and "Apple" in doc


def test_total_reconciles_with_summed_rows() -> None:
    """TOTAL market value (report ccy) == Σ per-row reporting values.

    2330: 1000 × 600 = 600,000 TWD. AAPL: 10 × 120 × 33 (USD/TWD) = 39,600 TWD.
    Total = 639,600 TWD — also the KPI 總市值.
    """
    conn = _golden_conn()
    try:
        art = _build(conn)
    finally:
        conn.close()
    doc = art.content.decode("utf-8")
    assert "600,000" in doc   # 2330 reporting value (a summed row)
    assert "39,600" in doc    # AAPL reporting value (a summed row)
    assert "639,600" in doc   # TOTAL row == 600,000 + 39,600, and the KPI 總市值


def test_allocation_has_sector_and_currency_tables() -> None:
    conn = _golden_conn()
    try:
        art = _build(conn)
    finally:
        conn.close()
    doc = art.content.decode("utf-8")
    assert "產業配置" in doc
    assert "幣別配置" in doc
    # Allocation-table sector name (canonicalized): R6 folds the seed's 'Semiconductors' (2330)
    # and 'Tech' (AAPL) into the single GICS Information Technology slice. ('Tech' is a substring
    # of 'Technology' inside that label, so we assert only the disappeared 'Semiconductors'.)
    assert "Information Technology" in doc
    assert "Semiconductors" not in doc


def test_dynamic_strings_are_html_escaped() -> None:
    """An instrument name carrying HTML metacharacters must be escaped, never injected."""
    conn = _golden_conn()
    try:
        upsert_instrument(conn, Instrument(
            symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
            sector="Tech", name="App<le>&Co"))
        conn.commit()
        art = _build(conn)
    finally:
        conn.close()
    doc = art.content.decode("utf-8")
    assert "App&lt;le&gt;&amp;Co" in doc
    assert "App<le>" not in doc


def test_self_contained_no_external_assets() -> None:
    conn = _golden_conn()
    try:
        art = _build(conn)
    finally:
        conn.close()
    doc = art.content.decode("utf-8")
    assert "http://" not in doc
    assert "https://" not in doc
    assert "<script" not in doc
    assert "<link" not in doc


def test_empty_portfolio_still_valid_document() -> None:
    """No holdings (base schema, no ledger rows) -> valid doc with empty-state notices."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_golden_base(conn)
    conn.commit()
    try:
        art = _build(conn)
    finally:
        conn.close()
    doc = art.content.decode("utf-8")
    assert doc.lstrip().startswith("<!doctype html>")
    assert "<title>持倉報告</title>" in doc
    assert "目前無持倉" in doc


def test_per_holding_return_ratio_is_display_percentage() -> None:
    """A holding with a positive adjusted cost renders a 2-dp percentage return =
    unrealized ÷ adjusted cost. 2330 = 105,000 / 495,000 ≈ 21.21%. The adjusted avg
    495 = (500,000 − 5,000 dividend) / 1,000 is the origin of that cost basis."""
    conn = _golden_conn()
    try:
        art = _build(conn)
    finally:
        conn.close()
    doc = art.content.decode("utf-8")
    assert "21.21%" in doc  # 2330 unrealized 105,000 / adjusted cost 495,000
    assert "495" in doc     # 2330 adjusted avg (TWD, 0 dp)
