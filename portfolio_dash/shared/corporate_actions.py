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
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction
from typing import Protocol

from pydantic import BaseModel, ValidationError, model_validator

from portfolio_dash.shared.money import cap_dp

_ZERO = Decimal("0")
_ONE = Decimal("1")
# Decimals kept for a PER-SHARE value restated across one action (:func:`apply_ratio_to_price`).
# 4 dp is the same cap ``pricing/store`` applies to every stored close and covers every market
# tick (US/TW 2 dp, MY 3 dp) — so a restated level can always be compared exactly against a
# stored price rather than against something one ulp away from it.
_PRICE_DP = 4


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


def apply_ratio_to_price(
    value: Decimal, *, ratio_to: Decimal, ratio_from: Decimal
) -> Decimal:
    """A PER-SHARE value restated across one action: ``value × from / to``, 4 dp.

    The mirror of :func:`apply_ratio`. Shares move by ``to/from``; anything denominated
    *per share* — a price, an owner-entered target level — moves by its reciprocal, because
    the two multiply out to the position's unchanged value (§2.1's conservation law seen
    from the price side). A 7-for-1 turns 200 into 28.5714.

    **Two bare terms, not a** :class:`CorporateAction`, unlike :func:`apply_ratio`. That
    one is called during a replay, where a validated action is already in hand; this one is
    called from *validation* and from the form preview, where the row is a candidate that
    may not be a legal action yet. Demanding the model there would make callers construct
    one from unvalidated input — precisely what the model's validator exists to stop.

    **It quantizes and :func:`apply_ratio` does not**, and the asymmetry is the point.
    ``apply_ratio``'s result is a share count that a bare ``>`` compares against a sell, so
    one ulp short is an oversell and a discarded cost basis — it must stay exact. This
    result is a **quotient**: ``200 × 1/7`` does not terminate, so *something* rounds it,
    and the only safe answer is that it rounds ONCE, here. Left unquantized, the number
    shown to the owner in a message, the number returned in the preview payload and the
    number actually written would each be free to round differently — the "two numbers on
    one screen" failure §5.1 names as the worst kind.

    **Not** :func:`split_factor`: that folds a whole *window* of actions into one quotient
    and is documented PRICES-ONLY-and-rounded. For a single action the two-term form is
    exact before the final cap, so this is the tighter of the two answers.
    """
    return cap_dp(value * ratio_from / ratio_to, _PRICE_DP)


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


class UnreadableAction(BaseModel):
    """A persisted ``corporate_actions`` row that is not a valid :class:`CorporateAction`.

    **Neither raised nor dropped, and both of those were tried (2026-08-11).** The three
    stored-row conversions all raised, and the one in ``store.load_ledger_bundle`` sits
    ABOVE ``build_book``'s graceful refusal path — so one row with a non-integer ratio
    term 500'd every page rather than degrading to 待釐清. Measured, not theorised.
    Dropping is worse and the old docstrings said so correctly: a silently omitted action
    yields a share count wrong by the ratio that looks entirely normal.

    So this is the third option, and it is not a new one — it is the mechanism the codebase
    already uses for "this row exists and cannot be trusted": the reader converts what it
    can, records what it cannot, and the consumer turns each record into an
    ``UnappliedAction`` with the same ``reason`` string. That blanks XIRR portfolio-wide
    with a named cause (D38 invariant 2) and marks the position 待釐清, exactly as E1/E2/E3
    already do one layer down.

    Reachable because **E6's rejection is entry-side only**: E21 establishes rows arriving
    behind validation, and until W7 wires it, ``validate_corporate_action`` has no
    production caller at all.
    """

    account_id: str
    date: date
    kind: str
    from_symbol: str
    to_symbol: str
    reason: str


