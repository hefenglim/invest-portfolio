"""The import gate: prove a grouped export is internally consistent, or refuse the batch.

This module is the point of the whole package. Parsing and grouping are mechanical; what
makes a broker import safe to run against a real ledger is that it can **state what it is
about to write and prove the statement**, and that a failure to prove it stops everything
rather than writing the part that happened to work.

**Two severities, and the line between them is not a matter of taste.**

* **blocking** — the numbers we are about to write contradict themselves. Money or shares
  were invented or destroyed by OUR OWN transformation of the file. There is no partial
  import: the whole batch is refused, because a ledger half-written from an inconsistent
  source is worse than one not written at all (:mod:`.provenance`'s batch id exists so the
  refusal is clean).
* **advisory** — rows that will NOT be written, named individually. An export legitimately
  contains option legs, positions older than the window, and a duplicated boundary row; a
  gate that rejected those would reject every real statement and be switched off within a
  week. They are a completeness gap for a human to close, not an arithmetic contradiction,
  so they are reported loudly and by ``ref`` — never counted and summarised away.

**What is checked here is deliberately not what the grouper already guarantees.** Re-running
a transformation's own logic over its own output proves nothing. Every blocking check below
re-measures from the ORIGINAL rows:

* :func:`grouping.suppress` phase 1 records ``amount_sum=0, quantity_sum=0`` as an
  *assertion* — it matches a reversal pair by ``o.amount == -n.amount`` and writes the zeros
  without ever adding the legs up. Here they are added up, from the raw rows the refs point
  at. That check is real precisely because the grouper skipped it.
* the priced-row check tests the BROKER's arithmetic and our parsing of it, which no amount
  of internal consistency can substitute for.
* row conservation (:func:`grouping.account_for`) is the one that has already caught two
  real defects — two rows silently dropped off an ``if``/``elif`` chain, and 108 rows
  double-counted because ``line_no`` is not unique across files.

⚠ **Share conservation is asserted under the DERIVED-share rule, not the printed one.** A
check written against the printed quantity would fail on 125 of 227 real reinvest rows and
would therefore forbid the very rule (``amount / price``, see
:func:`grouping.derive_reinvest_shares`) that makes a multi-year DRIP history come out right.
The substitution is the intended behaviour, so it is the baseline the check measures against.
"""

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal

from portfolio_dash.data_ingestion.broker.grouping import (
    GroupedImport,
    _share_delta,
    account_for,
    derive_reinvest_shares,
    overlap_duplicates,
    prehistory_shares,
)
from portfolio_dash.data_ingestion.broker.ir import EventKind, RawEvent, looks_like_cusip

_ZERO = Decimal(0)

#: Money tolerance. The broker states cents, so anything a correct parse produces lands on a
#: cent; half a cent is therefore "the same number" and nothing larger is. It also absorbs the
#: ~1e-27 tail of ``derive_reinvest_shares``'s exact division being multiplied back out.
#: A tolerance wide enough to hide a real error is not a tolerance, it is a switched-off check.
_MONEY_TOL: Final = Decimal("0.005")

#: Share tolerance. Reinvest share counts are fractional to many places by construction
#: (``amount / price``), so an exact comparison would fail on arithmetic that is right.
_SHARE_TOL: Final = Decimal("0.0000001")

#: How far a reinvestment may exceed its own net payout. **Measured, not chosen**: across 225
#: real DRIP groups exactly one overdraws, by exactly one cent — a broker pricing the
#: reinvestment before the withholding leg posts. The defect this check exists to catch is a
#: reinvest folded into the WRONG ``(date, symbol)`` bucket, which misses by the size of
#: another payout, i.e. dollars. One cent separates the two without hiding either.
_REINVEST_TOL: Final = Decimal("0.01")

Severity = Literal["blocking", "advisory"]

_BUY_SIDE: Final[frozenset[EventKind]] = frozenset({EventKind.BUY, EventKind.BUY_COVER})
_SELL_SIDE: Final[frozenset[EventKind]] = frozenset({EventKind.SELL, EventKind.SELL_SHORT})


@dataclass(frozen=True)
class ReconcileIssue:
    """One finding. ``refs`` names the exact source rows, because a total that disagrees is
    only actionable once it can point at lines."""

    code: str
    severity: Severity
    refs: tuple[str, ...]
    detail: str


