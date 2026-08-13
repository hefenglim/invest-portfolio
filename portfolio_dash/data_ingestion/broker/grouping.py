"""Fold a ``RawEvent`` stream into domain events. Broker-neutral: written once, for all.

Four transformations live here, and three of them exist because a plausible simpler version
was measured against a real export and found to destroy data.

1. :func:`suppress` — drop the paired self-cancelling rows. **Classification first,
   arithmetic as a veto**, keyed on ``(date, symbol)`` and never across symbols.
2. :func:`fold_dividends` — collapse the broker's 3-row DRIP group into one dividend, with
   the reinvested share count DERIVED from ``amount / price``.
3. :func:`derive_ratio` — recover a corporate action's rational ratio from its two legs.
4. :func:`prehistory_shares` — find the positions that predate the export, by the UNION of
   two detectors that each miss what the other catches.
"""

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from fractions import Fraction

from portfolio_dash.data_ingestion.broker.ir import (
    CORPORATE_ACTION_KINDS,
    LEDGER_KINDS,
    OPTION_KINDS,
    SUPPRESSIBLE_KINDS,
    EventKind,
    RawEvent,
    looks_like_cusip,
)
from portfolio_dash.shared.corporate_actions import CorporateActionKind

_ZERO = Decimal(0)

# Kinds that ADD shares to a position, and kinds that remove them — the replay used by the
# pre-history detector. Deliberately not "sign of quantity": the broker prints an unsigned
# quantity on some rows and a signed one on others, so the KIND is the reliable direction.
_ADDS_SHARES = frozenset({EventKind.BUY, EventKind.DRIP_BUY, EventKind.BUY_COVER})
_REMOVES_SHARES = frozenset({EventKind.SELL, EventKind.SELL_SHORT})


@dataclass(frozen=True)
class SuppressedGroup:
    """One dropped group, with the evidence for dropping it."""

    key: tuple[date, str]
    kinds: tuple[EventKind, ...]
    refs: tuple[str, ...]
    amount_sum: Decimal
    quantity_sum: Decimal


@dataclass(frozen=True)
class VetoedGroup:
    """A group classified as suppressible whose arithmetic REFUSED the drop.

    Not an error and not a silent keep: the rows stay in the stream and the converter's
    report names them, because "we classified this as noise and the numbers disagree" is
    the one finding a human has to look at.
    """

    key: tuple[date, str]
    kinds: tuple[EventKind, ...]
    refs: tuple[str, ...]
    amount_sum: Decimal
    quantity_sum: Decimal
    reason: str


@dataclass(frozen=True)
class DividendEvent:
    """One folded distribution: the payout, its withholding, and any reinvestment."""

    trade_date: date
    symbol: str
    gross: Decimal
    withholding: Decimal
    reinvest_shares: Decimal | None
    reinvest_price: Decimal | None
    refs: tuple[str, ...]

    @property
    def is_drip(self) -> bool:
        return self.reinvest_shares is not None

    @property
    def net(self) -> Decimal:
        return self.gross - self.withholding


@dataclass
class GroupedImport:
    """Everything one export becomes, including what it could not become."""

    dividends: list[DividendEvent] = field(default_factory=list)
    trades: list[RawEvent] = field(default_factory=list)
    cash: list[RawEvent] = field(default_factory=list)
    actions: list[RawEvent] = field(default_factory=list)
    options: list[RawEvent] = field(default_factory=list)
    suppressed: list[SuppressedGroup] = field(default_factory=list)
    vetoed: list[VetoedGroup] = field(default_factory=list)
    #: Rows that survived suppression and belong to no ledger — in practice a vetoed noise
    #: row whose partner was never found. They are LISTED, never discarded: the first
    #: version of :func:`group_events` fell off the end of an if/elif chain and lost two
    #: rows of a 1,375-row export without a word, which is the exact failure mode rule 7
    #: and this whole package exist to prevent.
    unrouted: list[RawEvent] = field(default_factory=list)
    #: Rows absorbed into another event rather than routed on their own — today, interest
    #: withholding netted into its credit (:func:`net_interest_withholding`). Their money is
    #: in the ledger, inside the row that absorbed them; they are listed separately so
    #: :func:`account_for` can still prove every input row landed somewhere.
    folded: list[RawEvent] = field(default_factory=list)


