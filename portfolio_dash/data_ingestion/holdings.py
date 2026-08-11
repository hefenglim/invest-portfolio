"""Holdings computation: aggregate current shares across ALL share-bearing ledgers.

Fixed 2026-07-02: the original implementation summed only the transactions table,
so a position held as opening inventory (期初) or grown by stock/DRIP dividends
looked smaller than it is — selling it raised FALSE oversell warnings, and the
instruments/input "held" flags undercounted. Shares come from four places and all
must count: opening inventory + buys − sells + non-cash dividend shares.

2026-07-03 (R4 dividend inbox): added the dated variant ``shares_on`` — shares
held going INTO a date (events strictly earlier count), the ex-date entitlement
rule for dividend detection.

2026-08-10 (W4, spec §6.2 / D23): this is the SECOND, parallel share-count path — it
computes independently of ``portfolio.cost_basis.build_book`` and feeds the date-aware
oversell guard, the corporate-action guards, and the symbol pickers. Corporate actions had
to reach it or the two disagree, and the disagreement is not symmetric: the replay says a
position holds the post-split count while the validator still sees the pre-split count, so
a **legitimate entry is blocked** and it looks like a bug in the owner's data. Measured on
``HEAD`` before this change: ``buy ABC → EXCHANGE ABC→XYZ → SPLIT XYZ`` had the replay
reporting 100 XYZ while ``shares_through`` returned 0 and validation hard-rejected the
second action of the chain as 「沒有持倉」.

Three things about the shape of the fix, each of which is load-bearing:

1. **A symbol with no corporate action never enters the new code** (D38 invariant 1). Not
   "the walk agrees with the old sum" — the walk does not RUN. See :func:`_shares_at`.
2. **The walk cannot be built on :func:`_shares_until` with a shifted ``before``.** A share
   query is a cut in ``(date, EventPriority)``, not a date: ``EventPriority.OPENING`` (0)
   precedes ``CORPORATE_ACTION`` (10), so opening inventory dated ON an action's date is
   PRE-action (D3) while a transaction dated on it is POST-action. One uniform ``<`` gets
   the opening wrong, and the case is enterable today (audit F-18).
3. **The recursion asks for the source's state STRICTLY BEFORE the actions of its own
   date.** Written inclusively it re-enters itself and hangs (D23). The cut form makes that
   a property of the ordering rather than a rule to remember.
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from portfolio_dash.data_ingestion.store import list_corporate_actions
from portfolio_dash.shared.corporate_actions import (
    ActionIndex,
    CorporateAction,
    CorporateActionKind,
    apply_ratio,
)
from portfolio_dash.shared.ledger_events import EventPriority
from portfolio_dash.shared.models.enums import CASH_DIVIDEND_TYPES, DividendType, Side

_ZERO = Decimal("0")

# F-12 — ONE definition of "a dividend that ADDS SHARES", derived from the single shared
# authority instead of restated. The SQL used to say ``type != 'CASH'``, which admits
# ``NET`` (MY 單層淨額) — but ``build_book`` books NET as CASH, so a NET row carrying a
# ``reinvest_shares`` value inflated this path's count and not the replay's, and §6.3's
# ``corporate_delta`` (defined as action-aware − naive) would have attributed the gap to
# corporate actions. Expressed as a POSITIVE list on purpose: a DividendType added later is
# excluded until somebody classifies it, rather than silently admitted by a ``!=``.
_REINVEST_DIVIDEND_TYPES = frozenset(DividendType) - CASH_DIVIDEND_TYPES
_REINVEST_TYPES_SQL = ", ".join(f"'{t.value}'" for t in sorted(_REINVEST_DIVIDEND_TYPES))

# Insurance, not the termination mechanism (D23, trap #20). The walk descends strictly in
# the (date, priority) order and D15 forbids two same-date actions whose symbol sets
# intersect, so it terminates by construction; the cap converts a corrupted or hand-edited
# ledger — or a future relaxation of D15 — from a hang into a diagnosable, flagged result.
MAX_ACTION_DEPTH = 32

# A cut in the replay's own ``(date, EventPriority)`` ordering: an event counts iff it sorts
# strictly BEFORE the cut. ``None`` means "no cut" — every event counts.
_Cut = tuple[date, int] | None


def _shares_until(
    conn: sqlite3.Connection, account_id: str, symbol: str, before: date | None
) -> Decimal:
    """Net shares from all four share sources, counting events dated < *before*
    (or every event when *before* is None).

    ⚠ **Corporate-action-UNAWARE, deliberately, and it must stay that way.** §6.3's
    ``corporate_delta`` is defined as ``shares_action_aware − shares_naive``, so this is the
    second term of the drawer's reconciliation identity — "improving" it silently changes
    the drawer's footer to always read zero. It is also the pre-existing path D38's
    containment invariant preserves byte-for-byte for every symbol with no corporate action.
    """
    cut = before.isoformat() if before is not None else None
    total = _ZERO
    opening_sql = "SELECT shares FROM opening_inventory WHERE account_id=? AND symbol=?"
    tx_sql = "SELECT side, quantity FROM transactions WHERE account_id=? AND symbol=?"
    div_sql = (
        "SELECT reinvest_shares FROM dividends WHERE account_id=? AND symbol=? "
        f"AND type IN ({_REINVEST_TYPES_SQL}) AND reinvest_shares IS NOT NULL"
    )
    params: tuple[str, ...] = (account_id, symbol)
    if cut is not None:
        opening_sql += " AND build_date < ?"
        tx_sql += " AND trade_date < ?"
        div_sql += " AND date < ?"
        params = (account_id, symbol, cut)
    opening = conn.execute(opening_sql, params).fetchone()
    if opening is not None:
        total += Decimal(opening["shares"])
    for r in conn.execute(tx_sql, params):
        q = Decimal(r["quantity"])
        total += q if r["side"] == Side.BUY.value else -q
    for r in conn.execute(div_sql, params):
        total += Decimal(r["reinvest_shares"])
    return total


class _DepthCapped(Exception):  # noqa: N818 - control flow inside one walk, never escapes
    """The walk exceeded :data:`MAX_ACTION_DEPTH`.

    Raised from the deep node and caught at the public entry point, so the cap ABORTS THE
    WHOLE WALK rather than truncating one branch. D23 is explicit that the guard "must never
    loop and never return a partial number" — a half-resolved predecessor folded into its
    parent's sum is precisely a partial number, and it would look entirely ordinary.
    """


def load_action_index(conn: sqlite3.Connection) -> ActionIndex:
    """Read the corporate-action ledger into one :class:`ActionIndex` (D23 rule 2).

    Build ONE per validation batch / per request and thread it through every share query.
    The oversell guard runs once per transaction, so a ~1,400-row import would otherwise
    re-read and re-group the whole action ledger ~1,400 times (trap #21).
    """
    return ActionIndex.from_stored(list_corporate_actions(conn))


def _resolve_index(conn: sqlite3.Connection, index: ActionIndex | None) -> ActionIndex:
    return index if index is not None else load_action_index(conn)


@dataclass
class _Walk:
    """One action-aware share walk: a date-ordered mini-replay over ONE position at a time.

    Not a second ``build_book``. It resolves a SIGNED SHARE COUNT and nothing else — no cost
    basis, no realized rows, no refusal matrix — which is why §6.2 specifies it as a separate
    implementation and why §7.2's parity test exists to hold the two together.
    """

    conn: sqlite3.Connection
    index: ActionIndex
    _flows: dict[tuple[str, str], tuple[tuple[date, int, Decimal], ...]] = field(
        default_factory=dict
    )
    _memo: dict[tuple[str, str, _Cut], Decimal] = field(default_factory=dict)
    _depth: int = 0

    def _flows_for(self, account_id: str, symbol: str) -> tuple[tuple[date, int, Decimal], ...]:
        """Every non-action share event for one position as ``(date, priority, delta)``.

        Loaded unbounded and filtered in Python, because the filter is a ``(date, priority)``
        cut and SQL sees only the date half. Cached per position for the duration of the
        walk, so a recursion that revisits a predecessor does not re-query.
        """
        cached = self._flows.get((account_id, symbol))
        if cached is not None:
            return cached
        rows: list[tuple[date, int, Decimal]] = []
        params = (account_id, symbol)
        for r in self.conn.execute(
            "SELECT build_date, shares FROM opening_inventory WHERE account_id=? AND symbol=?",
            params,
        ):
            rows.append(
                (date.fromisoformat(r["build_date"]),
                 int(EventPriority.OPENING),
                 Decimal(r["shares"]))
            )
        for r in self.conn.execute(
            "SELECT trade_date, side, quantity FROM transactions "
            "WHERE account_id=? AND symbol=?",
            params,
        ):
            qty = Decimal(r["quantity"])
            is_buy = r["side"] == Side.BUY.value
            rows.append(
                (date.fromisoformat(r["trade_date"]),
                 int(EventPriority.BUY if is_buy else EventPriority.SELL),
                 qty if is_buy else -qty)
            )
        for r in self.conn.execute(
            "SELECT date, reinvest_shares FROM dividends WHERE account_id=? AND symbol=? "
            f"AND type IN ({_REINVEST_TYPES_SQL}) AND reinvest_shares IS NOT NULL",
            params,
        ):
            rows.append(
                (date.fromisoformat(r["date"]),
                 int(EventPriority.DIVIDEND),
                 Decimal(r["reinvest_shares"]))
            )
        loaded = tuple(rows)
        self._flows[(account_id, symbol)] = loaded
        return loaded

    def _delta_of(self, account_id: str, symbol: str, action: CorporateAction) -> Decimal:
        """What *action* adds to *symbol*'s share count in *account_id*.

        Every branch is stated, including the one §6.2 left implicit, because a walker that
        infers the missing rule from the shape of the others gets it wrong in the direction
        that looks half-right.
        """
        before = (action.date, int(EventPriority.CORPORATE_ACTION))
        if action.kind is CorporateActionKind.SPLIT:
            # E20 forces from == to, so source and destination are ONE position and the
            # re-denomination is counted exactly once (this is `for_symbol`'s reason to
            # exist). The delta form — new minus old — keeps the caller's `+=` uniform.
            held = self.shares(account_id, symbol, before)
            return apply_ratio(held, action) - held
        # --- D33 (owner ruling 2026-08-10, widened by task #62): skip an action with a
        # NEGATIVE SIDE ---
        # The one exception to §6.3's "the share path applies every action unconditionally".
        # Applied to a negative source the walk manufactures a destination the replay never
        # created — no transaction, no opening, no holding, therefore NO FLAG — and §6.3's
        # footer then renders `＋公司行動 −100` under a red 對帳不一致 with nothing to explain
        # it. Skip AND flag: the footer paragraph only works when the cause is attached.
        #
        # BOTH ends are tested, and that is a correction to D33's stated grounds rather than
        # an extension of its reach. D33 said honouring E5 (source short) and E18 (destination
        # short) "would require importing the replay's model". It does not: long and short are
        # mutually exclusive BY CONSTRUCTION, so a negative signed count IS an open short.
        # E5 was therefore already covered by the source test D33 itself ordered, and E18 needs
        # the identical comparison pointed at the other end — one subtraction, zero import, the
        # two implementations still independent.
        #
        # The test is EXACTLY sound on both sides: a negative count is either an open declared
        # short (E5 source / E18 destination) or a currently-oversold position (E3 source /
        # E22 destination), and the replay refuses ALL FOUR — so the skip can never drop an
        # action the replay would have applied. What it still cannot see is E3/E22 in their
        # STICKY form, where the flag outlives the negative count; that divergence stays, and
        # `test_the_permitted_divergence_is_bounded_and_flagged` measures it.
        #
        # Scoped to EXCHANGE / SPINOFF, which is where the harm lives (a SPLIT has no separate
        # destination to manufacture). Extending it to SPLIT would break **E4**, which
        # deliberately ALLOWS a split to re-denominate an open short; the fixture that catches
        # that mistake is `III` in §7.2's parity ledger.
        src_before = self.shares(account_id, action.from_symbol, before)
        dest_before = self.shares(account_id, action.to_symbol, before)
        if src_before < _ZERO or dest_before < _ZERO:
            self.index.note_negative_side_skip(account_id, action.from_symbol)
            self.index.note_negative_side_skip(account_id, action.to_symbol)
            return _ZERO

        delta = _ZERO
        if symbol == action.from_symbol and action.kind is CorporateActionKind.EXCHANGE:
            # §4.2: the WHOLE position moves off the source.
            #
            # The `is EXCHANGE` test is the F-26 rule, and it is the one §6.2 never states:
            # **a SPINOFF contributes ZERO to its own source.** The parent keeps its
            # position (§4.3) — only cost is carved. §6.2 gives rules for SPLIT and for an
            # EXCHANGE's destination and stops there, and this walker is a separate
            # implementation by design, so it cannot inherit `cost_basis.py`'s
            # `source.shares unchanged`. A generic "source side" branch mirroring EXCHANGE
            # would ZERO THE PARENT, and because the child would still be right the fixture
            # looks half-correct — which is why the parent is asserted explicitly in §7.2.
            delta -= src_before
        if symbol == action.to_symbol:
            # §4.2 / §4.3: the destination's shares come from ANOTHER symbol's entire
            # history, which is what makes this a recursion rather than a sum.
            delta += apply_ratio(src_before, action)
        return delta

    def shares(self, account_id: str, symbol: str, cut: _Cut) -> Decimal:
        """Signed shares held at *cut* — every event sorting strictly before it."""
        key = (account_id, symbol, cut)
        memoized = self._memo.get(key)
        if memoized is not None:
            return memoized
        self._depth += 1
        if self._depth > MAX_ACTION_DEPTH:
            self._depth -= 1
            raise _DepthCapped(f"{symbol} ({account_id})")
        try:
            total = _ZERO
            for when, priority, qty in self._flows_for(account_id, symbol):
                if cut is None or (when, priority) < cut:
                    total += qty
            for action in self.index.for_symbol(account_id, symbol):
                at = (action.date, int(EventPriority.CORPORATE_ACTION))
                # Strictly before the cut. At the cut's OWN action priority this excludes
                # the action being evaluated, which is what makes the recursion descend.
                if cut is None or at < cut:
                    total += self._delta_of(account_id, symbol, action)
        finally:
            self._depth -= 1
        self._memo[key] = total
        return total


def _shares_at(
    conn: sqlite3.Connection,
    account_id: str,
    symbol: str,
    *,
    cut: _Cut,
    naive_before: date | None,
    index: ActionIndex | None,
    short_circuit: bool = True,
) -> Decimal:
    """Action-aware share count, or the pre-existing path when there is nothing to apply.

    **D38 invariant 1, the structural short-circuit.** A position that is neither the source
    nor the destination of any corporate action takes the ORIGINAL branch — the untouched
    :func:`_shares_until` with the argument it has always received. Not an equivalent
    computation that happens to agree: *code that does not execute cannot drift; code that
    computes an equal answer can.* An action reaches exactly ``(account, from_symbol)`` and
    ``(account, to_symbol)`` and nothing else, so :meth:`ActionIndex.for_symbol` being empty
    is a complete proof that this position is out of the feature's reach. On a ledger with
    no corporate actions at all the short-circuit therefore fires for every symbol, and
    ``tests/data_ingestion/test_holdings_containment.py`` proves it by making the walker
    raise on construction and running all nine call sites.
    """
    resolved = _resolve_index(conn, index)
    if short_circuit and not resolved.for_symbol(account_id, symbol):
        return _shares_until(conn, account_id, symbol, naive_before)
    try:
        return _Walk(conn, resolved).shares(account_id, symbol, cut)
    except _DepthCapped:
        # D31: read paths keep the bare `Decimal` and degrade to the ACTION-UNAWARE count —
        # a defined, pre-feature number rather than a truncated walk or a fabricated zero
        # (which would make the sell guard refuse every sell and `_held` report "not held").
        # The position is recorded on the index so the validation path can raise a
        # `needs_confirm` issue and the display can mark it 待釐清.
        resolved.note_depth_capped(account_id, symbol)
        return _shares_until(conn, account_id, symbol, naive_before)


def current_shares(
    conn: sqlite3.Connection,
    account_id: str,
    symbol: str,
    *,
    index: ActionIndex | None = None,
) -> Decimal:
    """Return the net shares currently held for *account_id* / *symbol*.

    opening_inventory shares + BUY − SELL + stock/DRIP ``reinvest_shares``
    (zero-cost shares, same replay rule as ``portfolio.cost_basis.build_book``), with every
    corporate action applied in date order.
    Returns ``Decimal("0")`` for no position.

    Pass *index* to reuse one :class:`ActionIndex` across a batch (D23 rule 2); omitted, one
    is read per call.
    """
    return _shares_at(conn, account_id, symbol, cut=None, naive_before=None, index=index)


def shares_through(
    conn: sqlite3.Connection,
    account_id: str,
    symbol: str,
    *,
    on: date,
    index: ActionIndex | None = None,
) -> Decimal:
    """Shares held at the CLOSE of *on* — events dated on or before it count.

    The sell-guard rule (2026-07-31): a sell must be covered by the position that exists at
    its own trade date. ``current_shares`` answers the net across ALL dates and therefore
    counts LATER buys, so a back-dated sell that oversells on its own day slips through it —
    the cash ledger has had the equivalent date-aware check (``running_min``) since audit C3.
    Same-day buys DO count, so an intraday round trip stays legal — and so does a same-day
    corporate action, which is effective at the START of the date.
    """
    return _shares_at(
        conn, account_id, symbol,
        cut=(on + timedelta(days=1), int(EventPriority.OPENING)),
        naive_before=on + timedelta(days=1),
        index=index,
    )


def shares_before_action_on(
    conn: sqlite3.Connection,
    account_id: str,
    symbol: str,
    *,
    on: date,
    index: ActionIndex | None = None,
) -> Decimal:
    """Shares as a corporate action dated *on* SEES them — the ``(on, CORPORATE_ACTION)`` cut.

    Neither :func:`shares_through` (the CLOSE of *on*, so a same-day sell counts) nor
    :func:`shares_on` (strictly before *on*, so a same-day opening does NOT) answers this.
    ``EventPriority`` puts the action between them: ``OPENING (0) < CORPORATE_ACTION (10) <
    BUY (20) < SELL (30)``.

    **Both neighbours were measured to be wrong here (2026-08-11).** With `shares_through`,
    a legitimate 7-for-1 was hard-rejected 「沒有持倉」 because the owner sold exactly their
    pre-split count on the split date — the sale is legal *after* the action, and the action
    runs first. With `shares_on`, D3's same-day opening would be invisible and the same
    rejection would fire on a position that demonstrably exists. This is D41 on the share
    side of the same function: two guards in one validator disagreeing about what "the
    action date" means.

    **No short-circuit, deliberately.** `_shares_at`'s structural short-circuit exists so a
    symbol with no corporate action takes the byte-identical pre-feature path (D38 invariant
    1). This accessor **has no pre-feature counterpart** — it is a corporate-action-only
    guard — so there is nothing to preserve byte-identity with, and the naive path cannot
    express the cut anyway: `_shares_until` applies one `<` bound to all three ledgers,
    which is precisely F-18's defect. The walk handles an actionless symbol correctly on its
    own (an empty `for_symbol` just means no deltas to add).
    """
    return _shares_at(
        conn, account_id, symbol,
        cut=(on, int(EventPriority.CORPORATE_ACTION)),
        naive_before=on,          # only reachable via the depth-cap degradation below
        index=index,
        short_circuit=False,
    )


def shares_on(
    conn: sqlite3.Connection,
    account_id: str,
    symbol: str,
    *,
    before: date,
    index: ActionIndex | None = None,
) -> Decimal:
    """Shares held going INTO *before* — events dated strictly earlier count.

    The dividend-entitlement rule: a holder receives a distribution when the
    position exists before the ex-date (buying ON the ex-date does not qualify). A corporate
    action dated on *before* is likewise excluded: it is effective during that date, not
    going into it.
    """
    return _shares_at(
        conn, account_id, symbol,
        cut=(before, int(EventPriority.OPENING)),
        naive_before=before,
        index=index,
    )