@dataclass(frozen=True)
class ReconcileReport:
    """The verdict plus the figures a human needs to check it against the broker's own totals.

    The figures are part of the report on purpose: an owner reconciling against a statement
    wants the net cash and the per-symbol share deltas the import WILL produce, and deriving
    them a second time somewhere else is how two answers to one question appear.
    """

    issues: tuple[ReconcileIssue, ...]
    rows_in: int
    #: Net cash effect of everything that will be written, in the export's currency.
    cash_total: Decimal
    #: Per-symbol share delta the import will apply, under the derived-share rule.
    share_deltas: dict[str, Decimal]

    @property
    def blocking(self) -> tuple[ReconcileIssue, ...]:
        return tuple(i for i in self.issues if i.severity == "blocking")

    @property
    def advisory(self) -> tuple[ReconcileIssue, ...]:
        return tuple(i for i in self.issues if i.severity == "advisory")

    @property
    def ok(self) -> bool:
        """True when nothing BLOCKS. Advisories do not make an import unsafe — they make it
        incomplete, which is a different thing and is reported as one."""
        return not self.blocking

    def counts(self) -> dict[str, int]:
        """Issue count per code, sorted — the report line that carries no account data."""
        out: dict[str, int] = defaultdict(int)
        for issue in self.issues:
            out[issue.code] += 1
        return dict(sorted(out.items()))


class ReconcileFailed(Exception):
    """Raised by :func:`require_clean`. Carries the report so a caller can print the detail."""

    def __init__(self, report: ReconcileReport):
        self.report = report
        lines = [f"  {i.code}: {i.detail}" for i in report.blocking]
        super().__init__(
            f"import refused — {len(report.blocking)} blocking issue(s):\n" + "\n".join(lines)
        )


def _reinvest_cash(shares: Decimal | None, price: Decimal | None) -> Decimal:
    """Cash a reinvestment consumed, from the DERIVED share count."""
    if shares is None or price is None:
        return _ZERO
    return shares * price


def _check_suppressed(
    grouped: GroupedImport, by_ref: dict[str, RawEvent], issues: list[ReconcileIssue]
) -> None:
    """Re-add every dropped group from the ORIGINAL rows and require both dimensions zero.

    Both dimensions, because a corporate action is itself a paired out/in group whose cash is
    zero on both legs: an amount-only check waves a 3-for-1 split (−85 / +255 shares, $0 / $0)
    straight through and the position's basis is gone.
    """
    for group in grouped.suppressed:
        rows = [by_ref[r] for r in group.refs if r in by_ref]
        if len(rows) != len(group.refs):
            missing = sorted(set(group.refs) - {r.ref for r in rows})
            issues.append(
                ReconcileIssue(
                    "suppressed_ref_unknown", "blocking", tuple(missing),
                    f"a dropped group cites rows that are not in the input: {missing}",
                )
            )
            continue
        amount_sum = sum((r.amount for r in rows), _ZERO)
        quantity_sum = sum((_share_delta(r) for r in rows), _ZERO)
        if abs(amount_sum) > _MONEY_TOL or abs(quantity_sum) > _SHARE_TOL:
            issues.append(
                ReconcileIssue(
                    "suppressed_not_zero", "blocking", group.refs,
                    f"{group.key[0]} {group.key[1] or '(no symbol)'}: dropped as "
                    f"self-cancelling but the legs sum to amount {amount_sum}, "
                    f"quantity {quantity_sum} — a real event was about to be deleted",
                )
            )


