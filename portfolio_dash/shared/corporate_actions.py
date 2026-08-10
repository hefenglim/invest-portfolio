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
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction
from typing import Protocol

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


class StoredActionRow(Protocol):
    """Structural view of a persisted ``corporate_actions`` row.

    A ``Protocol`` rather than an import of ``data_ingestion.store.StoredCorporateAction``,
    because the dependency only runs one way: ``store`` imports THIS module, so this module
    can never import ``store``. Structural typing lets :meth:`ActionIndex.from_stored` own
    the stored-row → domain-model conversion anyway, which is the point — that conversion is
    currently open-coded in ``store.load_ledger_bundle`` and again in ``scheduler/jobs.py``,
    and a third copy in ``data_ingestion/holdings.py`` is exactly the drift §6.0's
    "ONE owner per concept" exists to prevent.
    """

    account_id: str
    date: date
    kind: str
    from_symbol: str
    to_symbol: str
    ratio_to: Decimal
    ratio_from: Decimal
    cost_carry: Decimal | None
    note: str | None


def _reduced(action: CorporateAction) -> Fraction:
    """The ratio as a REDUCED fraction — ``3/1`` and ``30/10`` collapse to one value.

    Term-wise comparison is not equality of ratios, and the difference is not cosmetic: the
    split dedup key used ``(ratio_to, ratio_from)``, so the same 3-for-1 entered once as
    ``3/1`` and once as ``30/10`` survived as two entries and :func:`split_factor` returned
    **9** instead of 3 (audit F-10, reproduced). ``is_ratio_term`` guarantees both terms are
    positive integral Decimals, so ``int()`` is exact and ``Fraction`` normalises them.
    """
    return Fraction(int(action.ratio_to), int(action.ratio_from))


