"""Batch B T3: the manual + CSV entry paths pick the MARKET-appropriate fee rule set.

Wave 1 bound fee rules to (account, market) in ``account_market_rules``; T3 swapped the
account-scalar lookups in ``manual.py`` / ``csv_import.py`` to :func:`fee_rule_for`. For a
synthetic DUAL-MARKET account (US -> ``moomoo_us``, MY -> ``moomoo_my``) whose ACCOUNT
SCALAR is ``moomoo_us``, a US trade must use ``moomoo_us`` and an MY trade must use
``moomoo_my`` — proving the (account, market) BINDING, not the scalar, drives the fee.

The oracle is the REAL engine (``compute_fees`` with the market-appropriate rule set),
resolved exactly as the entry seam resolves ``stamp_fx``, so this is a rule-SELECTION test,
never a re-implementation of the fee math.
"""

import sqlite3
from datetime import date, datetime
from decimal import Decimal

import pytest

from portfolio_dash.data_ingestion.config_seed import get_fee_rule_set, seed_accounts
from portfolio_dash.data_ingestion.csv_import import build_transaction_preview
from portfolio_dash.data_ingestion.fees import FeeResult, compute_fees
from portfolio_dash.data_ingestion.fx_lookup import resolve_stamp_fx
from portfolio_dash.data_ingestion.manual import enter_transaction
from portfolio_dash.data_ingestion.store import upsert_instrument
from portfolio_dash.data_ingestion.validate import TxnInput
from portfolio_dash.pricing.results import FxRow
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.pricing.store import upsert_fx
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side

_TRADE_DATE = date(2026, 1, 10)


def _merged_db(conn: sqlite3.Connection) -> None:
    """A DUAL-MARKET Moomoo account: scalar ``moomoo_us``, but bound US->moomoo_us,
    MY->moomoo_my. Registers one US and one MY instrument and seeds a USD/MYR rate so the
    US path's FE-D2 MY stamp is genuinely computed."""
    create_pricing_tables(conn)
    seed_accounts(conn)
    conn.execute(
        "INSERT INTO accounts (account_id, name, broker, settlement_ccy, funding_ccy, "
        "fee_rule_set, dividend_model) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("merged", "Moomoo Merged", "Moomoo MY", "USD", "MYR", "moomoo_us", "drip_us"),
    )
    conn.executemany(
        "INSERT INTO account_market_rules (account_id, market, fee_rule_set, dividend_model) "
        "VALUES (?, ?, ?, ?)",
        [
            ("merged", "US", "moomoo_us", "drip_us"),
            ("merged", "MY", "moomoo_my", "cash"),
        ],
    )
    upsert_instrument(conn, Instrument(symbol="MSFT", market=Market.US,
                                       quote_ccy=Currency.USD, sector="Tech", name="Microsoft"))
    upsert_instrument(conn, Instrument(symbol="1155", market=Market.MY,
                                       quote_ccy=Currency.MYR, sector="Financials",
                                       name="Maybank", board=".KL"))
    upsert_fx(conn, [FxRow(base=Currency.USD, quote=Currency.MYR, as_of=date(2026, 1, 1),
                           rate=Decimal("4.4"), source="test")],
              fetched_at=datetime(2026, 1, 2, 12, 0))
    conn.commit()


def _oracle(conn: sqlite3.Connection, rule_name: str, side: Side,
            shares: Decimal, price: Decimal) -> FeeResult:
    """compute_fees with *rule_name*, resolving stamp_fx exactly as the entry seams do."""
    rules = get_fee_rule_set(rule_name, conn)
    stamp_fx = resolve_stamp_fx(conn, _TRADE_DATE) if rules.has_us_stamp else None
    return compute_fees(rules, side, shares, price, is_etf=False, stamp_fx=stamp_fx)


def test_manual_us_trade_uses_bound_us_rule(conn: sqlite3.Connection) -> None:
    _merged_db(conn)
    inp = TxnInput(account_id="merged", symbol="MSFT", side=Side.BUY,
                   quantity=Decimal("100"), price=Decimal("200"), trade_date=_TRADE_DATE)
    draft = enter_transaction(conn, inp, confirm=False)
    want = _oracle(conn, "moomoo_us", Side.BUY, Decimal("100"), Decimal("200"))
    assert (draft.fee, draft.tax) == (want.fee, want.tax)