def _check_priced_rows(grouped: GroupedImport, issues: list[ReconcileIssue]) -> None:
    """Every trade must satisfy ``amount == ±(quantity x price) - fees``.

    This is the only check that tests something outside our own head: it re-derives the cash
    from the three columns the broker printed beside it. It catches a mis-mapped column, a
    dropped minus sign, and a thousands separator eaten by a parser — the class of defect that
    produces a ledger full of plausible wrong numbers.

    Rows the broker priced at zero are skipped and reported nowhere: a transfer-in booked as a
    buy at no price is a real shape, and 'reconciles to zero' would be a vacuous pass.

    A trade that IS priced but moves no cash gets its own code, because it is not an
    arithmetic slip — it is a **free position**, the single most valuable thing that can enter
    a ledger silently. Observed once in a real export: a when-issued ADR re-badged as
    regular-way, where the broker books the new ticker at 50 x $170.00 for $0.00 and cancels
    the old side on a row that carries no symbol to pair it with. Neither suppression phase
    can match those two (one has no symbol, both have no cash), and that is the right
    outcome: the gate stops, names both lines, and a human decides. Inventing a rule from a
    shape seen once is how a converter starts guessing.
    """
    for e in grouped.trades:
        if e.price <= _ZERO or e.quantity == _ZERO:
            continue
        if e.amount == _ZERO:
            issues.append(
                ReconcileIssue(
                    "priced_row_no_cash", "blocking", (e.ref,),
                    f"{e.trade_date} {e.symbol} {e.kind.value}: {e.quantity} shares priced at "
                    f"{e.price} but the row moves no cash — this would book a position with "
                    "no basis. Resolve the row (a cancel, a re-badging) before importing",
                )
            )
            continue
        notional = e.quantity * e.price
        fees = abs(e.fees)
        if e.kind in _BUY_SIDE:
            expected = -(notional + fees)
        elif e.kind in _SELL_SIDE:
            expected = notional - fees
        else:  # pragma: no cover - trades[] holds only the four kinds above
            continue
        if abs(e.amount - expected) > _MONEY_TOL:
            issues.append(
                ReconcileIssue(
                    "priced_row_mismatch", "blocking", (e.ref,),
                    f"{e.trade_date} {e.symbol} {e.kind.value}: {e.quantity} x {e.price} "
                    f"less fees {fees} is {expected}, but the row states {e.amount}",
                )
            )


def _check_dividends(grouped: GroupedImport, issues: list[ReconcileIssue]) -> None:
    """Two invariants per folded distribution.

    * **Nothing reinvests more than it received.** A reinvestment costing more than the net
      payout means the fold pulled a DRIP purchase into the wrong ``(date, symbol)`` bucket,
      and the visible result would be a position that grew from money that never arrived.
    * **Withholding does not exceed the gross.** The withholding leg is stored positive and
      subtracted; a sign error on it doubles the payout instead of reducing it.
    """
    for d in grouped.dividends:
        reinvested = _reinvest_cash(d.reinvest_shares, d.reinvest_price)
        if reinvested > _ZERO and d.gross <= _ZERO:
            # A reinvestment with no payout leg in the file. The innocent cause is common and
            # unavoidable — the payout predates the export window, which is the same thing
            # ``prehistory_shares``'s soft detector keys on. The guilty cause is a payout
            # folded into a different bucket. They are indistinguishable from inside one
            # group, so this is reported and NOT blocked: blocking would make every export
            # whose first row for a symbol is a DRIP permanently unimportable.
            issues.append(
                ReconcileIssue(
                    "reinvest_without_payout", "advisory", d.refs,
                    f"{d.trade_date} {d.symbol}: {reinvested} reinvested with no payout leg "
                    "in the file — normal if the distribution predates the export window, "
                    "otherwise the payout landed in another group",
                )
            )
            continue
        if reinvested - d.net > _REINVEST_TOL:
            issues.append(
                ReconcileIssue(
                    "over_reinvested", "blocking", d.refs,
                    f"{d.trade_date} {d.symbol}: reinvested {reinvested} against a net "
                    f"payout of {d.net} (gross {d.gross} less withholding {d.withholding})",
                )
            )
        if d.gross > _ZERO and d.withholding - d.gross > _MONEY_TOL:
            issues.append(
                ReconcileIssue(
                    "withholding_exceeds_gross", "blocking", d.refs,
                    f"{d.trade_date} {d.symbol}: withholding {d.withholding} exceeds the "
                    f"gross payout {d.gross}",
                )
            )


def _cash_effect(grouped: GroupedImport) -> Decimal:
    """Net cash of everything the import will write.

    A folded dividend contributes ``net - reinvested`` with the reinvestment priced from the
    DERIVED share count, which is exactly where this total can legitimately differ by a
    fraction of a cent from the sum of the printed rows — and exactly why the caller compares
    it with a tolerance rather than for equality.
    """
    total = _ZERO
    for d in grouped.dividends:
        total += d.net - _reinvest_cash(d.reinvest_shares, d.reinvest_price)
    for bucket in (grouped.trades, grouped.cash, grouped.actions):
        for e in bucket:
            total += e.amount
    return total


def _share_deltas(grouped: GroupedImport) -> dict[str, Decimal]:
    """Per-symbol share delta the import will apply, under the derived-share rule."""
    out: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    for d in grouped.dividends:
        if d.reinvest_shares is not None:
            out[d.symbol] += d.reinvest_shares
    for e in grouped.trades:
        if e.symbol:
            out[e.symbol] += _share_delta(e)
    for e in grouped.actions:
        if e.symbol:
            out[e.symbol] += _share_delta(e)
    return {k: v for k, v in sorted(out.items())}


