"""The cash-movement kind vocabulary — ONE table, TWO orthogonal axes.

Before this module the direction of a cash movement was written out as ``kind ==
"WITHDRAW"`` in two calculation modules that do not import each other:

* ``portfolio/cash.py::_movement_sign`` — ``WITHDRAW`` is ``-1``, **everything else** ``+1``
* ``forex/pools.py::_is_debit`` — ``WITHDRAW`` is the only debit

Both were correct while every non-withdrawal kind was a credit, and both fail SILENTLY the
moment that stops being true.  Adding ``BROKER_FEE`` under the old predicate makes a broker
fee *increase* the cash balance **and** count as a basis-unknown foreign acquisition, which
drags ``covered_ratio`` down — two wrong money-of-record figures, neither of which raises.
The predicate is therefore replaced by an explicit table, and both modules read THIS one.

**The two axes are genuinely independent** — this is why one boolean was never enough:

``credit``
    Does the movement ADD to the pool balance?  Pure arithmetic; every kind has an answer.

``fx_acquisition``
    Does it ACQUIRE foreign currency, i.e. does it belong in ``covered_ratio``'s denominator
    (``forex/pools.py::acquisition_basis``)?  Only **funding** flows do.  ``domain-ledger.md``
    already draws this line for the flows it names: *"Sale proceeds and foreign cash dividends
    are not unbased acquisitions; they keep inheriting the pool average."*  Broker interest is
    the same shape — income arising INSIDE the pool, not capital brought into it.  Booking it
    as an acquisition would make a USD account that never converted a cent report
    ``covered_ratio < 1`` merely for having earned interest, flagging the whole foreign
    exposure (F3: cash *and* stocks) as basis-incomplete.  A false alarm on every real
    account is worse than no alarm.

Debits are never acquisitions — a disposal changes neither the average nor the coverage
(``domain-ledger.md`` N1) — so the table has no ``credit=False, fx_acquisition=True`` row.
The converse is populated: ``INTEREST`` is a credit that is not an acquisition.

**What this table deliberately does NOT govern: the overdraft guard.**  ``INTEREST_EXPENSE``
and ``BROKER_FEE`` are debits here, but they stay OUT of the ``running_min`` withdrawal guard
(audit C3) and out of N1's foreign-withdrawal hint, both of which remain keyed on
``WITHDRAW`` alone.  A withdrawal is a user's *intention*, so blocking one that overdraws the
pool catches a data-entry error before it is booked; a fee or margin interest is a *recorded
fact*, and a margin account legitimately runs a negative cash balance — that is what margin
is.  Blocking those would refuse to record what the statement says happened.

Registering a new kind means adding it here AND to ``data_ingestion/validate.py``'s allowed
set.  Those two are asserted equal by ``tests/shared/test_cash_kinds.py`` so a half-registered
kind is a test failure rather than a silently mis-signed pool.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final


class CashKind(StrEnum):
    """Every cash-movement kind the ledger stores.

    ``DEPOSIT`` / ``WITHDRAW`` / ``OPENING`` / ``REBATE`` predate this module and keep their
    exact prior behaviour.  The last three arrived with the broker-statement importer
    (2026-08-13): a real US broker export carries interest earned, **margin interest paid**
    and broker fees as three distinct economic events, and amounts are stored unsigned with
    the direction coming from the kind — so three events need three kinds.  Folding margin
    interest into ``BROKER_FEE`` would be arithmetically right and would display 融資利息 as
    「券商費用」 on every screen.
    """

    DEPOSIT = "DEPOSIT"
    WITHDRAW = "WITHDRAW"
    OPENING = "OPENING"
    REBATE = "REBATE"
    INTEREST = "INTEREST"
    INTEREST_EXPENSE = "INTEREST_EXPENSE"
    BROKER_FEE = "BROKER_FEE"


@dataclass(frozen=True)
class CashKindSpec:
    """How one kind behaves on each of the two axes (see the module docstring)."""

    credit: bool
    fx_acquisition: bool


_SPECS: Final[dict[CashKind, CashKindSpec]] = {
    # External capital entering the pool: a credit, and the acquisition whose home-currency
    # cost `acq_home_amount` records (F1).
    CashKind.DEPOSIT: CashKindSpec(credit=True, fx_acquisition=True),
    CashKind.OPENING: CashKindSpec(credit=True, fx_acquisition=True),
    # 折讓款 — a broker rebate credited back. Kept an acquisition: that is its behaviour
    # since the F1 spec, and changing it here would silently move an existing ledger's
    # covered_ratio, which is a money-of-record change with no decision behind it.
    CashKind.REBATE: CashKindSpec(credit=True, fx_acquisition=True),
    # N1: a foreign withdrawal recognises no realized FX and does not touch the coverage.
    CashKind.WITHDRAW: CashKindSpec(credit=False, fx_acquisition=False),
    # Income arising INSIDE the pool — same treatment as a foreign cash dividend.
    CashKind.INTEREST: CashKindSpec(credit=True, fx_acquisition=False),
    CashKind.INTEREST_EXPENSE: CashKindSpec(credit=False, fx_acquisition=False),
    CashKind.BROKER_FEE: CashKindSpec(credit=False, fx_acquisition=False),
}

#: Canonical stored spellings, for the write-path allowed set and the CSV/API validators.
CASH_KIND_VALUES: Final[frozenset[str]] = frozenset(k.value for k in CashKind)

#: Kinds that REDUCE the pool balance. Exposed for readers that want the set rather than
#: the per-row predicate (e.g. a SQL filter or a report grouping).
DEBIT_KINDS: Final[frozenset[str]] = frozenset(
    k.value for k, s in _SPECS.items() if not s.credit
)

_CREDIT = Decimal("1")
_DEBIT = Decimal("-1")

# An unrecognised kind cannot reach a calculation: `validate.py`'s allowed set gates every
# write path (manual API, CSV import, ledger edit). Should one ever arrive — a hand-edited
# database, a future kind added to only half the registration points — it is treated as a
# non-acquiring CREDIT, which is byte-for-byte what BOTH old predicates did, so this module
# cannot regress an existing ledger. The equality test named in the module docstring is the
# thing that keeps the case unreachable; this fallback only keeps the read path total.
_UNKNOWN: Final[CashKindSpec] = CashKindSpec(credit=True, fx_acquisition=True)


def canonical_kind(raw: str) -> str:
    """``' withdraw '`` -> ``'WITHDRAW'``.

    Unrecognised input is passed through (stripped + upper-cased) rather than rejected, so
    the caller's error message can name what was actually typed.
    """
    return raw.strip().upper()


def _spec(kind: str) -> CashKindSpec:
    canon = canonical_kind(kind)
    if canon not in CASH_KIND_VALUES:
        return _UNKNOWN
    return _SPECS[CashKind(canon)]


def is_debit(kind: str) -> bool:
    """True when *kind* REDUCES the pool balance (``WITHDRAW`` / fees / margin interest)."""
    return not _spec(kind).credit


def movement_sign(kind: str) -> Decimal:
    """``-1`` for a debit, ``+1`` for a credit — the multiplier for an unsigned amount."""
    return _DEBIT if is_debit(kind) else _CREDIT


def is_fx_acquisition(kind: str) -> bool:
    """True when *kind* ACQUIRES foreign currency, i.e. it belongs in ``covered_ratio``.

    Only funding flows do. See the module docstring for why interest does not.
    """
    return _spec(kind).fx_acquisition