def _share_delta(e: RawEvent) -> Decimal:
    """The signed share movement of one row, for the zero-sum check.

    The broker signs a row's quantity in only some cases, so the direction is taken from the
    strongest available evidence rather than from one column:

    * **money moved** — shares go the opposite way to the cash. Money out is shares in. This
      covers every trade-shaped row including a cancel, whose printed quantity is positive on
      both legs while its amount is the exact negation.
    * **no money moved** — a corporate-action or journal leg, where the broker DOES sign the
      quantity (``-85`` out, ``+255`` in). Take it as printed.

    Reading ``+quantity`` for anything not obviously a buy or a sell was the first version,
    and it made a ``Cancel Buy`` and its buy sum to ``+200`` instead of ``0`` — vetoing a
    group that should have been dropped.
    """
    if e.amount > _ZERO:
        return -abs(e.quantity)
    if e.amount < _ZERO:
        return abs(e.quantity)
    return e.quantity


def _bucket(events: list[RawEvent]) -> dict[tuple[date, str], list[RawEvent]]:
    """Group by ``(trade_date, symbol)``.

    ⚠ **Never across symbols.** Measured on a real export: grouping by date alone made the
    zero-sum rule drop 180 rows, **8 of which were real corporate actions** — three 1-for-1
    ticker exchanges and their option legs. Grouping by ``(date, symbol)`` dropped 168 rows
    and destroyed none. A 1-for-1 exchange nets zero in BOTH dimensions (shares out equal
    shares in, no cash on either leg), so it is arithmetically indistinguishable from an
    internal journal; the only thing that separates them is that they are not the same
    security moving, and the key is what encodes that.
    """
    out: dict[tuple[date, str], list[RawEvent]] = defaultdict(list)
    for e in events:
        out[(e.trade_date, e.symbol or e.option_symbol)].append(e)
    return dict(out)