def _check_cash_conserved(
    events: list[RawEvent], grouped: GroupedImport, issues: list[ReconcileIssue]
) -> Decimal:
    """Cash in equals cash out, once the rows we deliberately do not write are set aside.

    Suppressed groups net zero (checked separately), so they are simply excluded here; the
    option legs and the unrouted rows are excluded too, and their cash is reported as its own
    advisory figure rather than folded into a total that would then silently balance.
    """
    dropped = {r for g in grouped.suppressed for r in g.refs}
    parked = {e.ref for e in grouped.options} | {e.ref for e in grouped.unrouted}
    expected = sum(
        (e.amount for e in events if e.ref not in dropped and e.ref not in parked), _ZERO
    )
    actual = _cash_effect(grouped)
    if abs(actual - expected) > _MONEY_TOL:
        issues.append(
            ReconcileIssue(
                "cash_not_conserved", "blocking", (),
                f"the rows to be written state {expected} of net cash, but the events they "
                f"were folded into come to {actual} — the fold moved money",
            )
        )
    return actual


def _check_shares_conserved(
    events: list[RawEvent], grouped: GroupedImport, issues: list[ReconcileIssue]
) -> dict[str, Decimal]:
    """Per-symbol shares in equals shares out, with reinvests measured the derived way.

    ⚠ The baseline is rebuilt from the RAW rows — a reinvest leg contributes
    ``derive_reinvest_shares(amount, price)``, computed here, not the count the fold stored.
    The first version read that count back out of the ``DividendEvent`` and compared it with
    itself, so it agreed unconditionally: a fold that invented 999 shares passed. A check
    whose two sides share a source is not a check, and this module's docstring promises the
    opposite, so the promise is what got fixed.
    """
    dropped = {r for g in grouped.suppressed for r in g.refs}
    parked = {e.ref for e in grouped.options} | {e.ref for e in grouped.unrouted}

    expected: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    for e in events:
        if not e.symbol or e.ref in dropped or e.ref in parked:
            continue
        if e.kind is EventKind.DRIP_BUY:
            shares = derive_reinvest_shares(e.amount, e.price)
            expected[e.symbol] += shares if shares is not None else _ZERO
        else:
            expected[e.symbol] += _share_delta(e)

    actual = _share_deltas(grouped)
    for symbol in sorted(set(expected) | set(actual)):
        want = expected.get(symbol, _ZERO)
        got = actual.get(symbol, _ZERO)
        if abs(got - want) > _SHARE_TOL:
            issues.append(
                ReconcileIssue(
                    "shares_not_conserved", "blocking", (),
                    f"{symbol}: the rows state a delta of {want} shares, the events to be "
                    f"written come to {got}",
                )
            )
    return actual


def _check_symbols(
    grouped: GroupedImport, by_ref: dict[str, RawEvent], issues: list[ReconcileIssue]
) -> set[str]:
    """No row may reach a ledger identified by a CUSIP. Returns the unresolved identifiers.

    A statement names the OLD security of a name change or a reverse split by its CUSIP and
    nothing else, and importing that verbatim creates an instrument the rest of the ledger has
    never heard of. The damage is quiet in the worst way: a corporate action whose
    ``from_symbol`` matches no holding replays as a **no-op**, so the split never applies, the
    share count stays on the pre-split basis, and every figure downstream is wrong while every
    screen looks fine.

    Blocking rather than advisory, and the reason is that it is *cheap to clear*: the row's own
    description carries the company name, so one ``--alias CUSIP=TICKER`` fixes it. A gate that
    is trivial to satisfy and expensive to skip belongs on the blocking side. (Where the SAME
    row also names the ticker in its text, no alias is needed at all — the adapter already
    prefers the ticker; see ``schwab.recover_symbol``.)
    """
    # (ref, description) per identifier — the description carries the company name, which is
    # what makes the alias answerable without leaving the report.
    unresolved: dict[str, tuple[str, str]] = {}

    def note(symbol: str, ref: str, description: str) -> None:
        if symbol and looks_like_cusip(symbol) and symbol not in unresolved:
            unresolved[symbol] = (ref, description.strip())

    for bucket in (grouped.trades, grouped.cash, grouped.actions):
        for e in bucket:
            note(e.symbol, e.ref, e.description)
    for d in grouped.dividends:
        # A folded dividend has no description of its own; borrow its first leg's, or the
        # message would say "no description" on the one path where the owner most needs the
        # company name to answer it.
        ref = d.refs[0] if d.refs else ""
        source = by_ref.get(ref)
        note(d.symbol, ref, source.description if source is not None else "")

    for symbol, (ref, description) in sorted(unresolved.items()):
        issues.append(
            ReconcileIssue(
                "cusip_unresolved", "blocking", (ref,),
                f"{symbol} is a CUSIP, not a ticker — the statement names this security only "
                f"by its identifier ({description or 'no description'}). Supply an alias "
                "mapping it to the ticker; importing it as-is creates a second instrument and "
                "any corporate action against it silently does nothing",
            )
        )
    return set(unresolved)


