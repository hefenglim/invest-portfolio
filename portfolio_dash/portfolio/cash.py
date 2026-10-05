"""Per-account cash pools (2026-07-03, R6 item 7) — pure calculation.

Balance of one (account, currency) pool =
    Σ deposits/openings − Σ withdrawals              (cash_movements)
  − Σ fx.from_amount  + Σ fx.to_amount               (fx_conversions, per side)
  − Σ buy settlements (qty×price + fees + tax)       (transactions, quote ccy)
  + Σ sell proceeds  (qty×price − fees − tax)
  + Σ cash-family dividend nets (CASH / NET)         (DRIP/STOCK are share events)
    — of the dividends the replay BOOKED only (⑪, below)

**⑪ (owner ruling 2026-10-05): a dividend the replay refuses moves no cash.** The ``dividends``
every function here takes are the rows that BOOK — ``portfolio/returns.py::booked_dividends``
over the replay's ``refused_dividends`` (no position by the effective date, an open declared
short, a ticker an EXCHANGE vacated). 總報酬, XIRR and 股利總覽 already left those out; the pool
added them, so one payout had two answers and a guard could pass a withdrawal against money
no other figure contained. The functions stay pure and take no book: the CALLER filters, at
one seam per layer (``api/routers/cash.py::load_cash_ledgers``, the dashboard's own book, the
statement exports), and ``tests/contract/test_ruling11_refused_dividends_move_no_cash.py``
holds the set of callers to those seams. A surface that LISTS rows hands the refused ones to
:func:`pool_lines` separately (``unbooked_dividends``): they are listed, marked
``booked=False``, and move no running balance — a payout must not vanish from a statement the
owner reconciles, nor move a balance no other surface shows.

Opening inventory deliberately does NOT touch cash: its funding predates the
tracked history — record an initial DEPOSIT (or the ``opening`` 期初資金 movement)
if the pool history should balance from day one. This is operational cash tracking;
it feeds NO return metric (XIRR stays trade-flow based per domain-ledger.md).

Two views are exposed:
* ``cash_balances`` — the balance per pool AS OF a date (used by the cards + reporting
  total). **A balance is date-aware** (M5-06, owner ruling 2026-09-06): a row dated after
  ``as_of`` is in the ledger but not in that day's balance, so 「今天的餘額」 only counts
  what has happened by today — the same rule ``networth.daily_cash_series`` has always
  applied when it walks the calendar, which is why the two used to disagree by exactly
  the future row. ``as_of=None`` is the whole history (the pre-ruling reading).
* ``pool_lines`` / ``running_low`` / ``running_eod`` / ``running_min`` / ``running_statement`` — the
  DATE-ORDERED timeline of one pool, so a back-dated withdrawal that dips the running
  balance below zero is caught (audit C3) and the statement view (audit C5) can show a
  per-line running balance. The timeline is deliberately NOT bounded by a date: a
  future-dated flow still enters the running minimum, otherwise a deposit dated 2099
  could fund a withdrawal today whenever a pre-existing dip silences that branch. Both
  computed server-side; the frontend never computes money.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

from portfolio_dash.shared.cash_kinds import movement_sign
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import CASH_DIVIDEND_TYPES, Side
from portfolio_dash.shared.models.ledger import counts_by
from portfolio_dash.shared.money import settled_notional

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
    """The multiplier for an unsigned movement amount (audit C4).

    Delegates to ``shared/cash_kinds.py`` — the ONE table both this module and
    ``forex/pools.py`` read. This used to be ``kind == "WITHDRAW" -> -1, everything else
    -> +1``, which was right while every other kind was a credit and failed silently the
    moment one was not: ``BROKER_FEE`` made a fee ADD money to the pool. Delegating also
    fixes the case sensitivity the old comparison had (a wire-cased ``"withdraw"`` scored
    as a credit) that ``validate.py::_pool_row`` had to work around.
    """
    return movement_sign(kind)


def cash_balances(
    movements: Sequence[_MovementRow],
    fx_conversions: Sequence[_FxRow],
    transactions: Sequence[_TxRow],
    dividends: Sequence[_DivRow],
    instruments: dict[str, Instrument],
    *,
    as_of: date | None = None,
) -> dict[tuple[str, Currency], Decimal]:
    """All (account, currency) pool balances, including zero/negative ones.

    ``as_of`` bounds the balance to rows dated **on or before** that day (M5-06). A pool
    whose rows are all later is still LISTED — at zero — so the set of pools is the same
    whichever day is asked for; only the amounts move. ``None`` is the whole history.
    Inclusive on purpose: this ledger carries dates, not timestamps, so "today" includes
    everything dated today (the same reading ``forex/pools.acquisition_basis`` gives its
    own ``as_of``).

    Rows whose symbol is unregistered are skipped (same degradation rule as the
    dashboard) — an un-bookable row must not crash the cash view either.

    ``dividends`` are the rows the replay BOOKED (⑪ — see the module docstring); a refused
    payout is never handed here.
    """
    bal: dict[tuple[str, Currency], Decimal] = {}

    def add(account_id: str, ccy: Currency, on: date, delta: Decimal) -> None:
        key = (account_id, ccy)
        bal[key] = bal.get(key, _ZERO) + (delta if as_of is None or counts_by(on, as_of) else _ZERO)

    for m in movements:
        add(m.account_id, m.ccy, m.date, _movement_sign(m.kind) * m.amount)

    for c in fx_conversions:
        add(c.account_id, c.from_ccy, c.date, -c.from_amount)
        add(c.account_id, c.to_ccy, c.date, c.to_amount)

    for t in transactions:
        inst = instruments.get(t.symbol)
        if inst is None:
            continue
        # The settled 價金 (owner 2026-09-30), never the raw product: a sub-cent amount is
        # not money any account can hold.
        gross = settled_notional(t.quantity, t.price, inst.quote_ccy)
        if t.side is Side.BUY:
            add(t.account_id, inst.quote_ccy, t.trade_date, -(gross + t.fees + t.tax))
        else:
            add(t.account_id, inst.quote_ccy, t.trade_date, gross - t.fees - t.tax)

    for d in dividends:
        inst = instruments.get(d.symbol)
        if inst is None:
            continue
        if d.type in CASH_DIVIDEND_TYPES:
            add(d.account_id, inst.quote_ccy, d.date, d.net)

    return bal


# ---------------------------------------------------------------------------
# Date-ordered pool timeline (audit C3 running-balance guard + C5 statement)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CashLine:
    """One dated flow into a single (account, currency) pool.

    ``ref``/``delta`` are the load-bearing fields the running balance is built from;
    ``kind`` selects the label. The trailing OPTIONAL fields carry structured detail so
    the 說明 column (and the CSV/report exports) can render a human-readable line without
    the frontend recomputing anything (all values already Decimal, computed here):

    * ``buy`` / ``sell`` — ``symbol``, ``name``, ``qty``, ``price``, ``fee``, ``tax``
      (``delta`` is still the net settlement, unchanged).
    * ``dividend`` — ``symbol``, ``name`` (``delta`` is the cash net).
    * ``fx_in`` / ``fx_out`` — ``fx_rate`` (implied home-per-foreign = from/to), plus the
      PAIRED leg as ``counter_ccy`` + ``counter_amount`` signed by ITS pool's effect
      (fx_in: the from-pool lost money → negative; fx_out: the to-pool gained → positive).
    * movements (deposit/withdraw/opening/rebate) — no structured detail; ``ref`` is the note.

    ``booked`` (⑪): ``False`` only on a ``dividend`` line the replay REFUSED, which a listing
    shows so the payout does not vanish from the statement. ``delta`` is still the net the row
    records, but :func:`_effect` reads it as zero, so the line moves no running balance in any
    of this module's views — a refused line that reached a guard's timeline could not move it
    either.
    """

    date: date
    kind: str  # deposit | withdraw | opening | rebate | fx_in | fx_out | buy | sell | dividend
    ref: str   # symbol or note (or the fx pair string)
    delta: Decimal
    # optional structured detail (None when the field does not apply to the kind)
    symbol: str | None = None
    name: str | None = None
    qty: Decimal | None = None
    price: Decimal | None = None
    fee: Decimal | None = None
    tax: Decimal | None = None
    fx_rate: Decimal | None = None
    counter_ccy: str | None = None
    counter_amount: Decimal | None = None
    booked: bool = True


#: ⑪ — how every listing marks a ``booked=False`` line: the 現金收支明細 screen (sent on the
#: statement wire), its printed report and its reconciliation CSV. One spelling, here.
UNBOOKED_DIVIDEND_NOTE = "未入帳（待釐清）"
#: …and why, for the screen's hover and the printed report's note.
UNBOOKED_DIVIDEND_REASON = (
    "帳本重算時這筆股利無法入帳（該帳戶當時沒有對應持倉、落在放空部位，或標的已換股），"
    "不計入任何餘額"
)


def _effect(ln: CashLine) -> Decimal:
    """What *ln* does to its pool's balance: its ``delta``, or nothing for a refused line (⑪)."""
    return ln.delta if ln.booked else _ZERO