def suppress(
    events: list[RawEvent],
) -> tuple[list[RawEvent], list[SuppressedGroup], list[VetoedGroup]]:
    """Remove paired self-cancelling rows. Returns ``(kept, dropped, vetoed)``.

    **Classification is primary; arithmetic is the guard, not the decision.** A group may be
    dropped only if it was first classified as noise by its ``(action, description)`` pair;
    the zero-sum check then runs as a veto. That ordering is forced, not preferred:

    * *Arithmetic alone cannot decide.* One broker action carries 14 meanings, and its two
      largest — 145 withholding-tax rows to keep, 126 null journals to drop — are separated
      only by free text. An amount-keyed rule deletes the tax or keeps the journals.
    * *Classification alone is not safe.* A rule that drops rows because of what they are
      called is one typo away from deleting real money, so the sum must still agree.

    The zero-sum check covers **amount AND share quantity**, because a corporate action is
    itself a paired out/in group whose cash is zero on both legs: an amount-only check
    passes a 3-for-1 split (−85 / +255 shares, $0 / $0) and drops it silently.

    Two shapes are recognised, both within one ``(date, symbol)`` bucket:

    * the suppressible rows net to zero **among themselves** — an internal journal, an ACAT
      pair, a mark-to-market round trip;
    * a suppressible row is the exact **reversal of one kept row** — a ``Cancel Buy`` and the
      buy it cancels, a ``Reinvestment Adj`` and the DRIP it corrects. Rule 4: the cancelled
      order goes with its cancel, or the ledger keeps a trade that never happened.
    """
    dropped: list[SuppressedGroup] = []
    vetoed: list[VetoedGroup] = []
    removed: set[str] = set()          # `file:line` ref of every row leaving the stream

    # --- phase 1: cash-bearing REVERSALS (a cancel, a re-booking) ---------------------
    #
    # Matched per SYMBOL across dates, not within one day: a cancel is often dated after the
    # order it cancels (observed — one of the two real cancel pairs in the assessed export
    # straddles a date boundary, and a same-day-only rule left it orphaned in the ledger).
    #
    # ⚠ Restricted to rows that MOVE CASH, and that restriction is the load-bearing part.
    # A reversal is recognised by "one row exactly negates another", which on a ZERO-cash
    # row degenerates: ``0 == -0`` matches everything, so a zero-cash journal would happily
    # adopt a corporate-action leg as its "partner" and drag a real event out of the ledger
    # with it. Measured: without this restriction two 1-for-1 ticker exchanges were pulled
    # into journal groups — the exact destruction ``_bucket``'s key exists to prevent,
    # arriving by a second route. A zero-cash noise row can only be suppressed in phase 2,
    # against other noise.
    by_symbol: dict[str, list[RawEvent]] = defaultdict(list)
    for e in events:
        by_symbol[e.symbol or e.option_symbol].append(e)

    for symbol, rows in sorted(by_symbol.items()):
        for n in rows:
            if n.kind not in SUPPRESSIBLE_KINDS or n.amount == _ZERO:
                continue
            if n.ref in removed:
                continue
            partner = next(
                (
                    o
                    for o in rows
                    if o.ref not in removed
                    and o.ref != n.ref
                    and o.kind not in SUPPRESSIBLE_KINDS
                    and o.amount == -n.amount
                    and o.quantity == n.quantity
                ),
                None,
            )
            if partner is None:
                continue
            removed.update({n.ref, partner.ref})
            dropped.append(
                SuppressedGroup(
                    key=(n.trade_date, symbol),
                    kinds=(n.kind, partner.kind),
                    refs=(n.ref, partner.ref),
                    amount_sum=_ZERO,
                    quantity_sum=_ZERO,
                )
            )

    # --- phase 2: NOISE-ONLY groups that net to zero among themselves -----------------
    for key, bucket in sorted(_bucket(events).items()):
        noise = [
            e for e in bucket
            if e.kind in SUPPRESSIBLE_KINDS and e.ref not in removed
        ]
        if not noise:
            continue
        amount_sum = sum((e.amount for e in noise), _ZERO)
        quantity_sum = sum((_share_delta(e) for e in noise), _ZERO)
        refs = tuple(e.ref for e in noise)
        kinds = tuple(dict.fromkeys(e.kind for e in noise))

        if amount_sum == _ZERO and quantity_sum == _ZERO:
            removed.update(refs)
            dropped.append(SuppressedGroup(key, kinds, refs, amount_sum, quantity_sum))
            continue

        vetoed.append(
            VetoedGroup(
                key, kinds, refs, amount_sum, quantity_sum,
                reason=(
                    "classified as a self-cancelling group but the legs do not net to zero "
                    f"(amount {amount_sum}, quantity {quantity_sum}) — kept, not dropped"
                ),
            )
        )

    kept = sorted(
        (e for e in events if e.ref not in removed),
        key=lambda e: (e.trade_date, e.line_no),
    )
    return kept, dropped, vetoed


def derive_reinvest_shares(net: Decimal, price: Decimal) -> Decimal | None:
    """Shares bought by a reinvestment, from ``|net| / price``.

    ⚠ **The printed quantity is NOT the authority.** Measured on a real export: 125 of 227
    reinvest rows fail ``quantity x price == amount``, and in every case the printed
    quantity is exactly ``round(|amount| / price)`` at the 3–4 dp the column displays.
    Amount and price are what the broker actually asserts; the quantity is a rounded view of
    them. Trusting it drifts the share count away from the statement, in the same direction,
    for every reinvestment of a multi-year DRIP history.

    Same principle as ``close_raw`` / ``close`` in ``data-and-pricing.md``: store what the
    source asserts and derive the rest, rather than storing a derived value twice and
    letting the copies disagree.
    """
    if price <= _ZERO:
        return None
    return abs(net) / price


