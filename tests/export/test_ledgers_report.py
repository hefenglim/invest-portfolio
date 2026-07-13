"""Unit tests for the 帳本報告 HTML builder (export.ledgers_report).

Drives ``build_ledgers_report_html`` directly over the golden seed (2 transactions, 1
dividend, 1 FX conversion; no opening inventory). The builder mirrors the four ledger data
sources and only FORMATS stored values + sums listed columns, so the tests assert section
structure, date-range filtering, per-currency total reconciliation, empty-section rendering,
XSS escaping, and self-containment.
"""

import sqlite3
from datetime import date
from decimal import Decimal

from portfolio_dash.data_ingestion.store import upsert_instrument, upsert_opening
from portfolio_dash.export.artifact import ExportArtifact
from portfolio_dash.export.ledgers_report import build_ledgers_report_html
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


def _build(
    conn: sqlite3.Connection, frm: str | None = None, to: str | None = None
) -> ExportArtifact:
    return build_ledgers_report_html(conn, now=GOLDEN_NOW, frm=frm, to=to)


def test_filename_and_media_type() -> None:
    conn = _golden_conn()
    try:
        art = _build(conn)
    finally:
        conn.close()
    assert isinstance(art, ExportArtifact)
    assert art.filename == "ledger-report-20260611-1430.html"
    assert art.media_type == "text/html; charset=utf-8"


def test_all_sections_and_unbounded_range_label() -> None:
    conn = _golden_conn()
    try:
        art = _build(conn)
    finally:
        conn.close()
    doc = art.content.decode("utf-8")
    assert doc.lstrip().startswith("<!doctype html>")
    assert "<title>帳本報告</title>" in doc
    for heading in ("交易紀錄", "股利紀錄", "換匯紀錄", "期初庫存"):
        assert heading in doc
    assert "全部期間" in doc  # unbounded range label
    assert "共 2 筆" in doc   # the 2 golden transactions
    # opening inventory is empty in the golden seed.
    assert "本區間無紀錄" in doc
    # version stamp present.
    assert "portfolio-dash v" in doc


def test_per_currency_totals_reconcile() -> None:
    """Per-currency totals are simple Decimal sums of the listed columns.

    Dividend net = 5,000 TWD. FX 換出 = 32,000 TWD, 換入 = 1,000 USD. Transaction net cash:
    2330 buy = -(1000×500) = -500,000 TWD; AAPL buy = -(10×100) = -1,000 USD.
    """
    conn = _golden_conn()
    try:
        art = _build(conn)
    finally:
        conn.close()
    doc = art.content.decode("utf-8")
    assert "淨額合計" in doc and "5,000 TWD" in doc      # dividend net
    assert "淨現金流合計" in doc and "-500,000 TWD" in doc  # transaction net cash
    assert "換出合計" in doc and "32,000 TWD" in doc       # FX out
    assert "換入合計" in doc and "1,000.00 USD" in doc      # FX in


def test_date_range_filters_each_section() -> None:
    """A range covering only the dividend date (2026-03-01) drops the transactions/FX
    (all in January) but keeps the dividend row."""
    conn = _golden_conn()
    try:
        art = _build(conn, frm="2026-02-01", to="2026-04-01")
    finally:
        conn.close()
    doc = art.content.decode("utf-8")
    assert "2026-02-01 ～ 2026-04-01" in doc  # bounded range label in the header
    # 交易紀錄 + 換匯紀錄 empty (their events are in January), 股利紀錄 keeps the dividend.
    assert "本區間無紀錄" in doc
    assert "5,000 TWD" in doc  # the 2026-03-01 dividend still present


def test_opening_inventory_section_renders_rows_and_total() -> None:
    conn = _golden_conn()
    try:
        upsert_opening(
            conn, account_id="tw_broker", symbol="2330", shares=Decimal("2000"),
            original_avg_cost=Decimal("450"), original_cost_total=Decimal("900000"),
            build_date=date(2025, 12, 1),
        )
        conn.commit()
        art = _build(conn)
    finally:
        conn.close()
    doc = art.content.decode("utf-8")
    assert "期初庫存" in doc
    assert "900,000 TWD" in doc   # original cost total (both the row and 原始成本合計)
    assert "原始成本合計" in doc
    assert "2025-12-01" in doc     # build date shown


def test_dynamic_strings_are_html_escaped() -> None:
    conn = _golden_conn()
    try:
        upsert_instrument(conn, Instrument(
            symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
            sector="Semiconductors", name="TS<M>C&Co", board="TWSE"))
        conn.commit()
        art = _build(conn)
    finally:
        conn.close()
    doc = art.content.decode("utf-8")
    assert "TS&lt;M&gt;C&amp;Co" in doc
    assert "TS<M>C" not in doc


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


def test_empty_range_document_valid() -> None:
    """A range past every record -> all four sections empty, still a valid document."""
    conn = _golden_conn()
    try:
        art = _build(conn, frm="2027-01-01", to="2027-12-31")
    finally:
        conn.close()
    doc = art.content.decode("utf-8")
    assert doc.lstrip().startswith("<!doctype html>")
    assert "<title>帳本報告</title>" in doc
    # every section is empty.
    assert doc.count("本區間無紀錄") == 4
