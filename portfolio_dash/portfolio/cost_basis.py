"""Chronological ledger replay → open holdings (cost basis) + realized P&L."""

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from portfolio_dash.portfolio.results import (
    Book,
    Holding,
    RealizedPnL,
    RealizedRow,
    UnappliedAction,
)
from portfolio_dash.shared.corporate_actions import (
    CorporateAction,
    CorporateActionKind,
    apply_ratio,
)
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.ledger_events import EventPriority
from portfolio_dash.shared.models.enums import CASH_DIVIDEND_TYPES, Side
from portfolio_dash.shared.models.ledger import (
    Dividend,
    LedgerBundle,
    OpeningInventory,
    Transaction,
)

_ZERO = Decimal("0")


class OversellError(Exception):
    """A sell quantity exceeds held shares (input error vs short sale — require confirm).

    Carries the OFFENDING POSITION, not just a sentence about it. Every strict caller
    (重算 / 試算 / tax export) has to tell the user WHICH row to go and fix, and a caller
    that must regex a message to do so will not do it — measured by the 2026-08-27 sweep,
    where the drawer discarded the whole message and printed a bare 「試算暫不可用」 for
    every symbol in the book, in every account and every market (F-02).

    The three identifying kwargs are REQUIRED and have no defaults: forgetting them is a
    ``TypeError`` and a mypy error, never a quiet return to an unattributable failure —
    which is the exact state this constructor exists to end. ``str(exc)`` is unchanged, so
    every existing catch-and-report site keeps its wording byte-for-byte.
    """

    def __init__(self, message: str, *, account_id: str, symbol: str,
                 trade_date: date) -> None:
        super().__init__(message)
        self.account_id = account_id
        self.symbol = symbol
        self.trade_date = trade_date


class UnbookableLedgerError(ValueError):
    """The ledger contains an event this model cannot book honestly.

    Subclasses ``ValueError`` on purpose: the call sites that already degrade on
    ``except (ValueError, KeyError)`` keep behaving exactly as before, while the STRICT
    sites (重算 / what-if / tax export) can catch this precisely and answer 4xx instead of
    letting it escape as a 500 — the never-500-at-every-build_book-call-site rule.
    """


@dataclass
class _Position:
    """One (account, symbol) position during the replay.

    The LONG lot (``shares`` / totals) and the declared-SHORT lot are mutually exclusive by
    construction: a short sale covers the long lot first and only then opens a short, and a
    buy covers the short lot first and only then adds to the long. So a position is long,
    flat, or short — never both — and the emitted ``Holding`` carries one signed quantity.
    """

    quote_ccy: Currency
    shares: Decimal = field(default_factory=lambda: Decimal("0"))
    original_total: Decimal = field(default_factory=lambda: Decimal("0"))
    adjusted_total: Decimal = field(default_factory=lambda: Decimal("0"))
    # Declared short (spec 2026-07-31, option C): shares owed and the NET proceeds received
    # for them. Weighted-average, exactly like the long lot — no lot tracking.
    short_shares: Decimal = field(default_factory=lambda: Decimal("0"))
    short_proceeds: Decimal = field(default_factory=lambda: Decimal("0"))
    # STICKY 賣超 marker: an UNDECLARED oversell drops the cost basis permanently, but the
    # old flag was `shares < 0` read at the END of the replay — a later buy restored a
    # positive position and silently cleared it, leaving a wrong average with no warning
    # anywhere (measured 2026-07-30: average 6,100 against a 379 market price). Once true,
    # this stays true.
    ever_oversold: bool = False
    #: WHERE the first oversell happened, recorded so a caller can name the day instead of
    #: quoting the position's final net quantity. The guard is date-aware (a back-dated sell
    #: can be uncovered on its own date and covered by a later buy), so the final number is
    #: not evidence of anything — 「部位將為 9.5 股」 was being offered as proof of a shortfall
    #: (F-16, 2026-08-27). FIRST occurrence only: that is the one the user has to fix, and a
    #: later one may be a consequence of it. Recorded only on the ``allow_oversell`` path;
    #: the strict path raises with the same facts on the exception itself.
    oversold_on: date | None = None
    oversold_sold: Decimal | None = None
    oversold_held: Decimal | None = None
    # A dividend arrived while the short was open — skipped, never booked (see the dividend
    # branch). Surfaced so the position is visibly 待釐清 instead of quietly incomplete.
    unbookable_dividend: bool = False
    # A corporate action targeting this position could not be booked and was SKIPPED on the
    # dashboard path (E1a/E2/E3/E5/E18/E22). The shares are then in PRE-action terms against
    # global post-action prices, so the position is 待釐清 rather than merely stale.
    unbookable_action: bool = False
    # E24 (D32): the symbol an EXCHANGE moved this position to, in the same account. Set ONLY
    # by §4.2's zeroing, so it distinguishes "vacated by an action" from "sold down to zero"
    # — and that distinction is the whole rule. A guard keyed on `shares == 0` instead would
    # silently revert the 2026-07-26 audit's H2 ruling, which deliberately books a post-close
    # cash dividend as realized income (TW/MY pay weeks after the ex-date).
    #
    # It carries the destination rather than a bare bool because the refusal has to FLAG
    # something, and this position is about to be dropped by the holdings loop (zero shares —
    # audit F-47 case 2, where a flag is discarded with its carrier). The successor is the
    # position the owner still holds and the only one they will look at.
    vacated_to: str | None = None