def fold_dividends(events: list[RawEvent]) -> tuple[list[DividendEvent], list[RawEvent]]:
    """Collapse distribution legs into one event per ``(date, symbol)``.

    A US reinvested dividend arrives as three rows — the gross payout, the withholding
    adjustment, and the reinvest purchase — and the ledger's ``dividends`` table already has
    exactly the shape they fold into (``gross`` / ``withholding`` / ``reinvest_shares`` /
    ``reinvest_price``). Returns ``(folded, remaining)``; *remaining* is every event that was
    not part of a distribution, unchanged and in order.
    """
    dist_kinds = {
        EventKind.DIVIDEND, EventKind.CAPGAIN_DIST,
        EventKind.WITHHOLDING_TAX, EventKind.DRIP_BUY,
    }
    folded: list[DividendEvent] = []
    remaining = [e for e in events if e.kind not in dist_kinds or not e.symbol]

    buckets: dict[tuple[date, str], list[RawEvent]] = defaultdict(list)
    for e in events:
        if e.kind in dist_kinds and e.symbol:
            buckets[(e.trade_date, e.symbol)].append(e)

    for (day, symbol), rows in sorted(buckets.items()):
        gross = sum(
            (r.amount for r in rows
             if r.kind in {EventKind.DIVIDEND, EventKind.CAPGAIN_DIST}),
            _ZERO,
        )
        # Withholding is printed as a negative cash effect; the ledger stores it positive.
        withholding = -sum(
            (r.amount for r in rows if r.kind is EventKind.WITHHOLDING_TAX), _ZERO
        )
        reinvests = [r for r in rows if r.kind is EventKind.DRIP_BUY]
        price = reinvests[0].price if reinvests else None
        shares = (
            derive_reinvest_shares(
                sum((r.amount for r in reinvests), _ZERO), price
            )
            if reinvests and price is not None
            else None
        )
        folded.append(
            DividendEvent(
                trade_date=day, symbol=symbol,
                gross=gross, withholding=withholding,
                reinvest_shares=shares, reinvest_price=price,
                refs=tuple(r.ref for r in rows),
            )
        )
    return folded, remaining


def net_interest_withholding(
    events: list[RawEvent],
) -> tuple[list[RawEvent], list[RawEvent]]:
    """Net a symbol-less withholding row into the interest credit it belongs to.

    Returns ``(rewritten, absorbed)``. *rewritten* is the whole stream with each affected
    interest credit reduced to its NET amount; *absorbed* is the withholding rows that went
    into them, kept so conservation stays provable.

    A broker withholds tax on **credit interest** as well as on dividends, and prints it with
    the same action code and no symbol — measured on a real export: three rows reading
    ``SCHWAB1 INT 12/30-01/29``, each sitting beside a ``Credit Interest`` row of identical
    description on the same date. :func:`fold_dividends` cannot take them (it keys on a
    symbol) and no cash kind means "tax withheld", so before this they fell out of the
    conversion entirely and the cash balance was short by the tax.

    Recording the **net received** is the ledger's existing convention for a cash distribution
    it cannot decompose (``domain-ledger.md``, MY cash dividends). Booking the tax as a
    ``BROKER_FEE`` was the alternative and is rejected: it is not a fee, and it would show up
    under 券商費用 in every report that breaks costs down.

    Keyed on ``(date, no symbol)`` and requiring exactly one interest credit that day, so an
    unmatched withholding row is left alone to be reported rather than guessed at.
    """
    absorbed: list[RawEvent] = []
    by_day: dict[date, list[RawEvent]] = defaultdict(list)
    for e in events:
        if not e.symbol and not e.option_symbol:
            by_day[e.trade_date].append(e)

    netted: dict[str, Decimal] = {}
    for _day, rows in sorted(by_day.items()):
        credits = [e for e in rows if e.kind is EventKind.INTEREST_INCOME]
        taxes = [e for e in rows if e.kind is EventKind.WITHHOLDING_TAX]
        if len(credits) != 1 or not taxes:
            continue
        credit = credits[0]
        withheld = sum((abs(t.amount) for t in taxes), _ZERO)
        if withheld > credit.amount:
            continue  # not the shape this handles; leave both for the report
        netted[credit.ref] = credit.amount - withheld
        absorbed.extend(taxes)

    absorbed_refs = {e.ref for e in absorbed}
    rewritten = [
        e.model_copy(update={"amount": netted[e.ref]}) if e.ref in netted else e
        for e in events
        if e.ref not in absorbed_refs
    ]
    return rewritten, absorbed