def pool_lines(
    account_id: str,
    ccy: Currency,
    movements: Sequence[_MovementRow],
    fx_conversions: Sequence[_FxRow],
    transactions: Sequence[_TxRow],
    dividends: Sequence[_DivRow],
    instruments: dict[str, Instrument],
    *,
    unbooked_dividends: Sequence[_DivRow] = (),
) -> list[CashLine]:
    """Every dated flow into ONE (account, ccy) pool (movements + fx legs + trade
    settlements + cash dividends). Unsorted; use :func:`running_statement`/`running_min`.

    Each line also carries structured detail (symbol/name/qty/price/fee/tax for trades,
    symbol/name for dividends, fx_rate/counter_* for fx legs) drawn from the same in-scope
    rows — the ``delta`` and ordering math is UNCHANGED (detail is additive).

    ``dividends`` are the rows that BOOK (⑪). ``unbooked_dividends`` — the rows the replay
    refused — are for a LISTING only: each becomes a ``booked=False`` line that the statement
    shows and no balance counts. A guard or a balance never passes them."""
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
        rate = c.from_amount / c.to_amount if c.to_amount != _ZERO else None
        if c.to_ccy == ccy:
            # fx_in: this pool RECEIVED to_amount; the paired from-pool LOST from_amount.
            lines.append(CashLine(c.date, "fx_in", pair, c.to_amount, fx_rate=rate,
                                  counter_ccy=c.from_ccy.value, counter_amount=-c.from_amount))
        if c.from_ccy == ccy:
            # fx_out: this pool SPENT from_amount; the paired to-pool GAINED to_amount.
            lines.append(CashLine(c.date, "fx_out", pair, -c.from_amount, fx_rate=rate,
                                  counter_ccy=c.to_ccy.value, counter_amount=c.to_amount))
    for t in transactions:
        inst = instruments.get(t.symbol)
        if inst is None or t.account_id != account_id or inst.quote_ccy != ccy:
            continue
        kind = "buy" if t.side is Side.BUY else "sell"
        gross = settled_notional(t.quantity, t.price, ccy)
        delta = (-(gross + t.fees + t.tax) if t.side is Side.BUY
                 else gross - t.fees - t.tax)
        lines.append(CashLine(t.trade_date, kind, t.symbol, delta, symbol=t.symbol,
                              name=inst.name, qty=t.quantity, price=t.price,
                              fee=t.fees, tax=t.tax))
    for booked, rows in ((True, dividends), (False, unbooked_dividends)):
        for d in rows:
            inst = instruments.get(d.symbol)
            if inst is None or d.account_id != account_id or inst.quote_ccy != ccy:
                continue
            if d.type in CASH_DIVIDEND_TYPES:
                lines.append(CashLine(d.date, "dividend", d.symbol, d.net,
                                      symbol=d.symbol, name=inst.name, booked=booked))
    return lines