def convert_stored(
    rows: Iterable[StoredActionRow],
) -> tuple[list[CorporateAction], list[UnreadableAction]]:
    """Split persisted rows into what replays and what cannot — the ONE owner (§6.0).

    This conversion was open-coded in three places (``store.load_ledger_bundle``,
    ``scheduler/jobs.py``, and :meth:`ActionIndex.from_stored`). Three copies of a
    conversion is three chances for one of them to keep raising after the others learned
    not to, which is the drift "one owner per concept" exists to prevent — and here the
    drift would be invisible until a malformed row happened to arrive through one path.

    ``reason`` is a zh sentence naming the offending value, because every consumer of it
    ends up on a screen. A bare "invalid row" forces the UI to say "something went wrong",
    and this repo's whole 待釐清 vocabulary is built on saying **which** row and **why**.
    """
    good: list[CorporateAction] = []
    bad: list[UnreadableAction] = []
    for r in rows:
        try:
            good.append(
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
            )
        except (ValidationError, ValueError) as exc:
            bad.append(
                UnreadableAction(
                    account_id=r.account_id,
                    date=r.date,
                    kind=str(r.kind),
                    from_symbol=r.from_symbol,
                    to_symbol=r.to_symbol,
                    reason=(
                        f"{r.from_symbol} 在 {r.date.isoformat()} 的公司行動資料不完整，"
                        f"無法套用（{r.kind} {r.ratio_to}/{r.ratio_from}）— "
                        "比例必須是兩個正整數，且分割的前後代號必須相同。"
                        f"請到公司行動帳本修正這一筆（{_first_line(exc)}）"
                    ),
                )
            )
    return good, bad


def _first_line(exc: Exception) -> str:
    """The exception's first line — enough to identify the field, short enough for a chip."""
    return str(exc).splitlines()[0].strip()


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
    # Rows this index could NOT convert. Empty for every index built from domain models;
    # populated only by `from_stored`, the one constructor that sees raw rows. Part of the
    # index's identity (no `compare=False`), unlike the two diagnostic sinks below: an
    # index that silently skipped a row is NOT the same index as one that had nothing to
    # skip, and equality saying otherwise is how such a row goes unnoticed.
    unreadable: tuple[UnreadableAction, ...] = ()
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
    # says "the chain is too long to follow", the other "this action was skipped because one
    # of its two sides was already negative". One bag with a reason code would have been the same
    # information; two named pairs make the wrong message impossible to emit.
    _negative_side_skips: set[tuple[str, str]] = field(
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
        """Build from persisted ledger rows via :func:`convert_stored`.

        A row too malformed to be a :class:`CorporateAction` is **recorded on
        :attr:`unreadable`, neither raised nor dropped** — see :class:`UnreadableAction`
        for why both of those were tried and rejected. This method previously raised, and
        its docstring argued the case well for *dropping*; what it missed is that its
        callers include ``store.load_ledger_bundle`` (above every never-500 guard) and the
        scheduler's price-factor builder (a background job whose crash the owner sees only
        as prices that stopped updating).

        The share walk therefore proceeds **without** the malformed row's ratio, which is a
        wrong share count — and that is safe here only because the same row is
        simultaneously blanking XIRR and flagging the position through ``build_book``. The
        old objection ("wrong by the ratio and looks entirely normal") is answered by
        making sure it does **not** look normal, not by refusing to compute.
        """
        good, bad = convert_stored(rows)
        return replace(cls.build(good), unreadable=tuple(bad))

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

    def note_negative_side_skip(self, account_id: str, symbol: str) -> None:
        """Record that the share path skipped an action with a negative SIDE (D33).

        The skip itself is the correctness fix — applied unconditionally, the share-only path
        manufactures a destination the replay never created, with no transaction, no opening
        and no holding, and therefore **no flag of any kind**. The drawer then renders
        ``＋公司行動 −100`` under a red 對帳不一致 with nothing to explain it. D33's ruling is
        "skip **and flag**", and this is the flag's channel: §6.3's reconciliation footer only
        works when the mismatch comes with its cause attached.

        "Side", not "source", since task #62: the same one comparison applies to the
        **destination** and covers E18 there. Both ends of a skipped action are recorded
        either way, so callers never need to know which side triggered it.
        """
        self._negative_side_skips.add((account_id, symbol))

    def negative_side_skips(self) -> frozenset[tuple[str, str]]:
        """``(account_id, symbol)`` pairs — both ends — of every action skipped under D33."""
        return frozenset(self._negative_side_skips)


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