def derive_ratio(shares_out: Decimal, shares_in: Decimal) -> tuple[int, int]:
    """``(ratio_to, ratio_from)`` as POSITIVE INTEGERS, from a corporate action's two legs.

    The export states the share DELTA, never the ratio, so the ratio is recovered from the
    legs: 85 out and 255 in is 3-for-1. Both terms must be positive integers — D14 rejects a
    decimal ratio outright, because a rounded quotient (``0.2857`` for 2-for-7) re-creates
    the 賣超 cascade the corporate-action feature exists to prevent. ``Fraction`` reduces
    exactly, so a 1-for-1 ticker exchange comes back as ``(1, 1)`` rather than ``(100, 100)``.

    Raises ``ValueError`` on a leg of zero: a ratio cannot be recovered from it, and
    inventing one is how a wrong number that looks right gets into a ledger.
    """
    if shares_out <= _ZERO or shares_in <= _ZERO:
        raise ValueError(
            f"cannot derive a ratio from legs out={shares_out} in={shares_in} "
            "— both must be positive; supply the ratio explicitly instead"
        )
    ratio = Fraction(shares_in) / Fraction(shares_out)
    return ratio.numerator, ratio.denominator


def prehistory_shares(events: list[RawEvent]) -> dict[str, Decimal]:
    """Symbols whose position predates the export, and how many shares are missing.

    **Two detectors, UNIONED** — each misses what the other catches:

    * *the running balance goes negative* — a **hard** failure if unhandled: the replay trips
      the sticky 賣超 guard and DISCARDS that position's cost basis permanently. The shares
      needed are ``-min(balance)``, not the final balance: a back-dated dip that a later buy
      covers is invisible to a net-only check.
    * *the first event is a sell or a reinvest* — **soft**: the basis is silently wrong with
      no alarm at all. A DRIP implies a holding, so a symbol whose history opens with one was
      already held.

    "First event is a sell" alone is insufficient — observed: a symbol that buys twice, sells
    more than it bought, and nets negative. "Balance goes negative" alone misses a position
    held throughout and never oversold.

    ⚠ **Corporate actions are part of the balance walk, and leaving them out was a measured
    defect.** Shares also arrive by spinoff, split and exchange, and a walk that counts only
    trades sees those shares appear from nowhere: the later sell drives the balance negative
    and the symbol is reported as "held before the window". Measured on a real export, that
    fabricated SIX opening positions — a spin-off, a 10-for-1 split, two SPAC tickers and two
    CUSIP-named exchange legs — each of which would have
    invited the owner to invent an opening cost for shares the file already explains. The
    action legs print a SIGNED quantity against zero cash, so :func:`_share_delta` reads them
    correctly without a per-kind sign rule.

    ⚠ A **third** case exists that neither detector can see: a position already SHORT before
    the window, whose balance never turns negative and whose first event is a buy. It is
    found only by cross-checking split deltas against the broker's transfer quantities, and
    is deliberately not implemented here — the converter's report says so rather than
    implying the list is complete. (Measured against a hand-built ground truth: this is why
    the detector's output is not the same SET as the owner's opening-inventory file even
    when the two happen to have the same COUNT.)
    """
    walked = _ADDS_SHARES | _REMOVES_SHARES | CORPORATE_ACTION_KINDS
    by_symbol: dict[str, list[RawEvent]] = defaultdict(list)
    for e in events:
        if e.symbol and e.kind in walked:
            by_symbol[e.symbol].append(e)

    needed: dict[str, Decimal] = {}
    for symbol, rows in sorted(by_symbol.items()):
        rows.sort(key=lambda e: (e.trade_date, e.line_no))
        balance = _ZERO
        low = _ZERO
        for e in rows:
            if e.kind in _ADDS_SHARES:
                balance += e.quantity
            elif e.kind in _REMOVES_SHARES:
                balance -= e.quantity
            else:
                balance += _share_delta(e)
            low = min(low, balance)
        opens_held = rows[0].kind in {EventKind.SELL, EventKind.DRIP_BUY}
        if low < _ZERO:
            needed[symbol] = -low
        elif opens_held:
            # Held but never oversold: the share count cannot be recovered from the file, so
            # 0 marks "a cost is required here" without inventing a quantity to go with it.
            needed[symbol] = _ZERO
    return needed


