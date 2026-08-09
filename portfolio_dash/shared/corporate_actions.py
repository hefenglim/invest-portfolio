"""Corporate-action algebra — the ONE place a ratio is applied (spec §6.0, owner 1).

``shared/`` depends on nothing internal and everything may import it
(``architecture.md``), which makes it the only home all four consumers — ``portfolio/``,
``data_ingestion/``, ``pricing/`` and ``api/`` — can reach without a lateral dependency.
That is the single reason the design works: without it, un-adjusting a provider price in
``pricing/`` would have to import from ``portfolio/``, which is an architecture violation.

**There is deliberately no ``ratio`` property returning a quotient.** Exposing one would
put a rounded Decimal back within reach of every caller, and that is the entire defect this
module exists to prevent. The only way to apply a ratio to a share count is
:func:`apply_ratio`.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, model_validator

_ZERO = Decimal("0")
_ONE = Decimal("1")


class CorporateActionKind(StrEnum):
    """SPLIT re-denominates one position; EXCHANGE moves it; SPINOFF carves a child off it."""

    SPLIT = "SPLIT"
    EXCHANGE = "EXCHANGE"
    SPINOFF = "SPINOFF"


def is_ratio_term(value: Decimal) -> bool:
    """A legal ratio term: a **positive integer** (spec §3.1(ii)(b), D14).

    One predicate, two callers: this model's validator and ``data_ingestion/validate.py``
    (which wraps it in a zh message). "Decimal > 0" was the earlier rule and it was not
    enough — it admits ``ratio_to = 0.2857``, a rounded quotient, which reproduces the
    賣超 cascade this whole feature exists to prevent at **any** share count.
    """
    if not value.is_finite():
        return False
    return value > _ZERO and value == value.to_integral_value()


class CorporateAction(BaseModel):
    """One corporate-action ledger row.

    The ratio is kept as its **two terms** and never as a single decimal: a decimal ratio is
    a rounded quotient, and ``data-and-pricing.md`` forbids storing a rounded quotient as
    the authority. "3-for-1" is ``(to=3, from=1)``; "1-for-20" is ``(1, 20)``; "2-for-7" is
    ``(2, 7)``.

    ``cost_carry`` stays a single Decimal because its source differs in kind: an 8-K
    publishes an allocation *percentage*, which is already exact as a decimal. The parent's
    share is never stored — it is ``1 − c`` computed on read, so parent and child sum to
    exactly 1 with no rounding leak.
    """

    account_id: str
    date: date
    kind: CorporateActionKind
    from_symbol: str
    to_symbol: str
    ratio_to: Decimal
    ratio_from: Decimal
    cost_carry: Decimal | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _check_terms(self) -> "CorporateAction":
        """Structural invariants only — the ones every downstream formula relies on.

        Ledger-consistency rules that need a user-facing zh message (a SPLIT whose
        ``to_symbol`` differs from its ``from_symbol``, an action on an account that does
        not hold the symbol, same-date collisions) belong to ``data_ingestion/validate.py``.
        This validator exists so that a ``CorporateAction`` in hand is always safe to hand
        to :func:`apply_ratio` and to the §4.3 carve.
        """
        for name, term in (("ratio_to", self.ratio_to), ("ratio_from", self.ratio_from)):
            if not is_ratio_term(term):
                raise ValueError(f"{name} must be a positive integer, got {term}")
        if self.kind is CorporateActionKind.SPINOFF:
            if self.cost_carry is None:
                raise ValueError("SPINOFF requires cost_carry")
            if not (_ZERO <= self.cost_carry <= _ONE):
                raise ValueError(f"cost_carry must be in [0, 1], got {self.cost_carry}")
        elif self.cost_carry is not None:
            raise ValueError(f"cost_carry is SPINOFF-only, got {self.kind} with {self.cost_carry}")
        return self


def apply_ratio(qty: Decimal, action: CorporateAction) -> Decimal:
    """``qty × to / from`` — multiply FIRST, divide LAST. The one place this order lives.

    Evaluation order is a **correctness requirement, not a style preference**. Written as
    ``qty * (to / from)`` the quotient is formed first and rounded to the context's 28
    significant digits before it ever meets the share count; an exhaustive sweep (share
    counts 1–1,000 × to 1–20 × from 1–20) found 77,577 pairs where the two forms differ and
    **3,530 of those cross an integer boundary** — e.g. ``3 × 1/3`` is exactly ``1``
    multiply-first and ``0.999…9`` parenthesised.

    That matters because ``validate.py`` compares a later sell against holdings with a bare
    ``>`` and no epsilon: one ulp short turns a legitimate sell into an oversell, the owner
    acknowledges it, and the STICKY guard discards the position's cost basis permanently.

    A 2-for-7 exchange of 700 shares must therefore produce **exactly** ``Decimal("200")``.
    """
    return qty * action.ratio_to / action.ratio_from


@dataclass(frozen=True)
class ActionIndex:
    """Corporate actions pre-grouped for lookup. Built ONCE per request / validation batch.

    Not per row: ``validate.py``'s oversell guard runs once per transaction, so a ~1,400-row
    import would otherwise re-read and re-group the whole action ledger ~1,400 times.

    Construct with :meth:`build`; the grouped views are computed once there.
    """

    all: tuple[CorporateAction, ...]
    _by_source: dict[tuple[str, str], tuple[CorporateAction, ...]]
    _by_dest: dict[tuple[str, str], tuple[CorporateAction, ...]]
    _splits_by_symbol: dict[str, tuple[CorporateAction, ...]]

    @classmethod
    def build(cls, actions: Iterable[CorporateAction]) -> "ActionIndex":
        ordered = tuple(sorted(actions, key=lambda a: a.date))  # stable: input order breaks ties
        by_source: dict[tuple[str, str], list[CorporateAction]] = {}
        by_dest: dict[tuple[str, str], list[CorporateAction]] = {}
        splits: dict[str, list[CorporateAction]] = {}
        seen_split: set[tuple[str, date, Decimal, Decimal]] = set()
        for a in ordered:
            by_source.setdefault((a.account_id, a.from_symbol), []).append(a)
            by_dest.setdefault((a.account_id, a.to_symbol), []).append(a)
            if a.kind is CorporateActionKind.SPLIT:
                # Deduplicated on (symbol, date, ratio) because the ledger row is PER ACCOUNT
                # while `prices` is not: a 3-for-1 held in three accounts is three rows and
                # one price event. Multiplying all three would make it 27-for-1.
                key = (a.from_symbol, a.date, a.ratio_to, a.ratio_from)
                if key not in seen_split:
                    seen_split.add(key)
                    splits.setdefault(a.from_symbol, []).append(a)
        return cls(
            all=ordered,
            _by_source={k: tuple(v) for k, v in by_source.items()},
            _by_dest={k: tuple(v) for k, v in by_dest.items()},
            _splits_by_symbol={k: tuple(v) for k, v in splits.items()},
        )

    def by_source(self, account_id: str, symbol: str) -> tuple[CorporateAction, ...]:
        """Date-ordered actions this (account, symbol) is the SOURCE of."""
        return self._by_source.get((account_id, symbol), ())

    def by_dest(self, account_id: str, symbol: str) -> tuple[CorporateAction, ...]:
        """Date-ordered actions this (account, symbol) is the DESTINATION of.

        The share-count walker needs this: a destination's history reaches back through
        another symbol entirely, and transitively (a de-SPAC into a ticker later renamed).
        """
        return self._by_dest.get((account_id, symbol), ())

    def splits_on(self, symbol: str) -> tuple[CorporateAction, ...]:
        """Date-ordered SPLITs affecting *symbol*'s price series, deduplicated across accounts."""
        return self._splits_by_symbol.get(symbol, ())


