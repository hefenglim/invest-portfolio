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

New in W5 (corporate actions, spec §6.3):
  · the identity gains a ``＋公司行動`` term — ``corporate_delta_shares`` — WITHOUT which
    every symbol carrying an action reports ⚠ 對帳不一致 (measured on the demo corpus
    2026-08-11: ORBT −60/−40, VRTA −40, VRTB +40, KEMG −1,000).
  · ``activity`` gains the corporate-action rows, so the term is traceable to the events
    that produced it.
  · ``action_issues`` — the three distinct "this position is not trustworthy" channels a red
    footer needs beside it (F-17): the replay's refused rows, the depth-capped walks (D31)
    and the negative-side skips (D33).

``cost_basis`` binds to the account holding the most shares of the symbol (Q1); ``null`` for
a non-held / watchlist symbol.
"""

import sqlite3
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query

from portfolio_dash.api.deps import get_conn, get_now, get_reporting
from portfolio_dash.data_ingestion.holdings import (
    _shares_until as shares_naive,  # §6.3's second term, by NAME — see _corporate_delta
)
from portfolio_dash.data_ingestion.holdings import (
    current_shares,
    load_action_index,
)
from portfolio_dash.data_ingestion.store import (
    StoredCorporateAction,
    list_accounts,
    list_corporate_actions,
    list_dividends,
    list_instruments,
    list_opening,
    list_transactions,
)
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.portfolio.dashboard_models import HoldingRow
from portfolio_dash.portfolio.price_basis import series_in
from portfolio_dash.portfolio.results import RealizedRow, UnappliedAction
from portfolio_dash.pricing.store import get_price_history
from portfolio_dash.shared.corporate_actions import ActionIndex
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.ledger_events import EventPriority
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.ledger import OpeningInventory, Transaction
from portfolio_dash.shared.wire import decimal_str

router = APIRouter()

_ZERO = Decimal("0")

# Share-reconciliation tolerance (owner ruling 2026-08-06): ignore anything past the 6th
# decimal place of a SHARE COUNT. DRIP/STOCK reinvest shares are stored as `net / price`, a
# non-terminating quotient carrying ~28 significant digits, and the two sides of the identity
# below sum them in DIFFERENT orders — `_reconcile` adds the small values to each other first,
# `build_book` folds each one into a ~95-share running total. Decimal arithmetic at the default
# 28-digit context is not associative across that gap, so the two orders legitimately disagree
# in the last digit (measured 2026-08-05 on demo AAPL: 1E-26 shares) and an EXACT `==` reported
# 對帳不一致 on a perfectly consistent ledger.
#
# This is a DIFFERENCE test, not quantize-then-compare: truncating both sides to 6 dp keeps the
# same bug class, because two values 1E-27 apart can still straddle a 6-dp boundary and truncate
# to different results. A real reconciliation break (an unregistered symbol excluded from the
# book, a dividend skipped on an open short, a 賣超) is orders of magnitude larger than this.
_SHARE_EPS = Decimal("0.000001")

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
        "unrealized_pct": _dstr_or_none(h.unrealized_pct),
        "capital_gain": _dstr_or_none(h.capital_gain),
        "weight": _dstr_or_none(h.weight),
        "price_stale": h.price_stale,
        "price_as_of": h.price_as_of.isoformat() if h.price_as_of is not None else None,
        "quote_ccy": h.quote_ccy.value,
        "oversold": h.oversold,
        "short_open": h.short_open,
        "unbookable_dividend": h.unbookable_dividend,
        "unbookable_action": h.unbookable_action,
        # 已回本: cumulative cash dividends have fully repaid the original cost, so the
        # adjusted basis has gone <= 0 (legal per domain-ledger.md). Decided HERE with an
        # exact Decimal comparison so the UI never threshold-tests a Decimal string.
        # The `not short_open` gate is load-bearing: a short's basis is NEGATIVE by
        # construction, so the bare <= 0 test labelled every open short
        # 「配息已完全沖減成本」 without a single dividend having been paid.
        "fully_recovered": h.adjusted_cost_total <= _ZERO and not h.short_open,
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
    # abs(): same guard, same reason as unrealized_pct below — a short leg contributes a
    # NEGATIVE basis, so a signed sum can shrink or flip the denominator and print a
    # position that really returned 30% of its cost as -7.5% 回本進度 (review 2026-08-24).
    payback = dividend_portion / abs(original_total) if original_total != _ZERO else _ZERO
    # Aggregate unrealized % on the SAME basis as the per-holding figure (audit H1):
    # Σ unrealized / Σ original cost. Server-side Decimal; the drawer only prints it.
    unrealized_sum = _sum(ur) if ur else None
    # abs(): a short's basis is negative (proceeds received) and would flip the sign, showing
    # a profitable short as a loss. Same guard as the per-holding figure in dashboard.py.
    unrealized_pct = (
        unrealized_sum / abs(original_total)
        if unrealized_sum is not None and original_total != _ZERO
        else None
    )

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
        "unrealized_pnl": decimal_str(unrealized_sum) if unrealized_sum is not None else None,
        "unrealized_pct": _dstr_or_none(unrealized_pct),
        "capital_gain": decimal_str(_sum(cg)) if cg else None,
        # weight is a dimensionless ratio; summing the per-account weights server-side
        # (Σ mv_i/total) gives the aggregate share of portfolio value. Still done here, not
        # in JS, to keep ALL of 部位摘要's numbers server-authoritative.
        "weight": decimal_str(_sum(wt)) if wt else None,
        "price_stale": src.price_stale,
        "price_as_of": src.price_as_of.isoformat() if src.price_as_of is not None else None,
        "oversold": any(h.oversold for h in rows),
        "short_open": any(h.short_open for h in rows),
        "unbookable_dividend": any(h.unbookable_dividend for h in rows),
        # `any`, like the two flags above: the aggregate's shares/market value are a SUM, so
        # one account's pre-action share count contaminates the total. A per-account row can
        # still be clean — the drawer shows both, and only the aggregate is poisoned by one.
        "unbookable_action": any(h.unbookable_action for h in rows),
        # 已回本 across the aggregated position (see _account_wire).
        "fully_recovered": (adjusted_total <= _ZERO
                            and not any(h.short_open for h in rows)),
    }


def _corporate_delta(
    conn: sqlite3.Connection, index: ActionIndex, account_id: str, symbol: str
) -> Decimal:
    """§6.3's ``corporate_delta`` for one (account, symbol) — the DEFINITION, not arithmetic.

    ::

        corporate_delta(symbol, account) := shares_action_aware(...) − shares_naive(...)

    Both terms live in ``data_ingestion/holdings.py``; both are SQL-path; **neither is
    ``build_book``**. The spec spells out four reasons, and every one of them is a reason NOT
    to re-derive the number here from the ratio arithmetic (``×(ratio−1)`` on a SPLIT,
    ``+carried`` on an EXCHANGE's destination, …):

    1. **Exact by construction.** The delta is *defined* as the action-attributable component
       of the action-aware count, so no per-kind rule is restated and none can drift from the
       replay's. A hand-written ``×(ratio−1)`` is a third implementation of §4's algebra.
    2. **The identity stays a genuine cross-check** of two independent implementations — the
       SQL share path against ``build_book``. Deriving the delta from ``build_book``'s own
       output instead would make it circular: the footer would prove a number equals itself,
       and §7.3's detection-power test would be testing the test.
    3. **The transitive closure comes free.** A destination's shares reach back through
       ANOTHER symbol's entire history (and a chain of them — a de-SPAC into a ticker later
       renamed). ``symbol_detail`` loads only THIS symbol's ledgers — every one of the three
       loads in the route body filters on this symbol — so the data is simply not here; the
       walker reaches it, and this router never learns another symbol's ledger.
    4. It answers per-account and (summed) aggregated, which the footer and the per-account
       breakdown both need.

    *index* is the ONE per-request :class:`ActionIndex` (F-24 / trap #21). It is threaded
    rather than rebuilt for a second, load-bearing reason: it also carries D31's depth-cap
    sink and D33's negative-side-skip sink, which the walk MUTATES. A caller that builds its
    own throwaway index loses those records and the 待釐清 chip silently never appears.
    """
    # `shares_naive` is holdings.py's `_shares_until` under §6.3's own name for it. Imported
    # privately on purpose: the spec defines the second term AS that function ("shares_naive
    # stays exactly as it is … renaming or 'improving' it silently changes the drawer's
    # footer"), so re-summing an equivalent from this router's already-loaded ledger lists
    # would make the delta `aware − our_own_sum` instead of `aware − naive`. Those agree
    # today, and on the day they stop agreeing the second form ABSORBS the disagreement into
    # the corporate term and the footer stays green over it. This form reports it.
    return current_shares(conn, account_id, symbol, index=index) - shares_naive(
        conn, account_id, symbol, None
    )


def _action_wire(
    a: StoredCorporateAction, symbol: str, account_name: str, ccy: str | None
) -> dict[str, Any]:
    """One corporate-action row, serialized into the unified activity list's shape.

    ⚠ **``shares`` is deliberately ``None``, not the row's own share delta.** Computing it
    would mean re-deriving §4's ratio algebra per kind (``×(ratio−1)`` on a SPLIT,
    ``−pre-action shares`` on an EXCHANGE's source, ``apply_ratio(source)`` on either
    destination, ZERO on a SPINOFF's parent) against the position as it stood at the action's
    own ``(date, priority)`` cut — a THIRD implementation of the walker's ``_delta_of``,
    living in a router, and one whose only public approximation (``shares_on`` with a
    ``before`` cut) gets F-18's same-day opening wrong. Written without the parentheses on
    purpose: ``test_the_call_site_census_is_still_accurate`` greps for a CALL, and a prose
    mention that looks like one would pin a call site this module does not have. §6.0's
    "one owner per concept" is the rule and the
    same-day case is the drift it would produce. The EXACT aggregate lives in the footer's
    ``corporate_delta_shares``, sourced from the walker itself; this row's job is to name the
    event that produced it — kind, ratio, counterpart symbol, date, account.

    ``role`` is what the action did to THIS symbol, which is not a property of the row: the
    same EXCHANGE is a departure for its source and an arrival for its destination, and a
    SPINOFF's parent keeps its position entirely (§4.3 carves cost, not shares).
    """
    if a.from_symbol == a.to_symbol:
        role = "self"          # SPLIT (E20 forces to == from): re-denominates in place
    elif symbol == a.to_symbol:
        role = "destination"   # shares arrive from another symbol's position
    else:
        role = "source"        # EXCHANGE empties it; SPINOFF leaves the shares alone
    return {
        "date": a.date.isoformat(),
        "account_id": a.account_id,
        "account": account_name,
        "side": "action",
        "shares": None,
        "price": None,
        "fee": None,
        "tax": None,
        "total": decimal_str(_ZERO),  # a corporate action moves no cash
        "ccy": ccy,
        "kind": a.kind,
        "role": role,
        "from_symbol": a.from_symbol,
        "to_symbol": a.to_symbol,
        # Two terms, never a quotient: `data-and-pricing.md` forbids a rounded quotient as
        # the authority, and the drawer renders "3 股換 1 股" from the pair.
        "ratio_to": decimal_str(a.ratio_to),
        "ratio_from": decimal_str(a.ratio_from),
        "cost_carry": _dstr_or_none(a.cost_carry),
        "note": a.note,
    }


def _action_issues(
    symbol: str,
    unapplied: list[UnappliedAction],
    index: ActionIndex,
    acct_names: dict[str, str],
) -> dict[str, Any]:
    """The three DISTINCT "this position is not trustworthy" channels, for one symbol.

    §6.3's red footer only works when the mismatch comes with its cause attached, and audit
    F-17 found the cause had no way to reach the wire at all. They are three channels and not
    one because they fail in three different places and need three different sentences:

    * **``unapplied``** — ``Book.unapplied_actions``: rows the REPLAY refused. Not a
      substitute for ``HoldingRow.unbookable_action`` and not substitutable BY it — two of
      the three refusal shapes leave no surviving position to carry a flag (an EXCHANGE that
      already emptied the source, and a source that never existed), so the flag is False and
      the dashboard looks clean while an action was silently ignored.
    * **``depth_capped``** — D31: the share WALK hit ``MAX_ACTION_DEPTH`` and fell back to
      the action-unaware count. The replay may well have succeeded, so nothing else records
      it; without this the footer would go red with a corporate term of 0 and no reason.
    * **``negative_side_skipped``** — D33: the share path skipped an EXCHANGE/SPINOFF whose
      source or destination count was ``< 0`` on the action date. The skip is the correctness
      fix; this is the "and flag it" half of the same ruling.

    The last two are read off the per-request :class:`ActionIndex` the walks mutated, which is
    why this function must be called AFTER them.
    """
    def _pair(account_id: str) -> dict[str, str]:
        return {"account_id": account_id, "account": acct_names.get(account_id, account_id)}

    return {
        "unapplied": [
            {
                "account_id": u.account_id,
                "account": acct_names.get(u.account_id, u.account_id),
                "date": u.date.isoformat(),
                # StrEnum -> its value ("SPLIT"/"EXCHANGE"/"SPINOFF"); this router builds
                # plain dicts and never passes through `to_wire`, so the cast is explicit.
                "kind": str(u.kind),
                "from_symbol": u.from_symbol,
                "to_symbol": u.to_symbol,
                # The same zh sentence the strict path raises, so both paths explain the
                # refusal identically (UnappliedAction.reason).
                "reason": u.reason,
            }
            for u in unapplied
            if symbol in (u.from_symbol, u.to_symbol)
        ],
        "depth_capped": [
            _pair(acct) for acct, sym in sorted(index.depth_capped_symbols()) if sym == symbol
        ],
        "negative_side_skipped": [
            _pair(acct) for acct, sym in sorted(index.negative_side_skips()) if sym == symbol
        ],
    }


def _reconcile(
    holdings: list[HoldingRow],
    opening: list[OpeningInventory],
    txs: list[Transaction],
    divs: list[Any],
    corporate_delta: Decimal,
) -> dict[str, Any]:
    """The 期初＋買−賣(＋配股/DRIP)(＋公司行動)＝部位摘要 share identity, computed server-side.

    ``book_shares`` is the authoritative holding share count (``build_dashboard``); the other
    buckets are raw-ledger share sums. ``balances`` is True when the ledger flow reproduces the
    book to within ``_SHARE_EPS`` — the visible proof 交易明細 reconciles with 部位摘要 (owner
    #2a, Fable F1); ``diff_shares`` carries the exact signed gap either way. Shares are
    quantities, not money, but the totals are still computed here so the drawer footer renders
    server values under ONE definition rather than re-summing rows in the browser.

    *corporate_delta* (spec §6.3, W5) is the fifth term. A corporate action adds shares
    OUTSIDE the four ledger buckets — no transaction, no opening, no dividend row — so before
    this term existed every affected symbol reported ⚠ 對帳不一致 while being perfectly
    consistent. See :func:`_corporate_delta` for why it is a difference of two share paths and
    not ratio arithmetic re-derived here.

    ⚠ A **flagged** position (賣超 / 待釐清 / short-conflicted) SHOULD still fail to reconcile
    and is deliberately not special-cased: ``build_book`` skips an action on such a position
    while the SQL path applies it, and §6.3 rules that "a position whose basis was discarded
    genuinely does not reconcile — reporting ⚠ 對帳不一致 on it is the correct answer, not a
    false alarm." The drawer shows the cause beside the mismatch (``action_issues`` +
    the ``oversold`` / ``unbookable_*`` chips) instead of forcing the number green.
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
    net = opening_sh + buy_sh - sell_sh + reinvest_sh + corporate_delta
    diff = net - book_sh
    return {
        "opening_shares": decimal_str(opening_sh),
        "buy_shares": decimal_str(buy_sh),
        "sell_shares": decimal_str(sell_sh),
        "reinvest_shares": decimal_str(reinvest_sh),
        # Shown by the drawer only when non-zero, exactly like the 配股/DRIP term: a
        # 「＋公司行動 0」 in the equation explains nothing.
        "corporate_delta_shares": decimal_str(corporate_delta),
        # The WHOLE left-hand side, corporate term included — so `net − book` stays the
        # footer's single gap figure and the printed equation sums to the printed total.
        "net_shares": decimal_str(net),
        "book_shares": decimal_str(book_sh),
        # Exact on the wire (full precision, as everywhere else); the tolerance lives in the
        # FLAG, and the drawer names this figure when the flag is red — a footer that reports a
        # break without its size is unactionable.
        "diff_shares": decimal_str(diff),
        "balances": abs(diff) < _SHARE_EPS,
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
                    trade_date=s.trade_date,
                    short_sale=s.short_sale)
        for s in list_transactions(conn) if s.symbol == symbol
    ]
    sym_opening = [
        OpeningInventory(account_id=s.account_id, symbol=s.symbol, shares=s.shares,
                         original_cost_total=s.original_cost_total,
                         build_date=s.build_date)
        for s in list_opening(conn) if s.symbol == symbol
    ]
    sym_divs = list_dividends(conn, symbol=symbol)
    # ONE ActionIndex for the whole request (F-24 / D23 rule 2 / trap #21). `_reconcile` is
    # called 1 + len(acct_ids) times and each call needs the action-aware share count, so a
    # per-call index would re-read and re-group the entire action ledger every time. It is
    # ALSO the object the share walk writes its two 待釐清 sinks onto (D31 depth cap, D33
    # negative-side skip), so it must be the same instance every walk below runs through —
    # a throwaway index would lose the record and the chip would silently never appear.
    action_index = load_action_index(conn)
    # Matches EITHER end (store.list_corporate_actions): a symbol's history includes the
    # actions that CREATED it, not only those it was the source of.
    sym_actions = list_corporate_actions(conn, symbol=symbol)

    # price_history — STORED prices over [as_of - days, as_of] (read-only; no backfill).
    #
    # §5.1(d) / W6c, re-expressed into `as_of` on the SAME `action_index` built above (trap
    # #21 — never a second one). The drawer draws this line together with horizontal cost
    # mark-lines at `original_avg` / `adjusted_avg`, and those are `total / shares` over the
    # replay's ALREADY re-denominated share count: leaving the series as-traded puts the
    # pre-split part of the line a whole ratio away from its own cost line (a 20-for-1 draws
    # the cost line at 1/20 of the plotted price). The buy/sell markers are plotted at the
    # SERIES' close on their date (`closeOn`), so they follow this line automatically;
    # `trade_events.price` below stays RAW on purpose — it is the historical execution price
    # paired with the historical share count in the same tooltip ("買 100 股 @ 350"), and
    # both legs of that pair are as-traded.
    start = as_of.fromordinal(as_of.toordinal() - days)
    history = series_in(
        action_index, symbol, get_price_history(conn, symbol, start, as_of),
        valued_on=as_of,
    )
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
    # ``total`` is the cash flow (buy −, sell +, opening −cost, reinvest 0-cost, corporate
    # action 0). Openings carry no fee/tax and use original_avg (total/shares) as the price;
    # reinvest rows carry the DRIP reinvest price (配股 has none -> null). This is the ONE
    # list 交易明細 renders, so its share sum reconciles with 部位摘要 by construction.
    #
    # Sort keys are the REPLAY's own ``EventPriority`` values rather than 0/1/2/3 invented
    # here, because W5 inserts a row in the middle of that order: a corporate action is
    # effective at the START of its date — after a same-day opening (D3) and before that
    # day's trades, whose quantities are already quoted in post-action terms. Hand-numbering
    # around the new row is exactly how the list would drift from the replay it displays.
    aev: list[tuple[Any, int, dict[str, Any]]] = []
    for o in sym_opening:
        aev.append((o.build_date, int(EventPriority.OPENING), {
            "date": o.build_date.isoformat(),
            "account_id": o.account_id, "account": acct_names.get(o.account_id, o.account_id),
            "side": "open", "shares": decimal_str(o.shares),
            "price": decimal_str(o.original_avg), "fee": None, "tax": None,
            "total": decimal_str(-o.original_cost_total), "ccy": ccy}))
    for a in sym_actions:
        aev.append((a.date, int(EventPriority.CORPORATE_ACTION),
                    _action_wire(a, symbol, acct_names.get(a.account_id, a.account_id), ccy)))
    for tx in sym_txs:
        if tx.side is Side.BUY:
            total = -(tx.quantity * tx.price + tx.fees + tx.tax)
            aev.append((tx.trade_date, int(EventPriority.BUY), {
                "date": tx.trade_date.isoformat(),
                "account_id": tx.account_id,
                "account": acct_names.get(tx.account_id, tx.account_id),
                "side": "buy", "shares": decimal_str(tx.quantity),
                "price": decimal_str(tx.price), "fee": decimal_str(tx.fees),
                "tax": decimal_str(tx.tax), "total": decimal_str(total), "ccy": ccy}))
        else:
            total = tx.quantity * tx.price - tx.fees - tx.tax
            aev.append((tx.trade_date, int(EventPriority.SELL), {
                "date": tx.trade_date.isoformat(),
                "account_id": tx.account_id,
                "account": acct_names.get(tx.account_id, tx.account_id),
                "side": "sell", "shares": decimal_str(tx.quantity),
                "price": decimal_str(tx.price), "fee": decimal_str(tx.fees),
                "tax": decimal_str(tx.tax), "total": decimal_str(total), "ccy": ccy}))
    for d in sym_divs:
        dt = DividendType(d.type)
        if dt in _REINVEST_TYPES and d.reinvest_shares is not None:
            aev.append((d.date, int(EventPriority.DIVIDEND), {
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
    #
    # The account set now includes the CORPORATE-ACTION accounts (via the action rows just
    # added to `activity`), which closes audit F-45: a symbol whose whole life is two actions
    # — created by one, consumed by the next, so it owns no transaction, no opening and no
    # surviving holding — previously got `acct_ids = ∅` and an EMPTY per-account breakdown
    # while its activity list showed two events. It now gets a footer per account, and that
    # footer carries the corporate term that makes the empty position add up.
    acct_ids = sorted({str(ev["account_id"]) for ev in activity}
                      | {h.account_id for h in sym_holdings})
    # ONE walk per account, reused by the total and by that account's own footer. The total's
    # delta is the SUM of the per-account deltas rather than a separate cross-account query:
    # positions are keyed (account, symbol) and every action row binds to one account, so the
    # sum IS the aggregate — and computing it twice by two routes is how the drawer's
    # aggregate/detail pairs have drifted three times before.
    deltas = {aid: _corporate_delta(conn, action_index, aid, symbol) for aid in acct_ids}
    activity_reconcile = {
        "total": _reconcile(sym_holdings, sym_opening, sym_txs, sym_divs,
                            _sum(list(deltas.values()))),
        "by_account": {
            aid: _reconcile(
                [h for h in sym_holdings if h.account_id == aid],
                [o for o in sym_opening if o.account_id == aid],
                [t for t in sym_txs if t.account_id == aid],
                [d for d in sym_divs if d.account_id == aid],
                deltas[aid],
            )
            for aid in acct_ids
        },
    }

    # action_issues — the three channels that EXPLAIN a red footer (audit F-17). Read AFTER
    # the walks above, because two of them are sinks those walks write into.
    action_issues = _action_issues(
        symbol, data.unapplied_actions, action_index, acct_names
    )

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
        # W5 / F-17: what the drawer renders BESIDE a ⚠ 對帳不一致 footer. Always present
        # (three empty lists on a clean symbol) so the frontend needs no existence check.
        "action_issues": action_issues,
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
        "kind": r.kind,  # "sale" | "dividend" (post-close payout, audit H2)
    }