@dataclass(frozen=True)
class ActionPair:
    """One corporate action, assembled from the leg(s) the statement printed.

    ``ratio_to`` / ``ratio_from`` are ``None`` when the file does not determine them. That is
    a real state, not a failure: the converter writes such a row to a worksheet with the two
    fields blank rather than guessing, because a guessed ratio is D14's rejected decimal in
    another costume — it replays as a wrong share count that looks right.
    """

    kind: CorporateActionKind
    trade_date: date
    from_symbol: str
    to_symbol: str
    ratio_to: int | None
    ratio_from: int | None
    refs: tuple[str, ...]
    #: Why the ratio is missing, when it is. Printed beside the blank fields.
    needs: str = ""


#: How a broker's leg kind maps onto the ledger's three-value vocabulary. A REVERSE split is a
#: SPLIT whose ratio is less than one; a NAME_CHANGE is an EXCHANGE at 1:1. The ledger has no
#: separate kinds for either, by design (``shared/corporate_actions.py``).
_ACTION_FAMILY: dict[EventKind, CorporateActionKind] = {
    EventKind.SPLIT: CorporateActionKind.SPLIT,
    EventKind.REVERSE_SPLIT: CorporateActionKind.SPLIT,
    EventKind.EXCHANGE: CorporateActionKind.EXCHANGE,
    EventKind.NAME_CHANGE: CorporateActionKind.EXCHANGE,
    EventKind.SPINOFF: CorporateActionKind.SPINOFF,
}