def split_factor(
    index: ActionIndex, symbol: str, *, after: date, through: date
) -> Decimal:
    """Cumulative SPLIT ratio on *symbol*'s price series over the window ``(after, through]``.

    **PRICES ONLY.** This returns a quotient, so it is rounded for any non-terminating
    ratio — acceptable for a float-derived price already capped at 4 dp, and FATAL for a
    share count. Share counts go through :func:`apply_ratio`, one action at a time.

    **SPLIT only** (D22). An EXCHANGE *adds to* its destination rather than re-denominating
    it, so including one here would corrupt the entire stored price history of any symbol
    you already held before a merger into it. The cliff that tempts a reader to widen this
    scope is fixed at the import seam instead (D19), not here.

    An empty window is ``Decimal(1)`` — the identity, so callers need no special case.

    Note on evaluation order: the terms are accumulated as ``Π to / Π from`` — one division,
    performed last — rather than as a product of per-action quotients. Same principle as
    :func:`apply_ratio` rule (a), and strictly less rounding for a chain of splits. For a
    single split (every real case so far) the two are identical.
    """
    numerator = _ONE
    denominator = _ONE
    for a in index.splits_on(symbol):
        if after < a.date <= through:
            numerator *= a.ratio_to
            denominator *= a.ratio_from
    if numerator == denominator:
        return _ONE  # exact identity, including the empty window — never 1.000…0
    return numerator / denominator