def _ordered(lines: Sequence[CashLine]) -> list[CashLine]:
    """Chronological, with same-day credits before debits (so a same-day funding
    covers a same-day spend rather than spuriously dipping negative)."""
    return sorted(lines, key=lambda ln: (ln.date, 0 if ln.delta >= _ZERO else 1))


def running_low(lines: Sequence[CashLine]) -> tuple[Decimal, date | None]:
    """Minimum running balance over the date-ordered pool AND the day it is first reached.

    ``(0, None)`` for a pool that never dips below zero (an empty pool included): the day
    is only meaningful for a dip, and the strict ``<`` means a low reached on several days
    names the EARLIEST of them (M5-07) — that is the day the money was first short, which
    is what the owner has to go and fund."""
    bal = _ZERO
    mn = _ZERO
    on: date | None = None
    for ln in _ordered(lines):
        bal += _effect(ln)
        if bal < mn:
            mn, on = bal, ln.date
    return mn, on


def running_eod(lines: Sequence[CashLine]) -> tuple[tuple[date, Decimal], ...]:
    """The pool's END-OF-DAY running balance, one point per day that has a line.

    What ``shared/cash_dip.py`` compares day by day (DEF-090). Same-day credits are booked
    before debits (:func:`_ordered`), so a day's lowest point is its end-of-day balance and
    this series loses nothing :func:`running_low` would have seen."""
    out: dict[date, Decimal] = {}
    bal = _ZERO
    for ln in _ordered(lines):
        bal += _effect(ln)
        out[ln.date] = bal
    return tuple(out.items())