def pair_actions(
    events: list[RawEvent], actions: list[RawEvent]
) -> tuple[list[ActionPair], list[RawEvent]]:
    """Assemble corporate-action legs into ledger rows. Returns ``(pairs, unpaired)``.

    The statement prints **deltas, never ratios**, and it prints a different number of legs
    per kind — measured on a real export:

    * **exchange / name change / reverse split** — TWO legs on one date, in DIFFERENT symbols
      (``-2`` of one ticker against ``+2`` of another; ``-100`` against ``+10`` on a reverse
      split, where even the CUSIP changes). Paired here, and the ratio follows exactly from the
      two quantities.
    * **forward split** — ONE leg, the delta only (``+6`` shares). The ratio is **never derived**
      and always left blank. ⚠ It looks derivable: replay the file to get the holding the
      delta applied to and the ratio falls out. That was implemented, measured against the two
      real one-leg splits, and **removed** — it produced ``4:1`` for one (right) and
      ``109:24`` for the other (a 3-for-1 split). The wrong one is wrong because that position
      predates the export, which is the third pre-history case
      :func:`prehistory_shares` documents as undetectable; the replay cannot know it is
      incomplete, so it answers confidently either way. A ratio that is right half the time
      and silent about which half is worse than a blank field: the blank costs the owner one
      entry, the guess costs a share count that no screen will ever question.
    * **spin-off** — ONE leg naming the CHILD (``+18`` shares of it). The file never names the
      parent,
      so neither the ``from_symbol`` nor the ratio can be recovered. Always left for the owner.

    Cross-symbol pairing is allowed here and refused in :func:`_bucket` for opposite reasons:
    suppression must never join two symbols because a 1-for-1 exchange nets to zero and would
    be deleted; pairing must join them because that is what an exchange IS. The safety comes
    from the pairing being unambiguous — exactly one out-leg and one in-leg of that family on
    that date — and from anything else being handed back unpaired rather than matched
    arbitrarily.
    """
    pairs: list[ActionPair] = []
    used: set[str] = set()
    buckets: dict[tuple[date, CorporateActionKind], list[RawEvent]] = defaultdict(list)
    for e in actions:
        family = _ACTION_FAMILY.get(e.kind)
        if family is not None:
            buckets[(e.trade_date, family)].append(e)

    for (day, family), legs in sorted(buckets.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        outs = [e for e in legs if _share_delta(e) < _ZERO]
        ins = [e for e in legs if _share_delta(e) > _ZERO]

        if len(outs) == 1 and len(ins) == 1 and family is not CorporateActionKind.SPINOFF:
            out, into = outs[0], ins[0]
            shares_out, shares_in = -_share_delta(out), _share_delta(into)
            try:
                to_term, from_term = derive_ratio(shares_out, shares_in)
            except ValueError:
                continue
            used.update({out.ref, into.ref})
            pairs.append(
                ActionPair(
                    kind=family, trade_date=day,
                    from_symbol=out.symbol, to_symbol=into.symbol,
                    ratio_to=to_term, ratio_from=from_term,
                    refs=(out.ref, into.ref),
                )
            )
            continue

        for leg in legs:
            delta = _share_delta(leg)
            if family is CorporateActionKind.SPINOFF:
                used.add(leg.ref)
                pairs.append(
                    ActionPair(
                        kind=family, trade_date=day,
                        from_symbol="", to_symbol=leg.symbol,
                        ratio_to=None, ratio_from=None, refs=(leg.ref,),
                        needs=(
                            f"the statement names only the child ({leg.symbol}, {delta} "
                            "shares) — supply the parent symbol and the ratio"
                        ),
                    )
                )
                continue
            if family is CorporateActionKind.SPLIT:
                used.add(leg.ref)
                pairs.append(
                    ActionPair(
                        kind=family, trade_date=day,
                        from_symbol=leg.symbol, to_symbol=leg.symbol,
                        ratio_to=None, ratio_from=None, refs=(leg.ref,),
                        needs=(
                            f"a one-leg split stating only the delta ({delta} shares) — "
                            "supply the ratio; it is not derivable from the file"
                        ),
                    )
                )

    unpaired = [e for e in actions if e.ref not in used]
    return pairs, unpaired


def infer_cusip_aliases(
    events: list[RawEvent],
) -> tuple[dict[str, str], dict[str, set[str]]]:
    """Learn ``CUSIP -> ticker`` from the export itself. Returns ``(resolved, ambiguous)``.

    A statement can name one security both ways: in a real export 27 rows put its CUSIP in the
    ``Symbol`` column and its ticker in the text, and one row — the reverse split — names only
    the CUSIP. Imported verbatim that is TWO instruments, so the position's cost basis
    splits in half and the split lands on the half that has no shares.

    The mapping is **derived from the file, not guessed**: a CUSIP is resolved only when the
    rows that do name a ticker all name the SAME one. A CUSIP that maps to two tickers is
    returned as *ambiguous* and left alone, because that is either a data error or a
    re-used identifier, and both deserve a human rather than a coin toss.

    Deliberately NOT applied inside :func:`group_events`: a transformation that silently
    renames securities is one nobody can audit. The caller applies it with
    :func:`apply_aliases` and prints what it applied.

    Broker-neutral because it reads two fields the adapter already reconciled — the statement's
    printed ``broker_symbol`` and the ``symbol`` the adapter resolved it to. Re-parsing the
    description here would put one broker's narrative format in the shared layer.
    """
    named: dict[str, set[str]] = defaultdict(set)
    for e in events:
        printed = e.broker_symbol.strip()
        if printed and looks_like_cusip(printed) and e.symbol and e.symbol != printed:
            named[printed].add(e.symbol)

    resolved = {c: next(iter(t)) for c, t in sorted(named.items()) if len(t) == 1}
    ambiguous = {c: t for c, t in sorted(named.items()) if len(t) > 1}
    return resolved, ambiguous


def apply_aliases(events: list[RawEvent], aliases: Mapping[str, str]) -> list[RawEvent]:
    """Rewrite every ``symbol`` through *aliases*. Frozen events, so this copies."""
    if not aliases:
        return events
    return [
        e.model_copy(update={"symbol": aliases[e.symbol]}) if e.symbol in aliases else e
        for e in events
    ]


def overlap_duplicates(events: list[RawEvent]) -> list[tuple[RawEvent, RawEvent]]:
    """Rows that appear identically in TWO source files — reported, never dropped.

    A broker hands over consecutive exports with **overlapping date windows**, so one event
    can be printed in both. Suppression cannot see this: the two copies are in different
    files, are not a self-cancelling pair, and nothing about either row is wrong.

    Reported rather than removed, because the pair is genuinely ambiguous from inside the
    data — two deposits of the same amount on the same day is a thing that happens. The
    assessed export contained exactly one such pair and a human confirmed it; that is the
    right shape for the decision. (The ledger has a second net: B3's import-provenance hash
    catches a repeat when the two files are imported as separate batches. It does NOT catch
    it when they are merged into one file first, which is why this exists.)
    """
    seen: dict[tuple[date, str, str, Decimal, Decimal], RawEvent] = {}
    pairs: list[tuple[RawEvent, RawEvent]] = []
    for e in sorted(events, key=lambda x: (x.source_file, x.line_no)):
        key = (
            e.trade_date, e.symbol or e.option_symbol, e.kind.value, e.quantity, e.amount
        )
        first = seen.get(key)
        if first is None:
            seen[key] = e
        elif first.source_file != e.source_file:
            pairs.append((first, e))
    return pairs


def group_events(events: list[RawEvent]) -> GroupedImport:
    """The whole fold: suppress, then route what survives to its ledger.

    **Every input row lands somewhere** — dropped, folded into a dividend, routed to a
    ledger, parked as an option, or listed as unrouted. :func:`account_for` asserts it, and
    the reconciler (B6) runs that assertion as an import gate. A converter that can lose a
    row quietly is one whose output cannot be trusted to be complete, which is the whole
    reason the ledger did not already have a broker importer.
    """
    kept, dropped, vetoed = suppress(events)
    dividends, rest = fold_dividends(kept)
    rest, absorbed = net_interest_withholding(rest)
    result = GroupedImport(
        dividends=dividends, suppressed=dropped, vetoed=vetoed, folded=absorbed
    )
    trade_kinds = {
        EventKind.BUY, EventKind.SELL, EventKind.SELL_SHORT, EventKind.BUY_COVER
    }
    for e in rest:
        if e.kind in OPTION_KINDS or e.is_option:
            result.options.append(e)
        elif e.kind in CORPORATE_ACTION_KINDS:
            result.actions.append(e)
        elif e.kind in trade_kinds:
            result.trades.append(e)
        elif e.kind in LEDGER_KINDS:
            result.cash.append(e)
        else:
            result.unrouted.append(e)
    return result


def account_for(events: list[RawEvent], grouped: GroupedImport) -> None:
    """Raise unless every input row is accounted for exactly once.

    Line numbers, not counts: a count can balance while two rows swap places. The check is
    cheap and it is the difference between "the importer handled 1,375 rows" and "the
    importer handled 1,375 rows and can prove which ones".
    """
    seen: list[str] = [
        *(r for g in grouped.suppressed for r in g.refs),
        *(r for d in grouped.dividends for r in d.refs),
        *(e.ref for e in grouped.trades),
        *(e.ref for e in grouped.cash),
        *(e.ref for e in grouped.actions),
        *(e.ref for e in grouped.options),
        *(e.ref for e in grouped.unrouted),
        *(e.ref for e in grouped.folded),
    ]
    expected = {e.ref for e in events}
    got = set(seen)
    if len(seen) != len(got):
        duplicated = sorted({n for n in seen if seen.count(n) > 1})
        raise ValueError(f"rows accounted for more than once: {duplicated}")
    if got != expected:
        raise ValueError(
            f"rows lost: {sorted(expected - got)}; rows invented: {sorted(got - expected)}"
        )
