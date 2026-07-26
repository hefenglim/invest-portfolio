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
    insert_cash_movement,
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


def _month_starts(first: date, last: date) -> list[date]:
    """The 1st of every month from *first*'s month through *last*, plus *last* itself."""
    out: list[date] = []
    y, m = first.year, first.month
    while (y, m) <= (last.year, last.month):
        out.append(date(y, m, 1))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    if out and out[-1] != last:
        out.append(last)
    return out


# Monthly close path per symbol: (market, currency, [closes aligned to _month_starts]).
# Fictional but monotone-ish, ending at the current price seeded below.
_HISTORY: dict[str, tuple[Market, Currency, list[str]]] = {
    "2330": (Market.TW, Currency.TWD,
             ["590", "610", "700", "820", "1100", "1650", "2100", "2500"]),
    "0056": (Market.TW, Currency.TWD,
             ["37.6", "38.0", "38.4", "39.1", "39.8", "40.4", "41.0", "41.5"]),
    "AAPL": (Market.US, Currency.USD,
             ["220", "228", "236", "245", "258", "270", "284", "294"]),
    "NVDA": (Market.US, Currency.USD,
             ["132", "136", "140", "146", "151", "157", "161", "165"]),
    "1155": (Market.MY, Currency.MYR,
             ["9.80", "9.95", "10.10", "10.25", "10.40", "10.55", "10.68", "10.78"]),
}

# Monthly FX path per pair, same alignment; the last point equals the spot seeded below.
_FX_HISTORY: dict[tuple[Currency, Currency], list[str]] = {
    (Currency.USD, Currency.TWD): ["32.0", "32.1", "32.2", "32.3", "32.4", "32.5",
                                   "32.5", "32.5"],
    (Currency.USD, Currency.MYR): ["4.60", "4.58", "4.56", "4.53", "4.50", "4.48",
                                   "4.46", "4.45"],
    (Currency.MYR, Currency.TWD): ["6.96", "7.01", "7.06", "7.13", "7.20", "7.26",
                                   "7.29", "7.30"],
}


def _seed_history(conn: sqlite3.Connection) -> None:
    """Monthly price + FX series from the first ledger flow to today (audit L6)."""
    days = _month_starts(date(2026, 1, 1), _TODAY)
    price_rows = []
    for symbol, (market, _ccy, closes) in _HISTORY.items():
        for i, day in enumerate(days):
            close = closes[min(i, len(closes) - 1)]
            price_rows.append(PriceRow(instrument=symbol, market=market, as_of=day,
                                       close=Decimal(close), source="demo"))
    upsert_prices(conn, price_rows, fetched_at=_NOW)

    fx_rows = []
    for (base, quote), rates in _FX_HISTORY.items():
        for i, day in enumerate(days):
            fx_rows.append(FxRow(base=base, quote=quote, as_of=day,
                                 rate=Decimal(rates[min(i, len(rates) - 1)]), source="demo"))
    upsert_fx(conn, fx_rows, fetched_at=_NOW)


def seed(conn: sqlite3.Connection) -> None:
    # Idempotent table setup (safe whether the app has booted this DB yet or not).
    bootstrap_db(conn)
    create_pricing_tables(conn)
    seed_accounts(conn)
    if _already_seeded(conn):
        print("demo DB already has a transaction ledger — skipping (idempotent).")
        return

    # --- instruments (fictional but plausible; names tagged DEMO) ---
    # Sectors use the canonical GICS vocabulary (R6, 2026-07-19): Semiconductors + Tech both
    # fold into Information Technology; Banking → Financials; ETF stays its own bucket.
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Information Technology", name="台積電 (DEMO)",
                                       board="TWSE"))
    upsert_instrument(conn, Instrument(symbol="0056", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="ETF", name="元大高股息 (DEMO)", board="TWSE"))
    upsert_instrument(conn, Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
                                       sector="Information Technology", name="Apple (DEMO)"))
    upsert_instrument(conn, Instrument(symbol="NVDA", market=Market.US, quote_ccy=Currency.USD,
                                       sector="Information Technology", name="NVIDIA (DEMO)"))
    upsert_instrument(conn, Instrument(symbol="1155", market=Market.MY, quote_ccy=Currency.MYR,
                                       sector="Financials", name="Maybank (DEMO)"))

    # --- funding deposits (audit L6, 2026-07-26) ---
    # Without these every pool on the demo read NEGATIVE: the seed booked trades and FX legs
    # but never the money that paid for them, so 資金管理 showed five overdrawn accounts AND
    # the FX card marked a negative USD cash balance to spot (換匯損益 is computed on
    # stock value + cash — see forex/pools.py). Each deposit lands before the first flow it
    # funds and is sized to leave a small positive residue, as a real account would.
    insert_cash_movement(conn, account_id="tw_broker", move_date=date(2026, 1, 2),
                         kind="DEPOSIT", ccy=Currency.TWD, amount=Decimal("1600000"),
                         note="期初匯入 (DEMO)")
    insert_cash_movement(conn, account_id="schwab", move_date=date(2026, 1, 2),
                         kind="DEPOSIT", ccy=Currency.TWD, amount=Decimal("350000"),
                         note="期初匯入 (DEMO)")
    insert_cash_movement(conn, account_id="moomoo_my", move_date=date(2026, 1, 2),
                         kind="DEPOSIT", ccy=Currency.MYR, amount=Decimal("50000"),
                         note="期初匯入 (DEMO)")

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
    insert_transaction(conn, account_id="moomoo_my", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("10"), price=Decimal("240"), fees=Decimal("1"),
                       tax=Decimal("0"), trade_date=date(2026, 4, 1))
    insert_transaction(conn, account_id="moomoo_my", symbol="1155", side=Side.BUY,
                       quantity=Decimal("3000"), price=Decimal("10"), fees=Decimal("15"),
                       tax=Decimal("0"), trade_date=date(2026, 2, 20))

    # --- a TW cash dividend (folds into adjusted cost) ---
    insert_dividend(conn, account_id="tw_broker", symbol="0056", div_date=date(2026, 4, 20),
                    div_type="CASH", gross=Decimal("6000"), withholding=Decimal("0"),
                    net=Decimal("6000"))

    # --- funding FX conversions ---
    # Sized so each USD pool stays POSITIVE after the USD buys it funds (audit L6): schwab
    # buys 9,550 USD of stock, moomoo_my 2,401. The implied rates are unchanged for schwab
    # (320,000/10,000 = 32.0, as before) so the FX attribution keeps the same cost basis.
    insert_fx_conversion(conn, account_id="schwab", date=date(2026, 1, 12),
                         from_ccy=Currency.TWD, from_amount=Decimal("320000"),
                         to_ccy=Currency.USD, to_amount=Decimal("10000"))
    insert_fx_conversion(conn, account_id="moomoo_my", date=date(2026, 3, 28),
                         from_ccy=Currency.MYR, from_amount=Decimal("11500"),
                         to_ccy=Currency.USD, to_amount=Decimal("2500"))

    # --- price + FX HISTORY (audit L6, 2026-07-26) ---
    # The seed used to store ONE price row and ONE FX row, both dated today. Two flagship
    # surfaces died on that: XIRR needs a trade-date rate for every flow
    # (`no FX rate stored on or before 2026-01-15 for USD/TWD`) and the 總市值 trend needs a
    # dated series (`missing FX history for a ledger flow date`) — so the public demo showed
    # an empty XIRR card and an empty chart, which are the two things it exists to show.
    # A monthly series from the first flow to today fixes both; the app carries values
    # forward between points, so monthly granularity renders a smooth line.
    _seed_history(conn)

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