class _SkipAction(Exception):  # noqa: N818 - control flow, not an error surfaced to a caller
    """The dashboard path's counterpart to raising: skip this event, flag the position."""


def _follow_exchange_chain(
    positions: dict[tuple[str, str], _Position], account_id: str, start: _Position
) -> _Position:
    """The live successor of a vacated position — a de-SPAC later renamed is the real case.

    §6.2's own example: the from-side of one action is the to-side of an earlier one, so the
    lookup is **transitive**. Bounded by the number of positions rather than trusting D15 to
    have made a cycle unreachable — a walk that cannot terminate is not worth the one line it
    saves, and this one runs inside the replay's hot loop.

    Falls back to *start* if the chain dead-ends (nothing to flag is better than raising).
    """
    seen: set[str] = set()
    node = start
    while node.vacated_to is not None and node.vacated_to not in seen:
        seen.add(node.vacated_to)
        nxt = positions.get((account_id, node.vacated_to))
        if nxt is None:
            break
        node = nxt
    return node


def _reject(
    message: str,
    position: _Position | None,
    action: CorporateAction,
    unapplied: list[UnappliedAction],
    *,
    allow_oversell: bool,
) -> None:
    """Refuse an action: raise on the strict path, skip + record + flag on the dashboard path.

    Never-500 (E1a): ``portfolio/dashboard.py`` calls ``build_book`` with NO try/except, so
    a stranded or incoherent action row would take the whole dashboard down. Every rejection
    below therefore degrades exactly like the oversell and dividend-on-short paths already
    do — the position becomes visibly 待釐清 instead of the page becoming a 500.

    The *record* is unconditional and the *flag* is not, because ``position`` is ``None``
    whenever the source never existed (E1) and is a ZERO-SHARE position the holdings loop
    drops whenever an earlier action emptied it (E2 after an EXCHANGE). Those two cases were
    silent for exactly as long as the refusal was expressed only as a flag (audit F-47 / E1,
    2026-08-10), which is why ``Book.unapplied_actions`` — not the flag — is what the
    valuation and XIRR gates read. ``message`` is reused verbatim as ``reason`` so the strict
    and dashboard paths explain the same refusal in the same words.
    """
    if not allow_oversell:
        raise UnbookableLedgerError(message)
    unapplied.append(
        UnappliedAction(
            account_id=action.account_id,
            date=action.date,
            kind=action.kind,
            from_symbol=action.from_symbol,
            to_symbol=action.to_symbol,
            reason=message,
        )
    )
    if position is not None:
        position.unbookable_action = True
    raise _SkipAction


