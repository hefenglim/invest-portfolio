"""Per-account FX pool: weighted-avg acquisition rate and foreign cash reconstruction.

Inputs are already scoped to a single account (the caller filters by account_id).

Two "cash" definitions used to diverge here (audit C9). Since the 2026-07-30 spec
(``docs/spec/2026-07-30-fx-opening-basis.html``) they RECONCILE — a foreign-currency cash
movement now enters the pool, carrying the home-currency cost the user recorded:

* **FX-exposure view** (this module, :func:`foreign_cash_balance`): conversions + foreign
  trades + foreign cash dividends **+ foreign cash movements**. Drives 換匯損益.
* **Funds view** (``portfolio/cash.py`` :func:`cash_balances`): the operational cash pool
  per (account, currency) that the 資金管理 page shows, and the overdraft guard.

For one (account, foreign ccy) the two are now equal by construction: they sum the same
flows. The FX view additionally needs a COST BASIS, which is where the two still differ in
kind — a movement without ``acq_home_amount`` contributes to the balance but NOT to the
weighted average (its acquisition rate is unknown and is never guessed).

Cost basis, precisely (spec F1/F2/F3):

* **F1** — a movement stores the home-currency **AMOUNT** it cost, never a rate. Rates are
  averages, and ``data-and-pricing.md`` forbids storing an average as the authority;
  ``fx_conversions`` likewise stores two amounts. The displayed rate is computed on read.
* **F2** — when part of the pool has no basis, outflows are absorbed **pro rata**, not by
  subtracting the unbased amount. Cash is fungible and weighted-average tracks no lots, so
  ``covered_ratio`` scales the exposure. The subtraction shortcut goes NEGATIVE as soon as
  the balance drops below the unbased amount — reintroducing exactly the reversed-sign
  figure this design exists to remove.
* **F3** — ``covered_ratio`` applies to the WHOLE foreign exposure (cash *and* stocks),
  because ``avg_rate`` itself is derived from the incomplete population; degrading only the
  cash leg would leave the larger error (the stock leg) unflagged.

Sale proceeds and foreign cash dividends are deliberately NOT counted as unbased
acquisitions: they keep their long-standing treatment of inheriting the pool's average.
Only FUNDING flows (conversions, cash movements) determine the coverage, so a ledger with
no foreign movements yields ``covered_ratio == 1`` and numbers identical to before.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

from portfolio_dash.shared.cash_kinds import is_debit, is_fx_acquisition
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import CASH_DIVIDEND_TYPES, Side
from portfolio_dash.shared.models.ledger import Dividend, FXConversion, Transaction

_ZERO = Decimal("0")
_ONE = Decimal("1")


class MovementRow(Protocol):
    """Duck-typed cash-movement row (``StoredCashMovement`` satisfies it)."""

    account_id: str
    date: date
    kind: str
    ccy: Currency
    amount: Decimal
    acq_home_amount: Decimal | None


def _is_debit(kind: str) -> bool:
    """True when *kind* reduces the pool balance.

    Delegates to ``shared/cash_kinds.py``, which this module and ``portfolio/cash.py``
    now share. It used to read ``kind.upper() == "WITHDRAW"`` — a second, independent copy
    of the same predicate in a module the first one does not import, so the two could only
    ever be fixed twice.
    """
    return is_debit(kind)


@dataclass(frozen=True)
class FXPoolBasis:
    """The acquisition side of one (account, foreign ccy) pool.

    ``acquired_with_basis`` / ``acquired_without_basis`` are foreign-currency amounts;
    ``home_cost`` is the home-currency cost of the former. Disposals never appear here —
    under weighted average a disposal changes neither the average nor the coverage.
    """

    acquired_with_basis: Decimal = _ZERO
    home_cost: Decimal = _ZERO
    acquired_without_basis: Decimal = _ZERO

    @property
    def avg_rate(self) -> Decimal | None:
        """Weighted-average home-per-foreign rate, or None when no basis exists at all."""
        if self.acquired_with_basis == _ZERO:
            return None
        return self.home_cost / self.acquired_with_basis

    @property
    def covered_ratio(self) -> Decimal:
        """Fraction of the pool whose acquisition cost is known (F2).

        Exactly ``1`` whenever nothing is unbased — returned as the literal one, so a
        ledger without foreign movements produces byte-identical figures to the
        pre-spec engine (multiplying by it is skipped by the caller).
        """
        if self.acquired_without_basis == _ZERO:
            return _ONE
        total = self.acquired_with_basis + self.acquired_without_basis
        if total <= _ZERO:
            return _ONE
        return self.acquired_with_basis / total


def acquisition_basis(
    conversions: Sequence[FXConversion],
    movements: Sequence[MovementRow],
    home: Currency,
    foreign: Currency,
    *,
    as_of: date | None = None,
) -> FXPoolBasis:
    """Split the pool's foreign ACQUISITIONS into basis-known and basis-unknown.

    Sources: ``home -> foreign`` conversions (always basis-known — the ledger stores both
    amounts) and foreign-currency cash movements that ACQUIRE currency (basis-known iff
    ``acq_home_amount`` is set).

    The movement filter is ``is_fx_acquisition``, not ``not is_debit`` — the two stopped
    being the same predicate once ``INTEREST`` existed. Interest earned is a CREDIT that is
    not an acquisition: it arises inside the pool, so it inherits the pool's average exactly
    as sale proceeds and foreign cash dividends already do (``domain-ledger.md``). Counting
    it here would report ``covered_ratio < 1`` on a USD account that never converted a cent,
    flagging the whole foreign exposure — cash *and* stocks, per F3 — as basis-incomplete.

    ``as_of`` bounds BOTH sources to acquisitions dated ``<= as_of``; the default (None) is
    the full history. It exists for the REALIZED side, where the manual (§8.2) defines the
    cost of a reconversion as 「回換前 ... avg_rate」 — the average as it stood on the day of
    the disposal (QA-02). Two consequences worth stating rather than rediscovering:

    * **The bound is ``<=``, not ``<``.** This ledger carries dates, not timestamps, so a
      same-day fund-then-convert has no intra-day ordering to appeal to. A strict ``<``
      would leave such a day with no basis at all — a regression dressed as rigour.
    * **A disposal never appears here**, so bounding is safe for the very row being priced:
      a ``foreign -> home`` conversion is not an acquisition at any date, and under weighted
      average a disposal changes neither the average nor the coverage (N1).

    UNREALIZED calls this bounded to the VALUATION DAY (X8b, M5-06's third reader): it marks
    to market what is held now, so its rate must include every acquisition made to date —
    and none made after it. Full history was the same reading until a row could be dated in
    the future; a rated 2099 deposit then moved today's average (measured: 32 → 38.666…)
    and an unrated one pulled today's ``covered_ratio`` down on exposure that is fully
    covered. ``avg_rate`` and ``covered_ratio`` come from ONE bounded call on purpose: the
    coverage must be computed over the population that produced the balance it scales (F2
    is pro rata over the flows that MADE the balance), or ``fx_basis_gap = cash × (1 −
    ratio)`` reports a gap on money every dollar of which has a known cost.
    """
    with_basis = home_cost = without_basis = _ZERO
    for c in conversions:
        if as_of is not None and c.date > as_of:
            continue
        if c.from_ccy == home and c.to_ccy == foreign:
            with_basis += c.to_amount
            home_cost += c.from_amount
    for m in movements:
        if as_of is not None and m.date > as_of:
            continue
        if m.ccy != foreign or not is_fx_acquisition(m.kind):
            continue
        if m.acq_home_amount is None:
            without_basis += m.amount
        else:
            with_basis += m.amount
            home_cost += m.acq_home_amount
    return FXPoolBasis(with_basis, home_cost, without_basis)


def average_acquisition_rate(
    conversions: Sequence[FXConversion],
    home: Currency,
    foreign: Currency,
    *,
    movements: Sequence[MovementRow] | None = None,
    as_of: date | None = None,
) -> Decimal | None:
    """Weighted-average home-per-foreign rate over the pool's basis-known acquisitions.

    Returns None if the account has no such acquisition (no FX cost basis).
    ``as_of`` bounds the acquisitions to that date — see :func:`acquisition_basis`.
    """
    return acquisition_basis(
        conversions, movements or [], home, foreign, as_of=as_of).avg_rate


def foreign_cash_balance(
    transactions: Sequence[Transaction],
    dividends: Sequence[Dividend],
    conversions: Sequence[FXConversion],
    instruments: dict[str, Instrument],
    foreign: Currency,
    *,
    movements: Sequence[MovementRow] | None = None,
    as_of: date | None = None,
) -> Decimal:
    """Reconstruct the foreign-currency cash balance from the account's ledgers.

    + conversions into foreign, + foreign sale net proceeds, + foreign cash-family dividends
    net (``CASH_DIVIDEND_TYPES`` = CASH + NET), + foreign cash CREDITS (deposit / opening /
    rebate), - foreign buys (incl. fees+tax), - reconversions out of foreign, - foreign
    WITHDRAW. DRIP/STOCK dividends move no cash (DRIP nets to zero) and are excluded.

    Equals the funds view (``portfolio/cash.py``) for the same (account, ccy) pool — for
    the same ``as_of``. The bound is :func:`acquisition_basis`'s: rows dated ``<= as_of``
    count, ``None`` is the whole history, inclusive because this ledger carries dates, not
    timestamps. It is here because the funds view became date-aware first (M5-06, owner
    ruling 2026-09-06) and the identity this docstring claims — the manual's
    ``fx.pool_equals_funds`` (§8.3) — then failed on any ledger with a future-dated row: the
    資金 page read 「today」 while this pool still read 「ever」, and ``unrealized_fx_cash``
    marked money that has not arrived to market.

    ⚠ The dividend filter is ``CASH_DIVIDEND_TYPES``, never a bare ``is DividendType.CASH``.
    This module is a REPLAY SITE, and ``shared/models/enums.py`` holds that frozenset for
    exactly this reason — "ONE definition for every replay site ... so they can never drift".
    It did drift (QA-03): the narrow filter dropped every **NET** dividend, which is the MY
    single-tier net-received payout — real cash, in the pool's own currency — so the FX view
    disagreed with the funds view by the whole dividend, breaking the identity this module's
    own header claims and the manual anchors as ``fx.pool_equals_funds`` (§8.3).
    """
    cash = _ZERO
    for c in conversions:
        if as_of is not None and c.date > as_of:
            continue
        if c.to_ccy == foreign:
            cash += c.to_amount
        if c.from_ccy == foreign:
            cash -= c.from_amount
    for t in transactions:
        if as_of is not None and t.trade_date > as_of:
            continue
        if instruments[t.symbol].quote_ccy != foreign:
            continue
        if t.side is Side.BUY:
            cash -= t.quantity * t.price + t.fees + t.tax
        else:
            cash += t.quantity * t.price - t.fees - t.tax
    for d in dividends:
        if as_of is not None and d.date > as_of:
            continue
        if d.type in CASH_DIVIDEND_TYPES and instruments[d.symbol].quote_ccy == foreign:
            cash += d.net
    for m in movements or []:
        if as_of is not None and m.date > as_of:
            continue
        if m.ccy != foreign:
            continue
        cash += -m.amount if _is_debit(m.kind) else m.amount
    return cash
