"""Batch B: H1 coherence guard relaxed from a 1:1 market check to allowed-market SETS.

The merged Moomoo account holds US (USD) AND MY (MYR) instruments, so a single account
now binds two markets in ``account_market_rules``. The account↔instrument coherence guard
(:func:`validate_transaction`, audit H1) must accept an instrument whose market is ANY of
the account's bound markets, and still reject one that is not.

Single-market accounts (every account today) keep a settlement-derived singleton allowed
set, so their behavior — including the byte-exact rejection message — is unchanged; that is
pinned in :mod:`tests.data_ingestion.test_validate_hardening` and re-asserted here.
"""

import sqlite3
from datetime import date
from decimal import Decimal

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import upsert_instrument
from portfolio_dash.data_ingestion.validate import TxnInput, validate_transaction
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side

# The merged Moomoo account seeds BOTH a US and an MY binding (Batch B), so it holds both
# US (USD) and MY (MYR) positions in one account.
_MERGED = "moomoo_my"


def _register_instruments(conn: sqlite3.Connection) -> None:
    upsert_instrument(conn, Instrument(symbol="AAPL", market=Market.US,
                                       quote_ccy=Currency.USD, sector="Tech", name="Apple"))
    upsert_instrument(conn, Instrument(symbol="1155", market=Market.MY,
                                       quote_ccy=Currency.MYR, sector="Financials",
                                       name="Maybank"))
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW,
                                       quote_ccy=Currency.TWD, sector="Tech", name="TSMC"))


def _inp(acc: str, sym: str) -> TxnInput:
    return TxnInput(account_id=acc, symbol=sym, side=Side.BUY, quantity=Decimal("10"),
                    price=Decimal("100"), trade_date=date(2026, 6, 1))


def _kinds(conn: sqlite3.Connection, acc: str, sym: str) -> set[str]:
    return {i.kind for i in validate_transaction(conn, _inp(acc, sym))}


# --- dual-market account accepts EITHER bound market ------------------------


def test_dual_market_accepts_us_instrument(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    _register_instruments(conn)
    assert "market_mismatch" not in _kinds(conn, _MERGED, "AAPL")


def test_dual_market_accepts_my_instrument(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    _register_instruments(conn)
    assert "market_mismatch" not in _kinds(conn, _MERGED, "1155")


# --- dual-market account still rejects an UNbound market --------------------


def test_dual_market_rejects_tw_instrument(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    _register_instruments(conn)
    issues = validate_transaction(conn, _inp(_MERGED, "2330"))
    hit = next((i for i in issues if i.kind == "market_mismatch"), None)
    assert hit is not None and hit.needs_confirm is False
    assert "TW" in hit.message  # names the instrument's market


# --- single-market account behavior is unchanged (byte-exact message) ------


def test_single_market_us_rejects_my_byte_identical_message(conn: sqlite3.Connection) -> None:
    """A single-market US account (schwab) rejects an MY instrument with the exact pre-Batch-B
    message (settlement-derived label), proving no single-market drift from the SET relaxation."""
    seed_accounts(conn)  # schwab = US singleton (settlement USD, one binding)
    _register_instruments(conn)
    issues = validate_transaction(conn, _inp("schwab", "1155"))
    hit = next((i for i in issues if i.kind == "market_mismatch"), None)
    assert hit is not None
    assert hit.message == "1155 屬 MY 市場,不可登錄於 美股帳戶"
