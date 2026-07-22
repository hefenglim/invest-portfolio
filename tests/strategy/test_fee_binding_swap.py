"""Batch B T3: the what-if simulator picks the MARKET-appropriate fee rule set.

T3 swapped ``whatif.py``'s account-scalar fee lookup to :func:`fee_rule_for`. On a
DUAL-MARKET account (scalar ``moomoo_us``; bound US->moomoo_us, MY->moomoo_my) a what-if
on a US symbol must use ``moomoo_us`` and one on an MY symbol must use ``moomoo_my``,
proving the (account, market) binding — not the scalar — drives the fee. The oracle is the
REAL engine (``compute_fees``), resolving ``stamp_fx`` exactly as ``compute_whatif`` does.
"""

import sqlite3
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import get_fee_rule_set, seed_accounts
from portfolio_dash.data_ingestion.fees import FeeResult, compute_fees
from portfolio_dash.data_ingestion.fx_lookup import resolve_stamp_fx
from portfolio_dash.data_ingestion.store import upsert_instrument
from portfolio_dash.pricing.results import FxRow
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.pricing.store import upsert_fx
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.strategy.whatif import compute_whatif

_NOW = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))


def _merged_db() -> sqlite3.Connection:
    """In-memory DB with a DUAL-MARKET Moomoo account (scalar moomoo_us; US->moomoo_us,
    MY->moomoo_my) and one US + one MY instrument registered (unheld — what-if supplies the
    account explicitly). A USD/MYR rate is seeded for the US path's FE-D2 MY stamp."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    bootstrap_db(conn)
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
        [("merged", "US", "moomoo_us", "drip_us"), ("merged", "MY", "moomoo_my", "cash")],
    )
    upsert_instrument(conn, Instrument(symbol="MSFT", market=Market.US,
                                       quote_ccy=Currency.USD, sector="Tech", name="Microsoft"))
    upsert_instrument(conn, Instrument(symbol="1155", market=Market.MY,
                                       quote_ccy=Currency.MYR, sector="Financials",
                                       name="Maybank", board=".KL"))
    upsert_fx(conn, [FxRow(base=Currency.USD, quote=Currency.MYR, as_of=date(2026, 6, 9),
                           rate=Decimal("4.4"), source="test")], fetched_at=_NOW)
    conn.commit()
    return conn


def _oracle(conn: sqlite3.Connection, rule_name: str, side: Side,
            shares: Decimal, price: Decimal) -> FeeResult:
    rules = get_fee_rule_set(rule_name, conn)
    stamp_fx = resolve_stamp_fx(conn, _NOW.date()) if rules.has_us_stamp else None
    return compute_fees(rules, side, shares, price, is_etf=False, stamp_fx=stamp_fx)


def _whatif(conn: sqlite3.Connection, **kw: object) -> dict[str, Any]:
    base: dict[str, object] = dict(now=_NOW, reporting=Currency.TWD)
    base.update(kw)
    return compute_whatif(conn, **base)  # type: ignore[arg-type]


def test_whatif_us_symbol_uses_bound_us_rule() -> None:
    conn = _merged_db()
    r = _whatif(conn, symbol="MSFT", side=Side.BUY, shares=Decimal("100"),
                price=Decimal("200"), account_id="merged")
    want = _oracle(conn, "moomoo_us", Side.BUY, Decimal("100"), Decimal("200"))
    assert Decimal(str(r["fee"])) == want.fee
    assert Decimal(str(r["tax"])) == want.tax
    conn.close()


def test_whatif_my_symbol_uses_bound_my_rule_not_scalar() -> None:
    conn = _merged_db()
    r = _whatif(conn, symbol="1155", side=Side.BUY, shares=Decimal("1000"),
                price=Decimal("5"), account_id="merged")
    want_my = _oracle(conn, "moomoo_my", Side.BUY, Decimal("1000"), Decimal("5"))
    want_scalar = _oracle(conn, "moomoo_us", Side.BUY, Decimal("1000"), Decimal("5"))
    assert Decimal(str(r["fee"])) == want_my.fee
    assert Decimal(str(r["tax"])) == want_my.tax
    # Discrimination: the account scalar (moomoo_us) would have produced a different fee.
    assert (want_my.fee, want_my.tax) != (want_scalar.fee, want_scalar.tax)
    conn.close()