def _apply_action(
    positions: dict[tuple[str, str], _Position],
    action: CorporateAction,
    quote_ccy: Callable[[str], Currency],
    unapplied: list[UnappliedAction],
    *,
    allow_oversell: bool,
) -> None:
    """Apply one corporate action to the live position map (spec §4.1-§4.4).

    The field transfer is NORMATIVE and complete: `_Position` has thirteen fields and every
    one of them has an explicit rule in §4.4, because "the formula didn't mention it" is not
    a specification. (This sentence said *nine* while the table listed *ten*: `vacated_to`
    updated the table and the test tuple but not this line, because the count guard reads the
    tuple and cannot see prose. Corrected 2026-08-27 alongside F-16's three fields.)
    The count is pinned by a test, because this docstring and §4.4 both fell
    behind once already — W3 added `unbookable_action` and neither was updated, so the
    "complete" claim was false for two commits (audit F-37, 2026-08-10). Two of those rules
    exist only because a review found the omission —
    zeroing the source's short fields (an `ε` residue survives a full cover and would
    otherwise contaminate a position a later buy can reopen), and refusing an
    `ever_oversold` DESTINATION (a discarded basis silently averaged into a real one).
    """
    src_key = (action.account_id, action.from_symbol)
    source = positions.get(src_key)
    try:
        # --- E1 / E2: the source must be a live position on this date ---
        if source is None:
            _reject(f"{action.from_symbol}（{action.account_id}）於 "
                    f"{action.date.isoformat()} 沒有持倉，無法套用公司行動 — "
                    "請確認該日之前的買進或期初庫存是否遺漏",
                    None, action, unapplied, allow_oversell=allow_oversell)
            return
        if source.shares == _ZERO and source.short_shares == _ZERO:
            _reject(f"{action.from_symbol}（{action.account_id}）於 "
                    f"{action.date.isoformat()} 已無持倉（部位已結清），無法套用公司行動",
                    source, action, unapplied, allow_oversell=allow_oversell)

        # --- E3: an oversold source has no basis left to scale ---
        if source.ever_oversold:
            _reject(f"{action.from_symbol}（{action.account_id}）是賣超（待釐清）部位，"
                    "成本基礎已被捨棄 — 縮放一個未定義的基礎仍是未定義",
                    source, action, unapplied, allow_oversell=allow_oversell)

        if action.kind is CorporateActionKind.SPLIT:
            # §4.1. Totals untouched, so both averages scale by 1/ratio on read and
            # payback_ratio is unchanged — a split changes nothing about how much of the
            # cost has been returned as dividends.
            # E4: a declared short scales too. You owe more shares and you still received
            # the same money, so short_proceeds is UNCHANGED and the average sale price
            # scales correctly.
            source.shares = apply_ratio(source.shares, action)
            source.short_shares = apply_ratio(source.short_shares, action)
            return

        # --- EXCHANGE / SPINOFF share a destination and its guards ---
        # E5: no honest booking exists for moving an open short to another symbol.
        if source.short_shares > _ZERO:
            _reject(f"{action.from_symbol}（{action.account_id}）有未回補的放空部位，"
                    "換股／分拆沒有可誠實記錄的分錄 — 請先回補",
                    source, action, unapplied, allow_oversell=allow_oversell)

        dst_key = (action.account_id, action.to_symbol)
        dest = positions.get(dst_key)
        if dest is not None:
            # E18: long and short are mutually exclusive BY CONSTRUCTION; `Q.shares +=`
            # onto a short destination breaks the invariant the whole replay rests on.
            if dest.short_shares > _ZERO:
                _reject(f"目的標的 {action.to_symbol}（{action.account_id}）有未回補的"
                        "放空部位，多空混在一個部位裡會使均價失去意義",
                        source, action, unapplied, allow_oversell=allow_oversell)
            # E22 (D16): the mirror of E18 one level deeper. E19 stops a FLAG being
            # laundered; this stops a COST BASIS being restored onto a position whose basis
            # the sticky 賣超 guard deliberately discarded — which reads as an entirely
            # ordinary average over shares that have none.
            if dest.ever_oversold:
                _reject(f"目的標的 {action.to_symbol}（{action.account_id}）是賣超"
                        "（待釐清）部位，移轉成本過去會讓已捨棄的成本基礎「復活」，"
                        "並算出一個看似正常、實際上沒有依據的均價",
                        source, action, unapplied, allow_oversell=allow_oversell)
        else:
            dest = positions.setdefault(dst_key, _Position(quote_ccy(action.to_symbol)))

        carried_shares = apply_ratio(source.shares, action)
        if action.kind is CorporateActionKind.EXCHANGE:
            # §4.2. The whole position moves. If Q already holds shares the two merge by
            # weighted average — the sum of the totals over the sum of the shares, exactly
            # what the method prescribes, so there is no special case.
            dest.shares += carried_shares
            dest.original_total += source.original_total
            dest.adjusted_total += source.adjusted_total
            dest.unbookable_dividend |= source.unbookable_dividend      # E19
            dest.unbookable_action |= source.unbookable_action
            source.shares = _ZERO
            source.original_total = _ZERO
            source.adjusted_total = _ZERO
            # §4.4: zero the short fields even though E5 proved them "already zero". They
            # are NEARLY zero: a full cover computes `P - (P/S)*S`, and Decimal division is
            # inexact whenever S does not divide P, so a residue survives. Today it hides
            # because the emitted shares are 0-0 and the holdings loop drops the position —
            # but EXCHANGE leaves the source live, and a later buy on the old ticker could
            # reopen it carrying `-ε` of basis.
            source.short_shares = _ZERO
            source.short_proceeds = _ZERO
            source.vacated_to = action.to_symbol      # E24 (D32)
            return

        # §4.3 SPINOFF. cost_carry is never guessed; validation rejects a row without it.
        carry = action.cost_carry if action.cost_carry is not None else _ZERO
        carved_original = source.original_total * carry
        carved_adjusted = source.adjusted_total * carry
        dest.shares += carried_shares
        dest.original_total += carved_original
        dest.adjusted_total += carved_adjusted
        dest.unbookable_dividend |= source.unbookable_dividend          # E19
        dest.unbookable_action |= source.unbookable_action
        # `total - carved`, NOT `total * (1 - c)`: algebraically identical, numerically not.
        # `1 - c` rounds once and `* (1-c)` rounds again, so the two sides can miss §2.1's
        # conservation law by an ulp. Subtracting exactly what was added makes the law hold
        # BY CONSTRUCTION rather than by luck.
        source.original_total -= carved_original
        source.adjusted_total -= carved_adjusted
        # source.shares unchanged — the parent keeps its position (§4.3).
    except _SkipAction:
        return


