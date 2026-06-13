"""GET /api/symbol/{symbol}/detail — one read for the frontend's symbol-detail drawer.

Read-only and hermetic. price_history comes from STORED prices via
``pricing.store.get_price_history`` — there is NO live history backfill here (spec 01
impl-note 1 mentions a sync backfill; that is reconciled away: refresh is the
scheduler's job, and a synchronous network fetch would break the read-only principle
and test hermeticity). ``partial`` is therefore always False in v1; ``available`` is
False with a ``note`` when no points are stored.

The router is thin: it loads ledgers, calls the existing calc core (``build_book``),
and serializes. It computes no numbers of record. ``cost_basis`` binds to the account
holding the most shares of the symbol (Q1); ``null`` for a non-held / watchlist symbol.
"""

import sqlite3
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query

from portfolio_dash.api.deps import get_conn, get_now
from portfolio_dash.data_ingestion.store import (
    list_dividends,
    list_instruments,
    list_opening,
    list_transactions,
)
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.portfolio.results import Holding, RealizedRow
from portfolio_dash.pricing.store import get_price_history
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.ledger import (
    Dividend,
    OpeningInventory,
    Transaction,
)

router = APIRouter()

_ZERO = Decimal("0")

# Wire-format dividend type: lowercase. STOCK -> "stock" (配股), the rest map directly.
_DIV_TYPE_WIRE = {
    DividendType.CASH: "cash",
    DividendType.STOCK: "stock",
    DividendType.DRIP: "drip",
    DividendType.NET: "net",
}


def _q1_holding(holdings: list[Holding], symbol: str) -> Holding | None:
    """The account's holding with the MOST shares of *symbol* (Q1), or None if unheld."""
    candidates = [h for h in holdings if h.symbol == symbol and h.shares > _ZERO]
    if not candidates:
        return None
    return max(candidates, key=lambda h: h.shares)


@router.get("/symbol/{symbol}/detail")
def symbol_detail(
    symbol: str,
    days: int = Query(180, ge=1, le=3650),
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    as_of = now.date()

    # Ledgers (Stored* rows -> ledger models) — same mapping as build_dashboard step 1.
    txs = [
        Transaction(account_id=s.account_id, symbol=s.symbol, side=s.side,
                    quantity=s.quantity, price=s.price, fees=s.fees, tax=s.tax,
                    trade_date=s.trade_date)
        for s in list_transactions(conn)
    ]
    divs = [
        Dividend(account_id=s.account_id, symbol=s.symbol, date=s.date,
                 type=DividendType(s.type), gross=s.gross, withholding=s.withholding,
                 net=s.net, reinvest_shares=s.reinvest_shares,
                 reinvest_price=s.reinvest_price)
        for s in list_dividends(conn)
    ]
    opening = [
        OpeningInventory(account_id=s.account_id, symbol=s.symbol, shares=s.shares,
                         original_avg_cost=s.original_avg_cost,
                         original_cost_total=s.original_cost_total,
                         build_date=s.build_date)
        for s in list_opening(conn)
    ]
    instruments = {i.symbol: i for i in list_instruments(conn)}

    book = build_book(txs, divs, opening, instruments)

    # cost_basis -> Q1 most-shares account; null for an unheld / watchlist symbol.
    held = _q1_holding(book.holdings, symbol)
    cost_basis: dict[str, str] | None = None
    if held is not None:
        cost_basis = {
            "account_id": held.account_id,
            "original_avg": str(held.original_avg),
            "adjusted_avg": str(held.adjusted_avg),
        }

    # price_history — STORED prices over [as_of - days, as_of] (read-only; no backfill).
    start = as_of.fromordinal(as_of.toordinal() - days)
    history = get_price_history(conn, symbol, start, as_of)
    if history:
        last = history[-1]
        price_history: dict[str, Any] = {
            "available": True,
            "points": [{"date": p.as_of.isoformat(), "close": str(p.value)}
                       for p in history],
            "last_date": last.as_of.isoformat(),
            "stale": last.stale,
            "partial": False,
            "note": None,
        }
    else:
        price_history = {
            "available": False,
            "points": [],
            "last_date": None,
            "stale": True,
            "partial": False,
            "note": f"no stored price history for {symbol}",
        }

    # dividend_events — all ledger dividends for this symbol; lowercase type, UPPER ccy.
    sym_divs = list_dividends(conn, symbol=symbol)
    inst = instruments.get(symbol)
    ccy = inst.quote_ccy.value if inst is not None else None
    dividend_events = [
        {
            "date": d.date.isoformat(),
            "type": _DIV_TYPE_WIRE[DividendType(d.type)],
            "gross": str(d.gross),
            "net": str(d.net),
            "reinvest_shares": str(d.reinvest_shares) if d.reinvest_shares is not None else None,
            "reinvest_price": str(d.reinvest_price) if d.reinvest_price is not None else None,
            "ccy": ccy,
        }
        for d in sym_divs
    ]

    # trade_events — opening (side "open") + transactions (buy/sell), sorted by date.
    events: list[tuple[Any, ...]] = []
    for o in opening:
        if o.symbol == symbol:
            events.append((o.build_date, 0, {
                "date": o.build_date.isoformat(), "side": "open",
                "shares": str(o.shares), "price": str(o.original_avg_cost)}))
    for tx in txs:
        if tx.symbol == symbol:
            side = "buy" if tx.side is Side.BUY else "sell"
            order = 1 if tx.side is Side.BUY else 2
            events.append((tx.trade_date, order, {
                "date": tx.trade_date.isoformat(), "side": side,
                "shares": str(tx.quantity), "price": str(tx.price)}))
    events.sort(key=lambda e: (e[0], e[1]))
    trade_events = [e[2] for e in events]

    # realized_rows — dashboard realized.rows shape, filtered to this symbol.
    realized_rows = [_realized_wire(r) for r in book.realized.rows if r.symbol == symbol]

    return {
        "symbol": symbol,
        "as_of": as_of.isoformat(),
        "price_history": price_history,
        "cost_basis": cost_basis,
        "dividend_events": dividend_events,
        "trade_events": trade_events,
        "realized_rows": realized_rows,
    }


def _realized_wire(r: RealizedRow) -> dict[str, str]:
    """Serialize a RealizedRow to the dashboard realized.rows wire shape."""
    return {
        "account_id": r.account_id,
        "symbol": r.symbol,
        "quote_ccy": r.quote_ccy.value,
        "shares_sold": str(r.shares_sold),
        "proceeds_net": str(r.proceeds_net),
        "original_cost_removed": str(r.original_cost_removed),
        "adjusted_cost_removed": str(r.adjusted_cost_removed),
        "realized": str(r.realized),
    }
