"""Unit tests for the what-if trade simulator (spec 03 §3.2).

compute_whatif is compute-only: it reuses the REAL fee/tax engine (compute_fees) so the
numbers match the actual write path, and it never writes to any ledger table. The DB is
seeded via real write paths (mirroring tests/contract/test_export_tax.py's _db_with_sells)
so the holding cost basis is the one build_book produces, not a hand-rolled fixture.
"""

import sqlite3
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.pricing.store import upsert_fx, upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.strategy.whatif import compute_whatif

_NOW = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))


def _db() -> sqlite3.Connection:
    """In-memory DB seeded via real write paths: 2330 held 1000@500 in tw_broker, current
    price 600 TWD. (No AAPL/FX needed — TWD is the reporting ccy, so weight needs no rate.)"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    bootstrap_db(conn)
    create_pricing_tables(conn)
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW,
                                       quote_ccy=Currency.TWD, sector="Semiconductors",
                                       name="TSMC", board="TWSE"))
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=Decimal("1000"), price=Decimal("500"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 5))
    upsert_prices(conn, [PriceRow(instrument="2330", market=Market.TW,
                                  as_of=date(2026, 6, 9), close=Decimal("600"),
                                  source="test")], fetched_at=_NOW)
    upsert_fx(conn, [FxRow(base=Currency.USD, quote=Currency.TWD, as_of=date(2026, 6, 9),
                           rate=Decimal("33"), source="test")], fetched_at=_NOW)
    conn.commit()
    return conn


def _whatif(conn: sqlite3.Connection, **kw: object) -> dict[str, Any]:
    base: dict[str, object] = dict(now=_NOW, reporting=Currency.TWD)
    base.update(kw)
    return compute_whatif(conn, **base)  # type: ignore[arg-type]


def test_buy_unheld_symbol_in_given_account() -> None:
    """BUY a fresh symbol: total_cost = amount + fee + tax; new_adjusted_avg = total/shares."""
    conn = _db()
    upsert_instrument(conn, Instrument(symbol="2454", market=Market.TW,
                                       quote_ccy=Currency.TWD, sector="Semiconductors",
                                       name="MediaTek", board="TWSE"))
    r = _whatif(conn, symbol="2454", side=Side.BUY, shares=Decimal("100"),
                price=Decimal("1000"), account_id="tw_broker")
    amount = Decimal(str(r["amount"]))
    fee = Decimal(str(r["fee"]))
    tax = Decimal(str(r["tax"]))
    total_cost = Decimal(str(r["total_cost"]))
    assert amount == Decimal("100000")
    assert total_cost == amount + fee + tax
    assert Decimal(str(r["new_shares"])) == Decimal("100")
    assert Decimal(str(r["new_adjusted_avg"])) == total_cost / Decimal("100")
    assert r["account_id"] == "tw_broker"
    conn.close()


def test_buy_adds_to_existing_holding() -> None:
    """BUY adding to a held position blends with the held adjusted cost total."""
    conn = _db()
    r = _whatif(conn, symbol="2330", side=Side.BUY, shares=Decimal("1000"),
                price=Decimal("600"), account_id="tw_broker")
    total_cost = Decimal(str(r["total_cost"]))
    # held: 1000 sh, original/adjusted cost total 500000 (buy 1000@500, no fee/tax).
    held_adj_total = Decimal("500000")
    held_shares = Decimal("1000")
    new_shares = held_shares + Decimal("1000")
    assert Decimal(str(r["new_shares"])) == new_shares
    assert Decimal(str(r["new_adjusted_avg"])) == (held_adj_total + total_cost) / new_shares
    conn.close()


def test_sell_within_holdings() -> None:
    """SELL <= held: proceeds_net/realized/remaining all defined; oversell False."""
    conn = _db()
    r = _whatif(conn, symbol="2330", side=Side.SELL, shares=Decimal("400"),
                price=Decimal("600"), account_id="tw_broker")
    amount = Decimal(str(r["amount"]))
    fee = Decimal(str(r["fee"]))
    tax = Decimal(str(r["tax"]))
    proceeds_net = Decimal(str(r["proceeds_net"]))
    removed = Decimal(str(r["adjusted_cost_removed"]))
    realized = Decimal(str(r["realized"]))
    assert proceeds_net == amount - fee - tax
    # held adjusted_avg = 500000 / 1000 = 500.
    assert removed == Decimal("500") * Decimal("400")
    assert realized == proceeds_net - removed
    assert Decimal(str(r["remaining_shares"])) == Decimal("1000") - Decimal("400")
    assert r["oversell"] is False
    conn.close()


def test_sell_oversell_returns_full_numbers() -> None:
    """SELL > held: oversell True (soft warning) yet full numbers still returned."""
    conn = _db()
    r = _whatif(conn, symbol="2330", side=Side.SELL, shares=Decimal("5000"),
                price=Decimal("600"), account_id="tw_broker")
    assert r["oversell"] is True
    amount = Decimal(str(r["amount"]))
    fee = Decimal(str(r["fee"]))
    tax = Decimal(str(r["tax"]))
    proceeds_net = Decimal(str(r["proceeds_net"]))
    # realized still computed on the requested shares vs the held adjusted_avg (500).
    assert proceeds_net == amount - fee - tax
    assert Decimal(str(r["adjusted_cost_removed"])) == Decimal("500") * Decimal("5000")
    assert Decimal(str(r["realized"])) == proceeds_net - Decimal("500") * Decimal("5000")
    assert Decimal(str(r["remaining_shares"])) == Decimal("1000") - Decimal("5000")
    conn.close()


def test_account_omitted_resolves_to_most_shares() -> None:
    """No account_id -> the account holding the most shares of the symbol, echoed back."""
    conn = _db()
    r = _whatif(conn, symbol="2330", side=Side.SELL, shares=Decimal("100"),
                price=Decimal("600"), account_id=None)
    assert r["account_id"] == "tw_broker"
    conn.close()


# --- R7 A4: OLD-vs-NEW fields (old_shares / old_*_avg / old_weight) + remaining_market_value ---


def test_old_fields_and_old_weight_present_on_buy() -> None:
    """The OLD triple reflects the held basis (500,000/1,000 = 500, no dividend so
    adjusted == original); old_weight = current position value / current total (single-holding
    portfolio → 1), surfaced from the SAME dashboard pass as new_weight (no duplicate build)."""
    conn = _db()
    r = _whatif(conn, symbol="2330", side=Side.BUY, shares=Decimal("1000"),
                price=Decimal("600"), account_id="tw_broker")
    assert Decimal(str(r["old_shares"])) == Decimal("1000")
    assert Decimal(str(r["old_original_avg"])) == Decimal("500")
    assert Decimal(str(r["old_adjusted_avg"])) == Decimal("500")
    assert r["old_weight"] is not None
    assert Decimal(str(r["old_weight"])) == Decimal("1")
    conn.close()


def test_sell_remaining_market_value_is_remaining_times_current_price() -> None:
    """SELL 剩餘市值 = remaining shares × the CURRENT quote-ccy price (600), computed
    server-side — remaining 600 × 600 = 360,000. The OLD triple + old_weight ride along."""
    conn = _db()
    r = _whatif(conn, symbol="2330", side=Side.SELL, shares=Decimal("400"),
                price=Decimal("600"), account_id="tw_broker")
    assert Decimal(str(r["remaining_market_value"])) == Decimal("360000")
    # exactly remaining_shares × current price (not the trade price — the market price is 600).
    assert Decimal(str(r["remaining_market_value"])) == (
        Decimal(str(r["remaining_shares"])) * Decimal("600"))
    assert Decimal(str(r["old_shares"])) == Decimal("1000")
    assert r["old_weight"] is not None
    conn.close()


def test_sell_oversell_remaining_market_value_floors_at_zero() -> None:
    """Oversell: remaining_shares stays negative (unchanged), but 剩餘市值 floors at 0 — a
    negative market value is never fabricated."""
    conn = _db()
    r = _whatif(conn, symbol="2330", side=Side.SELL, shares=Decimal("5000"),
                price=Decimal("600"), account_id="tw_broker")
    assert r["oversell"] is True
    assert Decimal(str(r["remaining_shares"])) == Decimal("-4000")  # unchanged
    assert Decimal(str(r["remaining_market_value"])) == Decimal("0")
    conn.close()


def test_old_fields_null_for_fresh_buy() -> None:
    """A fresh (unheld, unpriced) symbol → old_* null; new_weight/old_weight honestly None
    (no current price to weight against)."""
    conn = _db()
    upsert_instrument(conn, Instrument(symbol="2454", market=Market.TW,
                                       quote_ccy=Currency.TWD, sector="Semiconductors",
                                       name="MediaTek", board="TWSE"))
    r = _whatif(conn, symbol="2454", side=Side.BUY, shares=Decimal("100"),
                price=Decimal("1000"), account_id="tw_broker")
    assert r["old_shares"] is None
    assert r["old_original_avg"] is None
    assert r["old_adjusted_avg"] is None
    assert r["old_weight"] is None
    conn.close()
