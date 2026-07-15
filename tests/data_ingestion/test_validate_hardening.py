"""Regression tests for the P3-batch3 Wave 2A validation hardening (audit findings).

Each audit finding's confirmed scenario is pinned here as a named regression test.
"""

import sqlite3
from datetime import date
from decimal import Decimal

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.manual import enter_transaction
from portfolio_dash.data_ingestion.store import upsert_instrument
from portfolio_dash.data_ingestion.validate import TxnInput, validate_transaction
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side


def _setup(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW,
                                       quote_ccy=Currency.TWD, sector="Tech", name="TSMC"))
    upsert_instrument(conn, Instrument(symbol="AAPL", market=Market.US,
                                       quote_ccy=Currency.USD, sector="Tech", name="Apple"))


def _inp(acc: str, sym: str, side: Side = Side.BUY, qty: str = "10", price: str = "100",
         *, fee: str | None = None, tax: str | None = None,
         trade_date: date = date(2026, 6, 1)) -> TxnInput:
    return TxnInput(
        account_id=acc, symbol=sym, side=side, quantity=Decimal(qty), price=Decimal(price),
        trade_date=trade_date,
        fee=Decimal(fee) if fee is not None else None,
        tax=Decimal(tax) if tax is not None else None,
    )


# --- H1: account↔instrument market coherence ------------------------------


def test_market_mismatch_rejected_hard(conn: sqlite3.Connection) -> None:
    _setup(conn)
    issues = validate_transaction(conn, _inp("tw_broker", "AAPL"))
    hit = next((i for i in issues if i.kind == "market_mismatch"), None)
    assert hit is not None and hit.needs_confirm is False
    assert "US" in hit.message and "台股" in hit.message  # names BOTH sides


def test_coherent_market_has_no_mismatch(conn: sqlite3.Connection) -> None:
    _setup(conn)
    kinds = {i.kind for i in validate_transaction(conn, _inp("schwab", "AAPL"))}
    assert "market_mismatch" not in kinds


# --- H2: negative fee / tax ------------------------------------------------


def test_negative_fee_rejected(conn: sqlite3.Connection) -> None:
    _setup(conn)
    kinds = {i.kind for i in validate_transaction(conn, _inp("tw_broker", "2330", fee="-1"))}
    assert "negative_fee" in kinds


def test_negative_tax_rejected(conn: sqlite3.Connection) -> None:
    _setup(conn)
    kinds = {i.kind for i in validate_transaction(conn, _inp("tw_broker", "2330", tax="-5"))}
    assert "negative_tax" in kinds


# --- M4: overflow-sized shares/price (no crash, hard issue) ----------------


def test_amount_too_large_rejected(conn: sqlite3.Connection) -> None:
    _setup(conn)
    kinds = {i.kind for i in validate_transaction(conn, _inp("tw_broker", "2330", price="1e13"))}
    assert "amount_too_large" in kinds


def test_overflow_price_does_not_crash_enter_transaction(conn: sqlite3.Connection) -> None:
    """price 1e999 500'd via the fee quantize before M4; now it degrades to issues."""
    _setup(conn)
    draft = enter_transaction(conn, _inp("tw_broker", "2330", price="1e999"), confirm=True)
    assert draft.written is False
    kinds = {i.kind for i in draft.issues}
    assert "amount_too_large" in kinds or "fee_overflow" in kinds


# --- M5: future trade date (soft, clock-gated) -----------------------------


def test_future_trade_date_soft_flag(conn: sqlite3.Connection) -> None:
    _setup(conn)
    issues = validate_transaction(
        conn, _inp("tw_broker", "2330", trade_date=date(2026, 6, 20)),
        today=date(2026, 6, 11))
    hit = next((i for i in issues if i.kind == "future_trade_date"), None)
    assert hit is not None and hit.needs_confirm is True


def test_future_check_skipped_without_clock(conn: sqlite3.Connection) -> None:
    _setup(conn)
    kinds = {i.kind for i in validate_transaction(
        conn, _inp("tw_broker", "2330", trade_date=date(2099, 1, 1)))}
    assert "future_trade_date" not in kinds


# --- M7: duplicate trade (soft) --------------------------------------------


def test_duplicate_trade_soft_flag(conn: sqlite3.Connection) -> None:
    _setup(conn)
    enter_transaction(conn, _inp("tw_broker", "2330", qty="100", price="600"), confirm=True)
    issues = validate_transaction(conn, _inp("tw_broker", "2330", qty="100", price="600"))
    hit = next((i for i in issues if i.kind == "duplicate_trade"), None)
    assert hit is not None and hit.needs_confirm is True


def test_no_duplicate_when_fields_differ(conn: sqlite3.Connection) -> None:
    _setup(conn)
    enter_transaction(conn, _inp("tw_broker", "2330", qty="100", price="600"), confirm=True)
    kinds = {i.kind for i in validate_transaction(
        conn, _inp("tw_broker", "2330", qty="100", price="601"))}
    assert "duplicate_trade" not in kinds