@dataclass(frozen=True)
class ActionIndex:
    """Corporate actions pre-grouped for lookup. Built ONCE per request / validation batch.

    Not per row: ``validate.py``'s oversell guard runs once per transaction, so a ~1,400-row
    import would otherwise re-read and re-group the whole action ledger ~1,400 times.

    Construct with :meth:`build` (domain models) or :meth:`from_stored` (ledger rows); the
    grouped views are computed once there.
    """

    all: tuple[CorporateAction, ...]
    _by_source: dict[tuple[str, str], tuple[CorporateAction, ...]]
    _by_dest: dict[tuple[str, str], tuple[CorporateAction, ...]]
    _by_symbol: dict[tuple[str, str], tuple[CorporateAction, ...]]
    _splits_by_symbol: dict[str, tuple[CorporateAction, ...]]
    # The share walker's depth-cap sink (D31). A MUTABLE set on a frozen dataclass, and
    # deliberately so: D31 says the capped symbol is recorded in a "per-request set", and
    # this index IS the per-request object D23 rule 2 already requires every caller to
    # thread. A second object to thread would be a second thing to forget, and forgetting
    # it loses the 待釐清 flag silently. `compare=False` keeps it out of equality: it is
    # diagnostics ABOUT a walk, not part of the index's identity.
    _depth_capped: set[tuple[str, str]] = field(
        default_factory=set, compare=False, repr=False
    )
    # D33's sink, same lifetime and the same reason. Kept SEPARATE from the depth cap
    # because the two degradations have different causes and need different sentences: one
    # says "the chain is too long to follow", the other "this action was skipped because the
    # position was already negative". One bag with a reason code would have been the same
    # information; two named pairs make the wrong message impossible to emit.
    _negative_source_skips: set[tuple[str, str]] = field(
        default_factory=set, compare=False, repr=False
    )

    @classmethod
    def build(cls, actions: Iterable[CorporateAction]) -> "ActionIndex":
        ordered = tuple(sorted(actions, key=lambda a: a.date))  # stable: input order breaks ties
        by_source: dict[tuple[str, str], list[CorporateAction]] = {}
        by_dest: dict[tuple[str, str], list[CorporateAction]] = {}
        by_symbol: dict[tuple[str, str], list[CorporateAction]] = {}
        splits: dict[str, list[CorporateAction]] = {}
        seen_split: set[tuple[str, date, Fraction]] = set()
        for a in ordered:
            by_source.setdefault((a.account_id, a.from_symbol), []).append(a)
            by_dest.setdefault((a.account_id, a.to_symbol), []).append(a)
            # A SET of keys, so a SPLIT — whose E20 rule forces `to_symbol == from_symbol`
            # — is filed ONCE. This is the whole reason `for_symbol` exists; see its
            # docstring for the 3-for-1 → 900-shares failure it removes.
            for key in {(a.account_id, a.from_symbol), (a.account_id, a.to_symbol)}:
                by_symbol.setdefault(key, []).append(a)
            if a.kind is CorporateActionKind.SPLIT:
                # Deduplicated on (symbol, date, REDUCED ratio) because the ledger row is
                # PER ACCOUNT while `prices` is not: a 3-for-1 held in three accounts is
                # three rows and one price event. Multiplying all three would make it
                # 27-for-1 — and keying on the raw terms let `3/1` and `30/10` through as
                # two distinct events, which is the same bug wearing a disguise (F-10).
                split_key = (a.from_symbol, a.date, _reduced(a))
                if split_key not in seen_split:
                    seen_split.add(split_key)
                    splits.setdefault(a.from_symbol, []).append(a)
        return cls(
            all=ordered,
            _by_source={k: tuple(v) for k, v in by_source.items()},
            _by_dest={k: tuple(v) for k, v in by_dest.items()},
            _by_symbol={k: tuple(v) for k, v in by_symbol.items()},
            _splits_by_symbol={k: tuple(v) for k, v in splits.items()},
        )

    @classmethod
    def from_stored(cls, rows: Iterable[StoredActionRow]) -> "ActionIndex":
        """Build from persisted ledger rows, converting each to a validated domain model.

        A row too malformed to be a :class:`CorporateAction` (a non-integer ratio term, an
        unknown ``kind``) RAISES rather than being dropped — the same strictness, for the
        same reason, as ``store.load_ledger_bundle`` and ``scheduler/jobs.py``: silently
        omitting a factor produces a share count that is wrong by the ratio and looks
        entirely normal. Validation makes such a row unreachable except by hand-editing the
        database, and a hand-edited row already breaks every replay call site.
        """
        return cls.build(
            CorporateAction(
                account_id=r.account_id,
                date=r.date,
                kind=CorporateActionKind(r.kind),
                from_symbol=r.from_symbol,
                to_symbol=r.to_symbol,
                ratio_to=r.ratio_to,
                ratio_from=r.ratio_from,
                cost_carry=r.cost_carry,
                note=r.note,
            )
            for r in rows
        )

    def by_source(self, account_id: str, symbol: str) -> tuple[CorporateAction, ...]:
        """Date-ordered actions this (account, symbol) is the SOURCE of.

        ⚠ **Never merge this with :meth:`by_dest`** — use :meth:`for_symbol`.
        """
        return self._by_source.get((account_id, symbol), ())

    def by_dest(self, account_id: str, symbol: str) -> tuple[CorporateAction, ...]:
        """Date-ordered actions this (account, symbol) is the DESTINATION of.

        The share-count walker needs this: a destination's history reaches back through
        another symbol entirely, and transitively (a de-SPAC into a ticker later renamed).

        ⚠ **Never merge this with :meth:`by_source`** — use :meth:`for_symbol`.
        """
        return self._by_dest.get((account_id, symbol), ())

    def for_symbol(self, account_id: str, symbol: str) -> tuple[CorporateAction, ...]:
        """Every action touching this (account, symbol) — either end — date-ordered, ONCE.

        This is the accessor the share walker must use, and it exists because the obvious
        way to get the same stream is wrong. E20 forces ``to_symbol == from_symbol`` on a
        SPLIT, so a SPLIT is filed under the SAME key in both :attr:`_by_source` and
        :attr:`_by_dest`; concatenating the two lists yields it **twice** and the walker
        applies the ratio twice — ``apply_ratio(apply_ratio(100, a), a)`` is **900** for a
        3-for-1, measured (audit F-09). De-duplication happens at build time via a set of
        keys, so there is no per-call dedup to get wrong and no reliance on
        :class:`CorporateAction` being hashable (it is a Pydantic model, and is not).
        """
        return self._by_symbol.get((account_id, symbol), ())

    def splits_on(self, symbol: str) -> tuple[CorporateAction, ...]:
        """Date-ordered SPLITs affecting *symbol*'s price series, deduplicated across accounts."""
        return self._splits_by_symbol.get(symbol, ())

    def note_depth_capped(self, account_id: str, symbol: str) -> None:
        """Record that a share walk for this position hit the depth cap (D31).

        Read paths keep their bare ``Decimal`` return and fall back to the pre-action share
        count; this is the channel that lets the validation path raise a ``needs_confirm``
        issue and the display surface a 待釐清 chip, instead of the wrong number passing as
        trustworthy.
        """
        self._depth_capped.add((account_id, symbol))

    def depth_capped_symbols(self) -> frozenset[tuple[str, str]]:
        """``(account_id, symbol)`` pairs whose walk was cut short since this index was built."""
        return frozenset(self._depth_capped)

    def note_negative_source_skip(self, account_id: str, symbol: str) -> None:
        """Record that the share path skipped an action on a negative source (D33).

        The skip itself is the correctness fix — applied unconditionally, the share-only path
        manufactures a destination the replay never created, with no transaction, no opening
        and no holding, and therefore **no flag of any kind**. The drawer then renders
        ``＋公司行動 −100`` under a red 對帳不一致 with nothing to explain it. D33's ruling is
        "skip **and flag**", and this is the flag's channel: §6.3's reconciliation footer only
        works when the mismatch comes with its cause attached.
        """
        self._negative_source_skips.add((account_id, symbol))

    def negative_source_skips(self) -> frozenset[tuple[str, str]]:
        """``(account_id, symbol)`` pairs — both ends — of every action skipped under D33."""
        return frozenset(self._negative_source_skips)


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
