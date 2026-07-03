"""Per-account cash pools (2026-07-03, R6 item 7) — pure calculation.

Balance of one (account, currency) pool =
    Σ deposits − Σ withdrawals                     (cash_movements)
  − Σ fx.from_amount  + Σ fx.to_amount             (fx_conversions, per side)
  − Σ buy settlements (qty×price + fees + tax)     (transactions, quote ccy)
  + Σ sell proceeds  (qty×price − fees − tax)
  + Σ cash-family dividend nets (CASH / NET)       (DRIP/STOCK are share events)

Opening inventory deliberately does NOT touch cash: its funding predates the
tracked history — record an initial DEPOSIT if the pool history should balance
from day one. This is operational cash tracking; it feeds NO return metric
(XIRR stays trade-flow based per domain-ledger.md).
"""

from collections.abc import Sequence
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
    kind: str
    ccy: Currency
    amount: Decimal


class _FxRow(Protocol):
    account_id: str
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


class _DivRow(Protocol):
    account_id: str
    symbol: str
    type: str
    net: Decimal


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
        sign = Decimal("1") if m.kind == "DEPOSIT" else Decimal("-1")
        add(m.account_id, m.ccy, sign * m.amount)

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
