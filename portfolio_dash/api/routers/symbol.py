"""GET /api/symbol/{symbol}/detail — one read for the frontend's symbol-detail drawer.

Read-only and hermetic. price_history comes from STORED prices via
``pricing.store.get_price_history`` — there is NO live history backfill here (spec 01
impl-note 1 mentions a sync backfill; that is reconciled away: refresh is the
scheduler's job, and a synchronous network fetch would break the read-only principle
and test hermeticity). ``partial`` is therefore always False in v1; ``available`` is
False with a ``note`` when no points are stored.

The router is thin: it calls the SAME calc core the dashboard uses (``build_dashboard``)
and serializes. It computes no NEW numbers of record — the per-account holding figures it
serializes are the very rows ``GET /api/dashboard`` returns, and the cross-account
``position`` aggregate is a plain Decimal re-sum of those rows (all holdings of one symbol
share a quote currency), never a fresh money formula. Re-using ``build_dashboard`` is the
"one authoritative definition" fix (round-8.1 Wave A, Fable F1): the drawer's 部位摘要 and
the dashboard's holding row can never diverge because they come from the same function.

New in round-8.1 Wave A:
  · ``position`` — the cross-account aggregate position summary (server-computed Decimal),
    so a symbol held in >1 account shows the TOTAL, not one account's slice (owner #2c).
  · ``position_accounts`` — the per-account breakdown behind that aggregate (owner #2c).
  · ``activity`` — a UNIFIED, account-tagged, chronological list of EVERY share-affecting
    event (opening / buy / sell / DRIP+配股 reinvest), so 交易明細 reconciles with 部位摘要
    by construction (owner #2a). ``trade_events`` is retained for the price-chart markers.
  · ``activity_reconcile`` — the 期初＋買−賣(＋配股/DRIP)＝部位摘要 share identity, computed
    server-side (total + per-account) so the drawer footer never sums shares in JS.

``cost_basis`` binds to the account holding the most shares of the symbol (Q1); ``null`` for
a non-held / watchlist symbol.
"""

import sqlite3
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query

from portfolio_dash.api.deps import get_conn, get_now, get_reporting
from portfolio_dash.data_ingestion.store import (
    list_accounts,
    list_dividends,
    list_instruments,
    list_opening,
    list_transactions,
)
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.portfolio.dashboard_models import HoldingRow
from portfolio_dash.portfolio.results import RealizedRow
from portfolio_dash.pricing.store import get_price_history
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.ledger import OpeningInventory, Transaction
from portfolio_dash.shared.wire import decimal_str

router = APIRouter()

_ZERO = Decimal("0")

# Wire-format dividend type: lowercase. STOCK -> "stock" (配股), the rest map directly.
_DIV_TYPE_WIRE = {
    DividendType.CASH: "cash",
    DividendType.STOCK: "stock",
    DividendType.DRIP: "drip",
    DividendType.NET: "net",
}

# DividendTypes that ADD SHARES (reinvest) rather than reduce cost — they contribute a row to
# the unified activity list and to the reconciliation's reinvest bucket. CASH/NET reduce the
# adjusted cost and never touch the share count (build_book), so they are NOT activity rows.
_REINVEST_TYPES = (DividendType.DRIP, DividendType.STOCK)


def _dstr_or_none(value: Decimal | None) -> str | None:
    """decimal_str, but pass ``None`` through (a missing-price / non-applicable figure)."""
    return decimal_str(value) if value is not None else None


def _sum(values: list[Decimal]) -> Decimal:
    """Decimal sum with a Decimal zero seed (keeps mypy + exactness; never float)."""
    total = _ZERO
    for v in values:
        total += v
    return total


def _account_wire(h: HoldingRow) -> dict[str, Any]:
    """One per-account holding, serialized to the drawer's position-breakdown wire shape.

    Every figure is the authoritative ``HoldingRow`` value from ``build_dashboard`` — the
    SAME row ``GET /api/dashboard`` serializes — passed straight through as a Decimal string.
    """
    return {
        "account_id": h.account_id,
        "account": h.account_name,
        "symbol": h.symbol,
        "shares": decimal_str(h.shares),
        "original_avg": decimal_str(h.original_avg),
        "adjusted_avg": decimal_str(h.adjusted_avg),
        "original_cost_total": decimal_str(h.original_cost_total),
        "adjusted_cost_total": decimal_str(h.adjusted_cost_total),
        "dividend_portion": decimal_str(h.dividend_portion),
        "payback_ratio": decimal_str(h.payback_ratio),
        "market_price": _dstr_or_none(h.market_price),
        "market_value": _dstr_or_none(h.market_value),
        "unrealized_pnl": _dstr_or_none(h.unrealized_pnl),
        "capital_gain": _dstr_or_none(h.capital_gain),
        "weight": _dstr_or_none(h.weight),
        "price_stale": h.price_stale,
        "price_as_of": h.price_as_of.isoformat() if h.price_as_of is not None else None,
        "quote_ccy": h.quote_ccy.value,
        "oversold": h.oversold,
    }


