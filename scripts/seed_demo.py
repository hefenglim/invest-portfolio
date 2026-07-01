"""Seed the DEMO database with synthetic (fictional) portfolio data.

Ops script for the public demo instance: it populates a realistic multi-currency,
multi-account portfolio so visitors can explore a full dashboard WITHOUT any real
data. Point ``DB_PATH`` at the demo folder and run once:

    DB_PATH=/home/<user>/data-demo/portfolio.db .venv/bin/python scripts/seed_demo.py

Everything here is FICTIONAL. It guards on an existing transaction ledger and refuses
to double-seed, so it is safe to re-run — but never point it at a real ledger.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_dividend,
    insert_fx_conversion,
    insert_transaction,
    upsert_instrument,
)
from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.pricing.store import upsert_fx, upsert_prices
from portfolio_dash.shared.config import get_settings
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side

_NOW = datetime.now(ZoneInfo("Asia/Taipei"))
_TODAY = _NOW.date()


def _already_seeded(conn: sqlite3.Connection) -> bool:
    try:
        count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    except sqlite3.OperationalError:
        return False
    return bool(count)


def seed(conn: sqlite3.Connection) -> None:
    # Idempotent table setup (safe whether the app has booted this DB yet or not).
    bootstrap_db(conn)
    create_pricing_tables(conn)
    seed_accounts(conn)
    if _already_seeded(conn):
        print("demo DB already has a transaction ledger — skipping (idempotent).")
        return

    # --- instruments (fictional but plausible; names tagged DEMO) ---
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Semiconductors", name="台積電 (DEMO)", board="TWSE"))
    upsert_instrument(conn, Instrument(symbol="0056", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="ETF", name="元大高股息 (DEMO)", board="TWSE"))
    upsert_instrument(conn, Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
                                       sector="Tech", name="Apple (DEMO)"))
    upsert_instrument(conn, Instrument(symbol="NVDA", market=Market.US, quote_ccy=Currency.USD,
                                       sector="Semiconductors", name="NVIDIA (DEMO)"))
    upsert_instrument(conn, Instrument(symbol="1155", market=Market.MY, quote_ccy=Currency.MYR,
                                       sector="Banking", name="Maybank (DEMO)"))

    # --- transactions across all four accounts ---
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=Decimal("2000"), price=Decimal("600"), fees=Decimal("171"),
                       tax=Decimal("0"), trade_date=date(2026, 1, 6))
    insert_transaction(conn, account_id="tw_broker", symbol="0056", side=Side.BUY,
                       quantity=Decimal("5000"), price=Decimal("38"), fees=Decimal("27"),
                       tax=Decimal("0"), trade_date=date(2026, 2, 10))
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("30"), price=Decimal("225"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 1, 15))
    insert_transaction(conn, account_id="schwab", symbol="NVDA", side=Side.BUY,
                       quantity=Decimal("20"), price=Decimal("140"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 3, 3))
    insert_transaction(conn, account_id="moomoo_my_us", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("10"), price=Decimal("240"), fees=Decimal("1"),
                       tax=Decimal("0"), trade_date=date(2026, 4, 1))
    insert_transaction(conn, account_id="moomoo_my_my", symbol="1155", side=Side.BUY,
                       quantity=Decimal("3000"), price=Decimal("10"), fees=Decimal("15"),
                       tax=Decimal("0"), trade_date=date(2026, 2, 20))

    # --- a TW cash dividend (folds into adjusted cost) ---
    insert_dividend(conn, account_id="tw_broker", symbol="0056", div_date=date(2026, 4, 20),
                    div_type="CASH", gross=Decimal("6000"), withholding=Decimal("0"),
                    net=Decimal("6000"))

    # --- funding FX conversions ---
    insert_fx_conversion(conn, account_id="schwab", date=date(2026, 1, 12),
                         from_ccy=Currency.TWD, from_amount=Decimal("220000"),
                         to_ccy=Currency.USD, to_amount=Decimal("6875"))
    insert_fx_conversion(conn, account_id="moomoo_my_us", date=date(2026, 3, 28),
                         from_ccy=Currency.MYR, from_amount=Decimal("11000"),
                         to_ccy=Currency.USD, to_amount=Decimal("2400"))

    # --- current prices ---
    upsert_prices(conn, [
        PriceRow(instrument="2330", market=Market.TW, as_of=_TODAY,
                 close=Decimal("2500"), source="demo"),
        PriceRow(instrument="0056", market=Market.TW, as_of=_TODAY,
                 close=Decimal("41.5"), source="demo"),
        PriceRow(instrument="AAPL", market=Market.US, as_of=_TODAY,
                 close=Decimal("294"), source="demo"),
        PriceRow(instrument="NVDA", market=Market.US, as_of=_TODAY,
                 close=Decimal("165"), source="demo"),
        PriceRow(instrument="1155", market=Market.MY, as_of=_TODAY,
                 close=Decimal("10.78"), source="demo"),
    ], fetched_at=_NOW)

    # --- FX rates (reporting blend) ---
    upsert_fx(conn, [
        FxRow(base=Currency.USD, quote=Currency.TWD, as_of=_TODAY,
              rate=Decimal("32.5"), source="demo"),
        FxRow(base=Currency.USD, quote=Currency.MYR, as_of=_TODAY,
              rate=Decimal("4.45"), source="demo"),
        FxRow(base=Currency.MYR, quote=Currency.TWD, as_of=_TODAY,
              rate=Decimal("7.3"), source="demo"),
    ], fetched_at=_NOW)

    conn.commit()
    print("demo seed complete: 5 instruments, 6 transactions, 1 dividend, 2 FX conversions.")


def main() -> None:
    db_path = get_settings().db_path
    print(f"seeding demo DB at: {db_path}")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        seed(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