def build_book(bundle: LedgerBundle, *, allow_oversell: bool = False) -> Book:
    """Replay the ledger in date order; return open holdings, realized P&L, gross invested.

    Takes the whole :class:`LedgerBundle` rather than one argument per ledger: this is the
    one signature every replay call site shares, so a new ledger is a field on the bundle
    instead of an edit at eight sites, seven of which fail silently when missed.

    Same-day ordering is :class:`EventPriority` — opening, CORPORATE ACTION, buy, sell,
    dividend. An action is effective at the START of its date: a same-day trade is quoted
    in post-action terms (post-split price, new ticker), so the action applies first, and
    opening inventory dated on an action date describes the position as it stood BEFORE.

    Oversell (a sell exceeding holdings): by default raise ``OversellError`` (validation
    callers — e.g. the 重算/rebuild action — want to reject it). With ``allow_oversell=True``
    (the dashboard path), DEGRADE GRACEFULLY instead of crashing: net the position to
    negative shares, drop its (now-undefined) cost basis, and emit NO realized row — the
    resulting holding is flagged ``oversold`` with 待釐清 value (decided 2026-06-18). This
    keeps the dashboard alive after an acked oversell; the user fixes it by recording the
    missing opening inventory / buy. It is NOT short-position accounting.

    A corporate action the replay cannot book behaves the same way — raise on the strict
    path, skip on the dashboard path — but the record of the skip is on the BOOK
    (``Book.unapplied_actions``), not only on a holding, because two of the three ways it
    happens leave no holding behind. See :class:`~portfolio_dash.portfolio.results.
    UnappliedAction`; consumers must treat a non-empty list as "the share counts in this
    book are not trustworthy".
    """

    def quote_ccy(symbol: str) -> Currency:
        inst = bundle.instruments.get(symbol)
        if inst is None:
            raise KeyError(f"unknown instrument: {symbol}")
        return inst.quote_ccy

    positions: dict[tuple[str, str], _Position] = {}
    realized_rows: list[RealizedRow] = []
    gross: dict[Currency, Decimal] = defaultdict(lambda: Decimal("0"))
    # Book-level, NOT per-position: two of the three ways an action goes unapplied leave no
    # position to flag (see Book.unapplied_actions). Stays empty on the strict path.
    unapplied: list[UnappliedAction] = []
    # Book-level for the same reason as `unapplied`, plus one of its own: the consumer
    # (returns.xirr_reporting) must skip the EVENT, and a per-position flag cannot say
    # WHICH event. Stays empty on the strict path, which raises instead.
    refused_dividends: list[Dividend] = []

    # A stored action row the LOADER could not convert (2026-08-11). It never becomes an
    # event — there is no ratio to apply — but it must not be silent either: a dropped
    # action leaves a share count wrong by the ratio that looks entirely normal. Recorded
    # through the same channel every other refusal uses, so the XIRR gate, the drawer and
    # the strict path all treat it identically without knowing it is a different species.
    #
    # Deliberately BEFORE the event loop, so a malformed row is refused even if the ledger
    # is otherwise empty, and so the strict path raises on it first — an import that cannot
    # read its own ledger should say so before it says anything else.
    for bad in bundle.unreadable_actions:
        if not allow_oversell:
            raise UnbookableLedgerError(bad.reason)
        unapplied.append(
            UnappliedAction(account_id=bad.account_id, date=bad.date,
                            kind=bad.kind, from_symbol=bad.from_symbol,
                            to_symbol=bad.to_symbol, reason=bad.reason)
        )

    events: list[tuple[date, int, str, object]] = []
    for oi in bundle.opening:
        events.append((oi.build_date, EventPriority.OPENING, "open", oi))
    for tx in bundle.transactions:
        events.append((tx.trade_date,
                       EventPriority.BUY if tx.side is Side.BUY else EventPriority.SELL,
                       "tx", tx))
    for dv in bundle.dividends:
        # R6: ordered by `effective_date`, so a STOCK dividend with a known ex-date is
        # replayed on the ex-date — where the price already reflects it.
        events.append((dv.effective_date, EventPriority.DIVIDEND, "div", dv))
    for ca in bundle.actions:
        events.append((ca.date, EventPriority.CORPORATE_ACTION, "action", ca))
    events.sort(key=lambda e: (e[0], e[1]))

    for _d, _p, kind, ev in events:
        if kind == "action":
            assert isinstance(ev, CorporateAction)
            _apply_action(positions, ev, quote_ccy, unapplied,
                          allow_oversell=allow_oversell)
        elif kind == "open":
            assert isinstance(ev, OpeningInventory)
            key = (ev.account_id, ev.symbol)
            pos = positions.setdefault(key, _Position(quote_ccy(ev.symbol)))
            pos.shares += ev.shares
            pos.original_total += ev.original_cost_total
            pos.adjusted_total += ev.original_cost_total
            gross[pos.quote_ccy] += ev.original_cost_total
        elif kind == "tx":
            assert isinstance(ev, Transaction)
            ccy = quote_ccy(ev.symbol)
            key = (ev.account_id, ev.symbol)
            pos = positions.setdefault(key, _Position(ccy))
            if ev.side is Side.BUY:
                # A buy COVERS an open declared short before it adds to the long lot. The
                # cover settles at THIS buy's all-in per-share cost and the leftover shares
                # start their long life at that same cost — the owner's stated rule
                # (2026-07-31): 買回的每股成本結算獲利，剩下的股數以本次成本為起點。
                cover = min(ev.quantity, pos.short_shares)
                if cover > _ZERO:
                    per_share = (ev.quantity * ev.price + ev.fees + ev.tax) / ev.quantity
                    short_avg = pos.short_proceeds / pos.short_shares
                    realized_rows.append(
                        RealizedRow(
                            account_id=ev.account_id,
                            symbol=ev.symbol,
                            quote_ccy=ccy,
                            sell_date=ev.trade_date,   # realizes on the COVER date
                            shares_sold=cover,
                            proceeds_net=short_avg * cover,
                            original_cost_removed=per_share * cover,
                            adjusted_cost_removed=per_share * cover,
                            realized=(short_avg - per_share) * cover,
                            kind="short_cover",
                        )
                    )
                    pos.short_proceeds -= short_avg * cover
                    pos.short_shares -= cover
                to_long = ev.quantity - cover
                if to_long > _ZERO:
                    # Exact total when nothing was covered, so an ordinary buy is
                    # byte-identical to the pre-short engine (no per-share round trip).
                    cost = (ev.quantity * ev.price + ev.fees + ev.tax if cover == _ZERO
                            else per_share * to_long)
                    pos.shares += to_long
                    pos.original_total += cost
                    pos.adjusted_total += cost
                    gross[ccy] += cost      # covering a short is not new investment
            elif getattr(ev, "short_sale", False):
                # DECLARED short sale: sell the long lot first (ordinary realized P&L), then
                # open/extend the short lot with the remainder. Declared per transaction and
                # OFF by default, so a missing buy can never become a fabricated short.
                from_long = pos.shares if pos.shares > _ZERO else _ZERO
                from_long = min(ev.quantity, from_long)
                per_share_net = (ev.quantity * ev.price - ev.fees - ev.tax) / ev.quantity
                if from_long > _ZERO:
                    frac = from_long / pos.shares
                    original_removed = pos.original_total * frac
                    adjusted_removed = pos.adjusted_total * frac
                    realized_rows.append(
                        RealizedRow(
                            account_id=ev.account_id,
                            symbol=ev.symbol,
                            quote_ccy=ccy,
                            sell_date=ev.trade_date,
                            shares_sold=from_long,
                            proceeds_net=per_share_net * from_long,
                            original_cost_removed=original_removed,
                            adjusted_cost_removed=adjusted_removed,
                            realized=per_share_net * from_long - adjusted_removed,
                        )
                    )
                    pos.shares -= from_long
                    pos.original_total -= original_removed
                    pos.adjusted_total -= adjusted_removed
                to_short = ev.quantity - from_long
                if to_short > _ZERO:
                    pos.short_shares += to_short
                    pos.short_proceeds += per_share_net * to_short
            else:
                if ev.quantity > pos.shares:
                    if not allow_oversell:
                        raise OversellError(
                            f"sell {ev.quantity} > held {pos.shares} for {ev.symbol}",
                            account_id=ev.account_id, symbol=ev.symbol,
                            trade_date=ev.trade_date,
                        )
                    # Graceful: net to a negative (賣超) position; its cost basis is
                    # undefined, so drop it and emit no realized row (待釐清). UNCHANGED —
                    # only the marker below is new, and it never clears.
                    if pos.oversold_on is None:
                        # BEFORE the mutation below, so `pos.shares` is still what was held
                        # on that date rather than the post-sale negative.
                        pos.oversold_on = ev.trade_date
                        pos.oversold_sold = ev.quantity
                        pos.oversold_held = pos.shares
                    pos.ever_oversold = True
                    pos.shares -= ev.quantity
                    pos.original_total = _ZERO
                    pos.adjusted_total = _ZERO
                    continue
                frac = ev.quantity / pos.shares
                original_removed = pos.original_total * frac
                adjusted_removed = pos.adjusted_total * frac
                proceeds_net = ev.quantity * ev.price - ev.fees - ev.tax
                realized_rows.append(
                    RealizedRow(
                        account_id=ev.account_id,
                        symbol=ev.symbol,
                        quote_ccy=ccy,
                        sell_date=ev.trade_date,
                        shares_sold=ev.quantity,
                        proceeds_net=proceeds_net,
                        original_cost_removed=original_removed,
                        adjusted_cost_removed=adjusted_removed,
                        realized=proceeds_net - adjusted_removed,
                    )
                )
                pos.shares -= ev.quantity
                pos.original_total -= original_removed
                pos.adjusted_total -= adjusted_removed
        else:  # dividend
            assert isinstance(ev, Dividend)
            key = (ev.account_id, ev.symbol)
            existing = positions.get(key)
            if existing is None:
                # Fail loud on a dividend for a position with no prior buy/opening:
                # silently creating one would discard cash dividends (filtered out at
                # 0 shares) or fabricate a $0-cost ghost holding from a DRIP.
                raise ValueError(
                    f"dividend for unknown position {key} (no prior buy/opening inventory)"
                )
            if existing.short_shares > _ZERO:
                # A dividend landing on an OPEN SHORT is not representable. A short seller
                # PAYS the dividend in lieu, and there is no debit row for that — while the
                # branches below would (a) book the recorded positive net as realized INCOME,
                # because an open short also has `shares == 0` in the long lot, or (b) add
                # DRIP/STOCK shares straight to the long lot, breaking the long/short
                # exclusivity the whole replay depends on (a 10-share DRIP against a 10-share
                # short nets to zero, and the position — with its proceeds — vanishes from
                # the report entirely). Both are money-of-record errors, so: fail loud on the
                # strict path, and on the dashboard path skip the event and flag the position
                # rather than crash (the same posture as the oversell degradation).
                if not allow_oversell:
                    raise UnbookableLedgerError(
                        f"{ev.symbol}（{ev.account_id}）於 {ev.date.isoformat()} 有股利紀錄，"
                        "但該時點是放空部位 — 放空方需支付股利，本系統無此借方分錄。"
                        "請刪除該筆股利，或改以現金收支登錄。"
                    )
                existing.unbookable_dividend = True
                refused_dividends.append(ev)
                continue
            if existing.vacated_to is not None:
                # E24 (D32) — the position was moved away by an EXCHANGE, and §4.2 leaves it
                # in the map with zeroed fields (required, so a later buy on the old ticker
                # cannot reopen it carrying −ε). So `existing is not None` and
                # `short_shares == 0`: NEITHER refusal above applies and the payment books.
                # Both branches below are money-of-record errors on a dead ticker —
                #   CASH/NET  → post-close realized income on a symbol that no longer exists;
                #   DRIP/STOCK → `shares += reinvest_shares` RESURRECTS it at `avg = 0`, and
                #     a delisted ticker never gets a price, and ONE unpriced holding makes
                #     `returns.py` return `rate=None` for the WHOLE portfolio.
                # The second is why this is not cosmetic: the damage is portfolio-wide.
                # The owner records such a payment as a cash movement instead — the same
                # remedy `domain-ledger.md` already gives for a dividend on an open short.
                successor = _follow_exchange_chain(positions, ev.account_id, existing)
                if not allow_oversell:
                    raise UnbookableLedgerError(
                        f"{ev.symbol}（{ev.account_id}）於 {ev.date.isoformat()} 有股利紀錄，"
                        f"但該部位已於此之前換股為 {existing.vacated_to} — "
                        "已換出的標的不再有持倉可歸屬這筆配息，"
                        "記在原標的上會變成一筆已下市代號的已實現收益（或把部位以零成本復活）。"
                        "請刪除該筆股利，或改以現金收支登錄。"
                    )
                successor.unbookable_dividend = True
                refused_dividends.append(ev)
                continue
            if ev.type in CASH_DIVIDEND_TYPES:  # CASH (TW) + NET (MY 單層淨額)
                if existing.shares == _ZERO:
                    # AUDIT H2 (2026-07-26) — the position is already CLOSED when this cash
                    # dividend lands. TW/MY pay weeks after the ex-date, so being entitled on
                    # the ex-date and flat by the payment date is ordinary, not exotic.
                    #
                    # Reducing `adjusted_total` here would be a write into a zero-share
                    # position that the holdings loop below drops (`shares == 0 -> continue`),
                    # so the payout silently disappeared from 總報酬 / 已實現 / 未實現 while
                    # 股利總覽 (which walks the dividend ledger directly) and the XIRR cashflow
                    # series both still counted it — one payout, three disagreeing answers.
                    #
                    # Book it as REALIZED INCOME instead: it is money received that can no
                    # longer be attributed to a cost basis. Still exactly ONCE (the cost path
                    # is skipped, not doubled), so the no-double-counting invariant holds.
                    realized_rows.append(
                        RealizedRow(
                            account_id=ev.account_id,
                            symbol=ev.symbol,
                            quote_ccy=existing.quote_ccy,
                            sell_date=ev.date,
                            shares_sold=_ZERO,
                            proceeds_net=ev.net,
                            original_cost_removed=_ZERO,
                            adjusted_cost_removed=_ZERO,
                            realized=ev.net,
                            kind="dividend",
                        )
                    )
                else:
                    existing.adjusted_total -= ev.net
            else:  # DRIP / STOCK add shares at zero cost
                if ev.reinvest_shares is None:
                    # Fail loud: a DRIP/stock dividend without share count would
                    # silently drop the reinvestment instead of coercing to zero.
                    raise ValueError(
                        f"{ev.type} dividend for {key} requires reinvest_shares"
                    )
                existing.shares += ev.reinvest_shares

    # Flag the positions an unreadable row names, now that the replay has built them. The
    # RECORD went in before the event loop (a book that cannot read its ledger should say
    # so first); the FLAG can only go on afterwards, and the two halves are the same split
    # `_reject` makes for the same reason — some refusals have no position to flag.
    for bad in bundle.unreadable_actions:
        for sym in (bad.from_symbol, bad.to_symbol):
            if (touched := positions.get((bad.account_id, sym))) is not None:
                touched.unbookable_action = True

    holdings: list[Holding] = []
    for (account_id, symbol), pos in positions.items():
        # An OPEN declared short is a real position and must be reported, so the quantity is
        # SIGNED: long stays positive, short comes out negative with the received proceeds as
        # its (negative) basis. Every downstream formula then works unchanged —
        # avg = total/shares is the short's average sale price, market_value = price*shares is
        # the negative exposure, and unrealized = (price-avg)*shares profits when price falls.
        shares = pos.shares - pos.short_shares
        original_total = pos.original_total - pos.short_proceeds
        adjusted_total = pos.adjusted_total - pos.short_proceeds
        if shares == _ZERO:
            continue
        original_avg = original_total / shares
        adjusted_avg = adjusted_total / shares
        dividend_portion = original_total - adjusted_total
        # abs() per domain-ledger.md: "any RATIO over the basis must divide by
        # abs(cost_total)". An open short carries a negative basis; the aggregate in
        # api/routers/symbol.py demonstrably flips sign without this, and holding both
        # sites to the one law is cheaper than proving this one unreachable.
        payback = dividend_portion / abs(original_total) if original_total != _ZERO else _ZERO
        holdings.append(
            Holding(
                account_id=account_id,
                symbol=symbol,
                quote_ccy=pos.quote_ccy,
                shares=shares,
                original_avg=original_avg,
                adjusted_avg=adjusted_avg,
                original_cost_total=original_total,
                adjusted_cost_total=adjusted_total,
                dividend_portion=dividend_portion,
                payback_ratio=payback,
                # 賣超 is now STICKY: an undeclared oversell keeps its warning even after a
                # later buy nets the position back above zero (that buy does not restore the
                # basis the oversell discarded).
                oversold=pos.ever_oversold or pos.shares < _ZERO,
                oversold_on=pos.oversold_on,
                oversold_sold=pos.oversold_sold,
                oversold_held=pos.oversold_held,
                short_open=pos.short_shares > _ZERO,
                unbookable_dividend=pos.unbookable_dividend,
                unbookable_action=pos.unbookable_action,
            )
        )

    realized_by_ccy: dict[Currency, Decimal] = defaultdict(lambda: Decimal("0"))
    for r in realized_rows:
        realized_by_ccy[r.quote_ccy] += r.realized

    return Book(
        holdings=holdings,
        realized=RealizedPnL(rows=realized_rows, by_currency=dict(realized_by_ccy)),
        gross_invested=dict(gross),
        unapplied_actions=unapplied,
        refused_dividends=refused_dividends,
    )
