"""Unit tests for the 再平衡試算執行報告 HTML builder (export.rebalance_report).

Drives ``build_rebalance_report_html`` directly over the dual-account seed (AAPL held in
schwab + moomoo_my_us; 2330 in tw_broker) so the cross-account structure, the （零股）TW
odd-lot annotation, per-account subtotals, XSS escaping, and self-containment are all
asserted without the HTTP layer. Numbers of record come from compute_rebalance; this module
only formats them.
"""

import sqlite3
from decimal import Decimal

from portfolio_dash.data_ingestion.store import upsert_instrument
from portfolio_dash.export.artifact import ExportArtifact
from portfolio_dash.export.rebalance_report import build_rebalance_report_html
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from tests.conftest import GOLDEN_NOW, _seed_dual_account, init_golden_base

# Same targets the dual-account preview test uses: 2330 0.791 -> 0.60 (TW odd-lot SELL),
# AAPL combined 0.209 -> 0.40 (US BUY routed to the most-shares account, schwab).
_TARGETS = {"2330": Decimal("0.6"), "AAPL": Decimal("0.4")}


def _dual_account_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_golden_base(conn)
    _seed_dual_account(conn)
    conn.commit()
    return conn


def _build(conn: sqlite3.Connection, targets: dict[str, Decimal]) -> ExportArtifact:
    return build_rebalance_report_html(
        conn, now=GOLDEN_NOW, reporting=Currency.TWD, targets=targets
    )


def test_filename_and_media_type() -> None:
    conn = _dual_account_conn()
    try:
        art = _build(conn, _TARGETS)
    finally:
        conn.close()
    assert isinstance(art, ExportArtifact)
    # GOLDEN_NOW = 2026-06-11 14:30 Asia/Taipei -> generation wall-clock stamp.
    assert art.filename == "rebalance-plan-20260611-1430.html"
    assert art.media_type == "text/html; charset=utf-8"


def test_document_structure_and_both_accounts_in_execution_list() -> None:
    conn = _dual_account_conn()
    try:
        art = _build(conn, _TARGETS)
    finally:
        conn.close()
    doc = art.content.decode("utf-8")

    # Valid, titled HTML document.
    assert doc.lstrip().startswith("<!doctype html>")
    assert "<title>再平衡試算執行指南</title>" in doc

    # The four sections are present.
    for heading in ("摘要表（依標的）", "執行清單（依帳戶）", "彙總"):
        assert heading in doc

    # 執行清單 groups by account: BOTH accounts carrying a leg appear (schwab AAPL buy +
    # tw_broker 2330 sell). The AAPL constituents line lists the moomoo_my_us holding too.
    assert "TW Broker" in doc
    assert "Charles Schwab" in doc
    assert "Moomoo MY (US)" in doc  # AAPL's second constituent (chip under the summary row)

    # A per-account subtotal is rendered.
    assert "小計" in doc

    # Nature statement + version stamps present.
    assert "本報告為試算結果，不寫入帳本" in doc
    assert "portfolio-dash v" in doc


def test_tw_odd_lot_annotation_present() -> None:
    """2330 sells ~242 shares (not a whole 1,000-share 張) -> flagged as a 零股 leg."""
    conn = _dual_account_conn()
    try:
        art = _build(conn, _TARGETS)
    finally:
        conn.close()
    doc = art.content.decode("utf-8")
    assert "（零股）" in doc


def test_self_contained_no_external_assets() -> None:
    conn = _dual_account_conn()
    try:
        art = _build(conn, _TARGETS)
    finally:
        conn.close()
    doc = art.content.decode("utf-8")
    # Zero external refs: no remote URLs, no <script>, no <link> — opens offline.
    assert "http://" not in doc
    assert "https://" not in doc
    assert "<script" not in doc
    assert "<link" not in doc


def test_dynamic_strings_are_html_escaped() -> None:
    """An instrument name carrying HTML metacharacters must be escaped, never injected."""
    conn = _dual_account_conn()
    try:
        # Overwrite AAPL's name with an XSS-shaped payload (name flows into the report via
        # build_dashboard -> the summary + execution rows).
        upsert_instrument(conn, Instrument(
            symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
            sector="Tech", name="App<le>&Co"))
        conn.commit()
        art = _build(conn, _TARGETS)
    finally:
        conn.close()
    doc = art.content.decode("utf-8")
    assert "App&lt;le&gt;&amp;Co" in doc  # escaped form present
    assert "App<le>" not in doc            # raw injection absent


def test_empty_targets_still_valid_document() -> None:
    """No targets -> no trades -> still a valid document with the 目前無需任何交易 notice."""
    conn = _dual_account_conn()
    try:
        art = _build(conn, {})
    finally:
        conn.close()
    doc = art.content.decode("utf-8")
    assert doc.lstrip().startswith("<!doctype html>")
    assert "目前無需任何交易" in doc
    assert "彙總" in doc  # summary section still rendered