def _advise(
    events: list[RawEvent],
    grouped: GroupedImport,
    issues: list[ReconcileIssue],
    cusips: set[str],
) -> None:
    """Everything that will not be written, named row by row.

    Deliberately one issue per item rather than one per category: a category line ("14 option
    legs skipped") is read as a footnote, while fourteen lines with dates and refs on them are
    read as work.
    """
    for v in grouped.vetoed:
        issues.append(
            ReconcileIssue(
                "vetoed_group", "advisory", v.refs,
                f"{v.key[0]} {v.key[1] or '(no symbol)'}: {v.reason}",
            )
        )
    for e in grouped.unrouted:
        issues.append(
            ReconcileIssue(
                "unrouted_row", "advisory", (e.ref,),
                f"{e.trade_date} {e.symbol or e.option_symbol or '(no symbol)'} "
                f"{e.kind.value}: classified but belongs to no ledger — not imported",
            )
        )
    for e in grouped.options:
        issues.append(
            ReconcileIssue(
                "option_row_unsupported", "advisory", (e.ref,),
                f"{e.trade_date} {e.kind.value}: option legs are recognised but not "
                "supported (P3) — not imported",
            )
        )
    for first, dup in overlap_duplicates(events):
        issues.append(
            ReconcileIssue(
                "overlap_duplicate", "advisory", (first.ref, dup.ref),
                f"{dup.trade_date} {dup.symbol or '(no symbol)'} {dup.kind.value}: identical "
                "row in two source files — confirm whether the exports overlap or the event "
                "genuinely happened twice",
            )
        )
    for symbol, shares in prehistory_shares(events).items():
        if symbol in cusips:
            # Already blocking as an unresolved identifier, and "supply opening inventory" is
            # the wrong instruction for it: the shares are not missing, the NAME is. Emitting
            # both would send the owner to invent an opening position that must not exist.
            continue
        need = f"{shares} shares" if shares > _ZERO else "an unknown quantity"
        issues.append(
            ReconcileIssue(
                "prehistory_position", "advisory", (),
                f"{symbol}: held before the export window — {need} plus an original cost "
                "must come from opening inventory, or the basis will be wrong",
            )
        )


def reconcile(events: list[RawEvent], grouped: GroupedImport) -> ReconcileReport:
    """Check a grouped export against its own source rows. Never writes, never raises.

    Returns the verdict so a caller can print it whole; :func:`require_clean` is what turns a
    blocking verdict into a refusal. Separating them is deliberate — the converter shows the
    owner every issue at once, and an exception thrown at the first one would show one.
    """
    issues: list[ReconcileIssue] = []
    by_ref = {e.ref: e for e in events}

    try:
        account_for(events, grouped)
    except ValueError as exc:
        issues.append(ReconcileIssue("rows_lost", "blocking", (), str(exc)))

    _check_suppressed(grouped, by_ref, issues)
    _check_priced_rows(grouped, issues)
    _check_dividends(grouped, issues)
    cusips = _check_symbols(grouped, by_ref, issues)
    cash_total = _check_cash_conserved(events, grouped, issues)
    share_deltas = _check_shares_conserved(events, grouped, issues)
    _advise(events, grouped, issues, cusips)

    return ReconcileReport(
        issues=tuple(issues),
        rows_in=len(events),
        cash_total=cash_total,
        share_deltas=share_deltas,
    )


def require_clean(report: ReconcileReport) -> None:
    """Raise :class:`ReconcileFailed` unless the report has no blocking issue.

    All or nothing: there is no flag to import the part that reconciled. A partial import of
    a file whose arithmetic contradicts itself leaves a ledger nobody can rebuild, and the
    only honest recovery from that is a restore.
    """
    if not report.ok:
        raise ReconcileFailed(report)