def running_min(lines: Sequence[CashLine]) -> Decimal:
    """Minimum running balance over the date-ordered pool (0 for an empty pool).

    Negative iff the pool dips below zero at ANY point in time — the date-aware
    overdraft check (audit C3), stricter than the end-aggregate it replaces."""
    return running_low(lines)[0]


def running_statement(lines: Sequence[CashLine]) -> list[tuple[CashLine, Decimal]]:
    """Date-ordered lines each paired with the running balance AFTER that line."""
    out: list[tuple[CashLine, Decimal]] = []
    bal = _ZERO
    for ln in _ordered(lines):
        bal += _effect(ln)
        out.append((ln, bal))
    return out


def account_statement(
    account_id: str,
    movements: Sequence[_MovementRow],
    fx_conversions: Sequence[_FxRow],
    transactions: Sequence[_TxRow],
    dividends: Sequence[_DivRow],
    instruments: dict[str, Instrument],
    *,
    ccy: Currency | None = None,
    unbooked_dividends: Sequence[_DivRow] = (),
) -> list[tuple[Currency, list[tuple[CashLine, Decimal]]]]:
    """Per-(account, ccy) running statements for one account.

    Returns ``[(ccy, running_statement)]`` in a stable currency order. The running balance
    is ALWAYS computed within its own (account, ccy) pool — currencies are never blended.

    * ``ccy`` given → exactly that one pool (even if empty, mirroring the single-pool view).
    * ``ccy`` None → every currency the account has activity in (empty pools dropped).

    The one shared source for the single-pool statement, the account-wide combined view,
    and the CSV/report exports (so all three read identical numbers). ``dividends`` /
    ``unbooked_dividends`` as in :func:`pool_lines` (⑪): a refused payout is listed, with the
    running balance it did not move."""
    candidates = [ccy] if ccy is not None else list(Currency)
    result: list[tuple[Currency, list[tuple[CashLine, Decimal]]]] = []
    for c in candidates:
        stmt = running_statement(pool_lines(
            account_id, c, movements, fx_conversions, transactions, dividends, instruments,
            unbooked_dividends=unbooked_dividends))
        if ccy is None and not stmt:
            continue  # skip currencies this account never touched
        result.append((c, stmt))
    return result
