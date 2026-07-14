"""Per-account cash pools (2026-07-03, R6 item 7) — pure calculation.

Balance of one (account, currency) pool =
    Σ deposits/openings − Σ withdrawals              (cash_movements)
  − Σ fx.from_amount  + Σ fx.to_amount               (fx_conversions, per side)
  − Σ buy settlements (qty×price + fees + tax)       (transactions, quote ccy)
  + Σ sell proceeds  (qty×price − fees − tax)
  + Σ cash-family dividend nets (CASH / NET)         (DRIP/STOCK are share events)

Opening inventory deliberately does NOT touch cash: its funding predates the
tracked history — record an initial DEPOSIT (or the ``opening`` 期初資金 movement)
if the pool history should balance from day one. This is operational cash tracking;
it feeds NO return metric (XIRR stays trade-flow based per domain-ledger.md).

Two views are exposed:
* ``cash_balances`` — the END balance per pool (used by the cards + reporting total).
* ``pool_lines`` / ``running_min`` / ``running_statement`` — the DATE-ORDERED timeline
  of one pool, so a back-dated withdrawal that dips the running balance below zero is
  caught (audit C3) and the statement view (audit C5) can show a per-line running
  balance. Both computed server-side; the frontend never computes money.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import CASH_DIVIDEND_TYPES, Side

_ZERO = Decimal("0")


# Duck-typed row shapes: both the Stored* models and the ledger models satisfy
# these, so the calc stays pure and reusable (mirrors build_book's posture).
class _MovementRow(Protocol):
    account_id: str
    date: date
    kind: str
    ccy: Currency
    amount: Decimal
    note: str | None


class _FxRow(Protocol):
    account_id: str
    date: date
    from_ccy: Currency
    from_amount: Decimal
    to_ccy: Currency
    to_amount: Decimal


class _TxRow(Protocol):
    account_id: str
    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal
    fees: Decimal
    tax: Decimal
    trade_date: date


class _DivRow(Protocol):
    account_id: str
    symbol: str
    date: date
    type: str
    net: Decimal


def _movement_sign(kind: str) -> Decimal:
    """WITHDRAW is a debit; DEPOSIT and OPENING (期初資金) are credits (audit C4)."""
    return Decimal("-1") if kind == "WITHDRAW" else Decimal("1")


def cash_balances(
    movements: Sequence[_MovementRow],
    fx_conversions: Sequence[_FxRow],
    transactions: Sequence[_TxRow],
    dividends: Sequence[_DivRow],
    instruments: dict[str, Instrument],
) -> dict[tuple[str, Currency], Decimal]:
    """All (account, currency) pool balances, including zero/negative ones.

    Rows whose symbol is unregistered are skipped (same degradation rule as the
    dashboard) — an un-bookable row must not crash the cash view either.
    """
    bal: dict[tuple[str, Currency], Decimal] = {}

    def add(account_id: str, ccy: Currency, delta: Decimal) -> None:
        key = (account_id, ccy)
        bal[key] = bal.get(key, _ZERO) + delta

    for m in movements:
        add(m.account_id, m.ccy, _movement_sign(m.kind) * m.amount)

    for c in fx_conversions:
        add(c.account_id, c.from_ccy, -c.from_amount)
        add(c.account_id, c.to_ccy, c.to_amount)

    for t in transactions:
        inst = instruments.get(t.symbol)
        if inst is None:
            continue
        if t.side is Side.BUY:
            add(t.account_id, inst.quote_ccy, -(t.quantity * t.price + t.fees + t.tax))
        else:
            add(t.account_id, inst.quote_ccy, t.quantity * t.price - t.fees - t.tax)

    for d in dividends:
        inst = instruments.get(d.symbol)
        if inst is None:
            continue
        if d.type in CASH_DIVIDEND_TYPES:
            add(d.account_id, inst.quote_ccy, d.net)

    return bal


# ---------------------------------------------------------------------------
# Date-ordered pool timeline (audit C3 running-balance guard + C5 statement)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CashLine:
    """One dated flow into a single (account, currency) pool."""

    date: date
    kind: str  # deposit | withdraw | opening | fx_in | fx_out | buy | sell | dividend
    ref: str   # symbol or note
    delta: Decimal


def pool_lines(
    account_id: str,
    ccy: Currency,
    movements: Sequence[_MovementRow],
    fx_conversions: Sequence[_FxRow],
    transactions: Sequence[_TxRow],
    dividends: Sequence[_DivRow],
    instruments: dict[str, Instrument],
) -> list[CashLine]:
    """Every dated flow into ONE (account, ccy) pool (movements + fx legs + trade
    settlements + cash dividends). Unsorted; use :func:`running_statement`/`running_min`."""
    lines: list[CashLine] = []
    for m in movements:
        if m.account_id != account_id or m.ccy != ccy:
            continue
        lines.append(CashLine(m.date, m.kind.lower(), m.note or "",
                              _movement_sign(m.kind) * m.amount))
    for c in fx_conversions:
        if c.account_id != account_id:
            continue
        pair = f"{c.from_ccy.value}→{c.to_ccy.value}"
        if c.to_ccy == ccy:
            lines.append(CashLine(c.date, "fx_in", pair, c.to_amount))
        if c.from_ccy == ccy:
            lines.append(CashLine(c.date, "fx_out", pair, -c.from_amount))
    for t in transactions:
        inst = instruments.get(t.symbol)
        if inst is None or t.account_id != account_id or inst.quote_ccy != ccy:
            continue
        if t.side is Side.BUY:
            lines.append(CashLine(t.trade_date, "buy", t.symbol,
                                  -(t.quantity * t.price + t.fees + t.tax)))
        else:
            lines.append(CashLine(t.trade_date, "sell", t.symbol,
                                  t.quantity * t.price - t.fees - t.tax))
    for d in dividends:
        inst = instruments.get(d.symbol)
        if inst is None or d.account_id != account_id or inst.quote_ccy != ccy:
            continue
        if d.type in CASH_DIVIDEND_TYPES:
            lines.append(CashLine(d.date, "dividend", d.symbol, d.net))
    return lines


def _ordered(lines: Sequence[CashLine]) -> list[CashLine]:
    """Chronological, with same-day credits before debits (so a same-day funding
    covers a same-day spend rather than spuriously dipping negative)."""
    return sorted(lines, key=lambda ln: (ln.date, 0 if ln.delta >= _ZERO else 1))


def running_min(lines: Sequence[CashLine]) -> Decimal:
    """Minimum running balance over the date-ordered pool (0 for an empty pool).

    Negative iff the pool dips below zero at ANY point in time — the date-aware
    overdraft check (audit C3), stricter than the end-aggregate it replaces."""
    bal = _ZERO
    mn = _ZERO
    for ln in _ordered(lines):
        bal += ln.delta
        if bal < mn:
            mn = bal
    return mn


def running_statement(lines: Sequence[CashLine]) -> list[tuple[CashLine, Decimal]]:
    """Date-ordered lines each paired with the running balance AFTER that line."""
    out: list[tuple[CashLine, Decimal]] = []
    bal = _ZERO
    for ln in _ordered(lines):
        bal += ln.delta
        out.append((ln, bal))
    return out
