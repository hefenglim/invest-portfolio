"""A synthetic option ledger for the W5 web e2e flows (options spec §7, §8, §10).

Built on the golden ledger (`tests/conftest.py::_seed_golden`) so every stock surface keeps its
known figures, plus five AAPL contracts in the Schwab account that exercise every 選擇權
section state the dashboard renders:

* ``OPEN_PUT``  — a sold put, 2 口, open, expiring ~70 days ahead (履約義務, 賣方);
* ``OPEN_CALL`` — a bought call, 1 口, open, expiring ~30 days ahead (買方);
* ``PAST_PUT``  — a sold put, 1 口, whose expiry passed 5 days ago and has no close row yet
  (``past_expiry`` → 「已過到期日・待登錄」 + 「登錄到期作廢」);
* ``CLOSED_PUT`` — sold then bought back (realized via 買進平倉);
* ``EXPIRED_CALL`` — bought then expired worthless (realized via 到期作廢).

Expiries are computed from the SERVER's clock (the flow server runs on the real wall clock),
never fixed, so the days-to-expiry ordering holds on any day the suite runs. No expiry and no
row is ever dated on the DEF-056 sweep's seeded day (2099-01-02).
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from decimal import Decimal

from portfolio_dash.data_ingestion.register import register_option_contract
from portfolio_dash.data_ingestion.store import insert_transaction
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.options import CloseReason, OptionTerms, option_symbol
from tests.conftest import _seed_golden

D = Decimal
_SWEEP_DAY = date(2099, 1, 2)   # tests/e2e/test_def056_r5_future_row_sweep_flow.py::_AHEAD


def _contract(days_ahead: int, strike: str, right: str) -> str:
    expiry = date.today() + timedelta(days=days_ahead)
    assert expiry != _SWEEP_DAY
    return option_symbol(OptionTerms(underlying="AAPL", expiry=expiry, strike=D(strike),
                                     right="C" if right == "C" else "P"))


OPEN_PUT = _contract(70, "110", "P")
OPEN_CALL = _contract(30, "130", "C")
PAST_PUT = _contract(-5, "100", "P")
CLOSED_PUT = _contract(-40, "105", "P")
EXPIRED_CALL = _contract(-20, "140", "C")


def _trade(conn: sqlite3.Connection, symbol: str, side: Side, qty: str, price: str,
           on: date, *, fee: str = "0.65", short: bool = False,
           close_reason: CloseReason | None = None) -> None:
    insert_transaction(conn, account_id="schwab", symbol=symbol, side=side,
                       quantity=D(qty), price=D(price), fees=D(fee), tax=D("0"),
                       trade_date=on, short_sale=short, close_reason=close_reason)


def seed_option_ledger(conn: sqlite3.Connection) -> None:
    """The golden ledger + the five contracts above (all rows dated on or before today)."""
    _seed_golden(conn)
    today = date.today()
    for sym in (OPEN_PUT, OPEN_CALL, PAST_PUT, CLOSED_PUT, EXPIRED_CALL):
        register_option_contract(conn, sym)
    _trade(conn, OPEN_PUT, Side.SELL, "200", "3.20", today - timedelta(days=10),
           fee="1.30", short=True)
    _trade(conn, OPEN_CALL, Side.BUY, "100", "4.00", today - timedelta(days=8))
    _trade(conn, PAST_PUT, Side.SELL, "100", "1.50", today - timedelta(days=30), short=True)
    _trade(conn, CLOSED_PUT, Side.SELL, "100", "2.00", today - timedelta(days=60), short=True)
    _trade(conn, CLOSED_PUT, Side.BUY, "100", "0.50", today - timedelta(days=45))
    _trade(conn, EXPIRED_CALL, Side.BUY, "100", "1.00", today - timedelta(days=50))
    _trade(conn, EXPIRED_CALL, Side.SELL, "100", "0", today - timedelta(days=20), fee="0",
           close_reason="expired")
    conn.commit()