def test_manual_my_trade_uses_bound_my_rule_not_the_scalar(
    conn: sqlite3.Connection,
) -> None:
    """The MY trade must use the BOUND moomoo_my rule, NOT the account scalar (moomoo_us)."""
    _merged_db(conn)
    inp = TxnInput(account_id="merged", symbol="1155", side=Side.BUY,
                   quantity=Decimal("1000"), price=Decimal("5"), trade_date=_TRADE_DATE)
    draft = enter_transaction(conn, inp, confirm=False)
    want_my = _oracle(conn, "moomoo_my", Side.BUY, Decimal("1000"), Decimal("5"))
    want_scalar = _oracle(conn, "moomoo_us", Side.BUY, Decimal("1000"), Decimal("5"))
    assert (draft.fee, draft.tax) == (want_my.fee, want_my.tax)
    # Discrimination: the scalar (moomoo_us) would have produced a DIFFERENT fee, so this
    # proves the (account, market) binding — not accounts.fee_rule_set — selected the rule.
    assert (want_my.fee, want_my.tax) != (want_scalar.fee, want_scalar.tax)


def test_manual_snapshot_matches_bound_rule(conn: sqlite3.Connection) -> None:
    """PRESERVE fee_rule_snapshot semantics: the MY snapshot equals the moomoo_my engine
    snapshot (same rule-set name in -> same snapshot content out)."""
    _merged_db(conn)
    inp = TxnInput(account_id="merged", symbol="1155", side=Side.BUY,
                   quantity=Decimal("1000"), price=Decimal("5"), trade_date=_TRADE_DATE)
    draft = enter_transaction(conn, inp, confirm=False)
    want_my = _oracle(conn, "moomoo_my", Side.BUY, Decimal("1000"), Decimal("5"))
    assert draft.fee_rule_snapshot == want_my.snapshot


def test_csv_import_selects_per_market_rule(conn: sqlite3.Connection) -> None:
    _merged_db(conn)
    csv_text = (
        "account,symbol,side,date,shares,price\r\n"
        "merged,MSFT,buy,2026-01-10,100,200\r\n"
        "merged,1155,buy,2026-01-10,1000,5\r\n"
    )
    preview = build_transaction_preview(conn, csv_text)
    us_row = next(r for r in preview.rows if r.payload.get("symbol") == "MSFT")
    my_row = next(r for r in preview.rows if r.payload.get("symbol") == "1155")
    want_us = _oracle(conn, "moomoo_us", Side.BUY, Decimal("100"), Decimal("200"))
    want_my = _oracle(conn, "moomoo_my", Side.BUY, Decimal("1000"), Decimal("5"))
    assert (us_row.fee, us_row.tax) == (want_us.fee, want_us.tax)
    assert (my_row.fee, my_row.tax) == (want_my.fee, want_my.tax)
    # The MY row's persisted snapshot (snap.* payload keys) mirrors the bound moomoo_my rule.
    snap = {k[5:]: v for k, v in my_row.payload.items() if k.startswith("snap.")}
    assert snap == want_my.snapshot


def test_unregistered_symbol_keeps_account_scalar(conn: sqlite3.Connection) -> None:
    """LOCKED None-fallback: an unregistered symbol has no instrument -> no market, so the
    account scalar is read exactly as before (the row is a HARD symbol_unresolved block, but
    the fee still auto-fills from the scalar rule set)."""
    _merged_db(conn)
    inp = TxnInput(account_id="merged", symbol="NOPE", side=Side.BUY,
                   quantity=Decimal("100"), price=Decimal("200"), trade_date=_TRADE_DATE)
    draft = enter_transaction(conn, inp, confirm=False)
    assert any(i.kind == "symbol_unresolved" for i in draft.issues)
    # scalar == moomoo_us: fee auto-filled from the scalar rule (fallback path, market None).
    want_scalar = _oracle(conn, "moomoo_us", Side.BUY, Decimal("100"), Decimal("200"))
    assert (draft.fee, draft.tax) == (want_scalar.fee, want_scalar.tax)


@pytest.mark.parametrize("market,rule", [(Market.US, "moomoo_us"), (Market.MY, "moomoo_my")])
def test_fee_rule_for_returns_market_appropriate_set(
    conn: sqlite3.Connection, market: Market, rule: str
) -> None:
    """Direct resolver check on the synthetic dual-market account (foundation for the swaps)."""
    from portfolio_dash.data_ingestion.rules_binding import fee_rule_for

    _merged_db(conn)
    assert fee_rule_for(conn, "merged", market) == rule