def _aggregate_position(
    rows: list[HoldingRow], inst: Instrument | None
) -> dict[str, Any] | None:
    """Cross-account aggregate of a symbol's holdings (owner #2c) — server-side Decimal only.

    All holdings of one symbol share the same quote currency, so the aggregate is a plain
    Decimal sum (money) + a shares-weighted average cost (``total_cost / total_shares``,
    computed on read per domain-ledger.md — never a stored rounded average). The frontend
    prints these strings; it does NOT sum market value / unrealized / cost across accounts
    (that would breach the "frontend never computes money" invariant — which is exactly why
    this aggregation lives here). ``None`` when the symbol is not held.

    Missing-price degradation mirrors the dashboard: a 缺價 holding carries ``None`` market
    fields and is excluded from the value sums; because price is per-symbol, either every
    holding of the symbol is valued or none is, so no partial-blend can occur.
    """
    if not rows:
        return None
    quote_ccy = rows[0].quote_ccy
    total_shares = _sum([h.shares for h in rows])
    original_total = _sum([h.original_cost_total for h in rows])
    adjusted_total = _sum([h.adjusted_cost_total for h in rows])
    dividend_portion = _sum([h.dividend_portion for h in rows])

    mv = [h.market_value for h in rows if h.market_value is not None]
    ur = [h.unrealized_pnl for h in rows if h.unrealized_pnl is not None]
    cg = [h.capital_gain for h in rows if h.capital_gain is not None]
    wt = [h.weight for h in rows if h.weight is not None]

    # market_price / staleness are per-symbol identical; take them from a priced row.
    src = next((h for h in rows if h.market_price is not None), rows[0])

    original_avg = original_total / total_shares if total_shares != _ZERO else _ZERO
    adjusted_avg = adjusted_total / total_shares if total_shares != _ZERO else _ZERO
    payback = dividend_portion / original_total if original_total != _ZERO else _ZERO

    return {
        "account_count": len(rows),
        "symbol": rows[0].symbol,
        "quote_ccy": quote_ccy.value,
        "name": inst.name if inst is not None else None,
        "market": inst.market.value if inst is not None else None,
        "board": inst.board if inst is not None else "",
        "shares": decimal_str(total_shares),
        "original_avg": decimal_str(original_avg),
        "adjusted_avg": decimal_str(adjusted_avg),
        "original_cost_total": decimal_str(original_total),
        "adjusted_cost_total": decimal_str(adjusted_total),
        "dividend_portion": decimal_str(dividend_portion),
        "payback_ratio": decimal_str(payback),
        "market_price": _dstr_or_none(src.market_price),
        "market_value": decimal_str(_sum(mv)) if mv else None,
        "unrealized_pnl": decimal_str(_sum(ur)) if ur else None,
        "capital_gain": decimal_str(_sum(cg)) if cg else None,
        # weight is a dimensionless ratio; summing the per-account weights server-side
        # (Σ mv_i/total) gives the aggregate share of portfolio value. Still done here, not
        # in JS, to keep ALL of 部位摘要's numbers server-authoritative.
        "weight": decimal_str(_sum(wt)) if wt else None,
        "price_stale": src.price_stale,
        "price_as_of": src.price_as_of.isoformat() if src.price_as_of is not None else None,
        "oversold": any(h.oversold for h in rows),
    }


def _reconcile(
    holdings: list[HoldingRow],
    opening: list[OpeningInventory],
    txs: list[Transaction],
    divs: list[Any],
) -> dict[str, Any]:
    """The 期初＋買−賣(＋配股/DRIP)＝部位摘要 share identity, computed server-side.

    ``book_shares`` is the authoritative holding share count (``build_dashboard``); the other
    buckets are raw-ledger share sums. ``balances`` is True when the ledger flow reproduces the
    book — the visible proof 交易明細 reconciles with 部位摘要 (owner #2a, Fable F1). Shares are
    quantities, not money, but the totals are still computed here so the drawer footer renders
    server values under ONE definition rather than re-summing rows in the browser.
    """
    opening_sh = _sum([o.shares for o in opening])
    buy_sh = _sum([t.quantity for t in txs if t.side is Side.BUY])
    sell_sh = _sum([t.quantity for t in txs if t.side is Side.SELL])
    reinvest_sh = _sum([
        d.reinvest_shares
        for d in divs
        if DividendType(d.type) in _REINVEST_TYPES and d.reinvest_shares is not None
    ])
    book_sh = _sum([h.shares for h in holdings])
    net = opening_sh + buy_sh - sell_sh + reinvest_sh
    return {
        "opening_shares": decimal_str(opening_sh),
        "buy_shares": decimal_str(buy_sh),
        "sell_shares": decimal_str(sell_sh),
        "reinvest_shares": decimal_str(reinvest_sh),
        "net_shares": decimal_str(net),
        "book_shares": decimal_str(book_sh),
        "balances": net == book_sh,
    }


@router.get("/symbol/{symbol}/detail")
def symbol_detail(
    symbol: str,
    days: int = Query(180, ge=1, le=3650),
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
    reporting: Currency = Depends(get_reporting),
) -> dict[str, Any]:
    as_of = now.date()

    instruments = {i.symbol: i for i in list_instruments(conn)}
    inst = instruments.get(symbol)
    ccy = inst.quote_ccy.value if inst is not None else None
    acct_names = {a.account_id: a.name for a in list_accounts(conn)}

    # Authoritative valued book — the SAME combiner GET /api/dashboard serializes, so the
    # per-account rows here are byte-identical to the dashboard's and the aggregate below is a
    # provable re-sum of them (round-8.1 Wave A: one definition, never a parallel calc).
    data = build_dashboard(conn, now=now, reporting=reporting)
    sym_holdings = sorted(
        (h for h in data.holdings if h.symbol == symbol),
        key=lambda h: h.shares,
        reverse=True,  # primary (most-shares) account first, for the drawer's default 試算
    )

    # cost_basis -> Q1 most-shares account; null for an unheld / watchlist symbol.
    held = [h for h in sym_holdings if h.shares > _ZERO]
    cost_basis: dict[str, str] | None = None
    if held:
        q1 = max(held, key=lambda h: h.shares)
        cost_basis = {
            "account_id": q1.account_id,
            "original_avg": decimal_str(q1.original_avg),
            "adjusted_avg": decimal_str(q1.adjusted_avg),
        }

    # position + per-account breakdown (owner #2c): the drawer's 部位摘要 primary aggregate.
    position = _aggregate_position(sym_holdings, inst)
    position_accounts = [_account_wire(h) for h in sym_holdings]

    # This symbol's ledgers (typed models) — for the unified activity + trade_events.
    sym_txs = [
        Transaction(account_id=s.account_id, symbol=s.symbol, side=s.side,
                    quantity=s.quantity, price=s.price, fees=s.fees, tax=s.tax,
                    trade_date=s.trade_date)
        for s in list_transactions(conn) if s.symbol == symbol
    ]
    sym_opening = [
        OpeningInventory(account_id=s.account_id, symbol=s.symbol, shares=s.shares,
                         original_cost_total=s.original_cost_total,
                         build_date=s.build_date)
        for s in list_opening(conn) if s.symbol == symbol
    ]
    sym_divs = list_dividends(conn, symbol=symbol)

    # price_history — STORED prices over [as_of - days, as_of] (read-only; no backfill).
    start = as_of.fromordinal(as_of.toordinal() - days)
    history = get_price_history(conn, symbol, start, as_of)
    if history:
        last = history[-1]
        price_history: dict[str, Any] = {
            "available": True,
            "points": [{"date": p.as_of.isoformat(), "close": decimal_str(p.value)}
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
    dividend_events = [
        {
            "date": d.date.isoformat(),
            "type": _DIV_TYPE_WIRE[DividendType(d.type)],
            "gross": decimal_str(d.gross),
            "net": decimal_str(d.net),
            "reinvest_shares": (
                decimal_str(d.reinvest_shares) if d.reinvest_shares is not None else None
            ),
            "reinvest_price": (
                decimal_str(d.reinvest_price) if d.reinvest_price is not None else None
            ),
            "ccy": ccy,
        }
        for d in sym_divs
    ]

    # trade_events — opening (side "open") + transactions (buy/sell), sorted by date. Retained
    # for the price-chart markers (buy/sell triangles); the richer 交易明細 table now reads the
    # unified ``activity`` list below instead.
    tev: list[tuple[Any, int, dict[str, Any]]] = []
    for o in sym_opening:
        tev.append((o.build_date, 0, {
            "date": o.build_date.isoformat(), "side": "open",
            "shares": decimal_str(o.shares),
            "price": decimal_str(o.original_avg)}))  # computed on read (total/shares) — A6
    for tx in sym_txs:
        side = "buy" if tx.side is Side.BUY else "sell"
        order = 1 if tx.side is Side.BUY else 2
        tev.append((tx.trade_date, order, {
            "date": tx.trade_date.isoformat(), "side": side,
            "shares": decimal_str(tx.quantity), "price": decimal_str(tx.price)}))
    tev.sort(key=lambda e: (e[0], e[1]))
    trade_events = [e[2] for e in tev]

    # activity — the UNIFIED, account-tagged, share-affecting event list (owner #2a). Signed
    # ``total`` is the cash flow (buy −, sell +, opening −cost, reinvest 0-cost). Openings carry
    # no fee/tax and use original_avg (total/shares) as the price; reinvest rows carry the DRIP
    # reinvest price (配股 has none -> null). This is the ONE list 交易明細 renders, so its share
    # sum reconciles with 部位摘要 by construction.
    aev: list[tuple[Any, int, dict[str, Any]]] = []
    for o in sym_opening:
        aev.append((o.build_date, 0, {
            "date": o.build_date.isoformat(),
            "account_id": o.account_id, "account": acct_names.get(o.account_id, o.account_id),
            "side": "open", "shares": decimal_str(o.shares),
            "price": decimal_str(o.original_avg), "fee": None, "tax": None,
            "total": decimal_str(-o.original_cost_total), "ccy": ccy}))
    for tx in sym_txs:
        if tx.side is Side.BUY:
            total = -(tx.quantity * tx.price + tx.fees + tx.tax)
            aev.append((tx.trade_date, 1, {
                "date": tx.trade_date.isoformat(),
                "account_id": tx.account_id,
                "account": acct_names.get(tx.account_id, tx.account_id),
                "side": "buy", "shares": decimal_str(tx.quantity),
                "price": decimal_str(tx.price), "fee": decimal_str(tx.fees),
                "tax": decimal_str(tx.tax), "total": decimal_str(total), "ccy": ccy}))
        else:
            total = tx.quantity * tx.price - tx.fees - tx.tax
            aev.append((tx.trade_date, 2, {
                "date": tx.trade_date.isoformat(),
                "account_id": tx.account_id,
                "account": acct_names.get(tx.account_id, tx.account_id),
                "side": "sell", "shares": decimal_str(tx.quantity),
                "price": decimal_str(tx.price), "fee": decimal_str(tx.fees),
                "tax": decimal_str(tx.tax), "total": decimal_str(total), "ccy": ccy}))
    for d in sym_divs:
        dt = DividendType(d.type)
        if dt in _REINVEST_TYPES and d.reinvest_shares is not None:
            aev.append((d.date, 3, {
                "date": d.date.isoformat(),
                "account_id": d.account_id,
                "account": acct_names.get(d.account_id, d.account_id),
                "side": "drip" if dt is DividendType.DRIP else "stock",
                "shares": decimal_str(d.reinvest_shares),
                "price": (decimal_str(d.reinvest_price)
                          if d.reinvest_price is not None else None),
                "fee": None, "tax": None, "total": decimal_str(_ZERO), "ccy": ccy}))
    aev.sort(key=lambda e: (e[0], e[1]))
    activity = [e[2] for e in aev]

    # activity_reconcile — total + per-account share identities (owner #2a). Per-account so the
    # drawer's account filter can show a matching footer without any client share arithmetic.
    acct_ids = sorted({str(ev["account_id"]) for ev in activity}
                      | {h.account_id for h in sym_holdings})
    activity_reconcile = {
        "total": _reconcile(sym_holdings, sym_opening, sym_txs, sym_divs),
        "by_account": {
            aid: _reconcile(
                [h for h in sym_holdings if h.account_id == aid],
                [o for o in sym_opening if o.account_id == aid],
                [t for t in sym_txs if t.account_id == aid],
                [d for d in sym_divs if d.account_id == aid],
            )
            for aid in acct_ids
        },
    }

    # realized_rows — dashboard realized.rows shape, filtered to this symbol.
    realized_rows = [_realized_wire(r) for r in data.realized.rows if r.symbol == symbol]

    return {
        "symbol": symbol,
        "as_of": as_of.isoformat(),
        # Registry enrichment (FU-D24): name/market from the instruments registry so the
        # drawer can title itself even for a non-held / watchlist symbol. None when the
        # symbol is unregistered.
        "name": inst.name if inst is not None else None,
        "market": inst.market.value if inst is not None else None,
        "price_history": price_history,
        "cost_basis": cost_basis,
        "position": position,
        "position_accounts": position_accounts,
        "dividend_events": dividend_events,
        "trade_events": trade_events,
        "activity": activity,
        "activity_reconcile": activity_reconcile,
        "realized_rows": realized_rows,
    }


def _realized_wire(r: RealizedRow) -> dict[str, str]:
    """Serialize a RealizedRow to the dashboard realized.rows wire shape."""
    return {
        "account_id": r.account_id,
        "symbol": r.symbol,
        "quote_ccy": r.quote_ccy.value,
        "sell_date": r.sell_date.isoformat(),
        "shares_sold": decimal_str(r.shares_sold),
        "proceeds_net": decimal_str(r.proceeds_net),
        "original_cost_removed": decimal_str(r.original_cost_removed),
        "adjusted_cost_removed": decimal_str(r.adjusted_cost_removed),
        "realized": decimal_str(r.realized),
    }
