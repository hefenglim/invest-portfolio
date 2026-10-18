# Frozen reference series: v0.1.31's trend, net-worth and benchmark functions, copied from
# commit 473fe5a. NEVER EDITED except to follow a type rename.
#
# Each FROZEN BEGIN / END region is the named line range of the named file byte for byte (LF
# line endings; the hash test normalises a CRLF checkout first). Verify one by hand with:
#     git show 473fe5a:<file> | sed -n '<a>,<b>p' | sha256sum
# which must print the sha256 under its BEGIN line. ``tests/reference/test_frozen_hashes.py``
# re-hashes every region on every run.
#
# Copied: ``_entry_at_or_before`` / ``_at_or_before`` / ``_fx_at`` / ``build_reporting_flows``
# / ``daily_value_series`` (timeseries.py), ``replay_dividend_refusals`` / ``replay_refusals``
# (returns.py — ``daily_value_series`` reaches ``build_book`` through them, so they are frozen
# too), ``_day_steps`` / ``_balance_on`` / ``daily_cash_series`` / ``compose_net_worth``
# (networth.py), ``_close_at_or_before`` / ``counterfactual`` (benchmark_counterfactual.py).
#
# ``build_book`` below is the FROZEN engine (``cost_basis_v0131``), never the live one, and so
# are ``UnbookableLedgerError`` / ``_uncomputable_message``. Every other name the copies read
# (the result types, ``price_in``, ``ActionIndex``, ``cash_balances`` / ``pool_lines`` /
# ``running_statement``, ``booked_dividends``, ``dividend_refusals``, ``convert`` …) is the
# live package's: not frozen, by design. The public names are the ``ref_*`` aliases at the end.
"""Frozen v0.1.31 trend / net-worth / benchmark functions (see the header comment)."""

from bisect import bisect_right
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal

from portfolio_dash.portfolio.benchmark_counterfactual import (
    Counterfactual,
    MarketLeg,
    ReportingFlow,
)
from portfolio_dash.portfolio.cash import (
    CashLine,
    _DivRow,
    _FxRow,
    _MovementRow,
    _TxRow,
    cash_balances,
    pool_lines,
    running_statement,
)
from portfolio_dash.portfolio.dashboard_models import TrendPoint, TrendSeries
from portfolio_dash.portfolio.networth import CashDay
from portfolio_dash.portfolio.price_basis import price_in
from portfolio_dash.portfolio.results import Book
from portfolio_dash.portfolio.returns import (
    XIRR_CASH_KINDS,
    CashMovementFlow,
    DividendIdentity,
    DividendRefusals,
    booked_dividends,
    dividend_refusals,
)
from portfolio_dash.shared.cash_kinds import movement_sign
from portfolio_dash.shared.corporate_actions import ActionIndex
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.fx import convert
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import CASH_DIVIDEND_TYPES, Side
from portfolio_dash.shared.models.ledger import Dividend, LedgerBundle, counts_by
from portfolio_dash.shared.money import settled_notional
from tests.reference.cost_basis_v0131 import (
    UnbookableLedgerError,
    _uncomputable_message,
)
from tests.reference.cost_basis_v0131 import (
    ref_build_book as build_book,  # the FROZEN engine — never the live one
)

# ==== FROZEN BEGIN 473fe5a:portfolio_dash/portfolio/timeseries.py L60-L65 ====
# sha256=4574b76bc40dc2326ae1939f37c0ae3b59516bd8ac537cebc06b98ee4fb9a9aa
_ZERO = Decimal("0")
_ONE = Decimal("1")

# Ascending (date, value) series, as bulk-loaded by the combiner.
PriceHistory = dict[str, list[tuple[date, Decimal]]]
FxHistory = dict[tuple[Currency, Currency], list[tuple[date, Decimal]]]
# ==== FROZEN END ====


# ==== FROZEN BEGIN 473fe5a:portfolio_dash/portfolio/timeseries.py L68-L238 ====
# sha256=3512c7da2e33d6c247310d43168ad08adb3785bf16315acc8956a2a0bb9b1258
def _entry_at_or_before(
    series: list[tuple[date, Decimal]], on: date
) -> tuple[date, Decimal] | None:
    """Latest ``(date, value)`` at-or-before ``on`` over an ascending series, else None.

    The price path needs the DATE as well as the value: §5.1(d)'s read rule re-expresses a
    carried-forward close over the window ``(pd, d]``, where ``pd`` is the date of the row
    actually returned — which is precisely the fact a value-only lookup throws away.
    """
    idx = bisect_right(series, on, key=lambda item: item[0])
    if idx == 0:
        return None
    return series[idx - 1]


def _at_or_before(series: list[tuple[date, Decimal]], on: date) -> Decimal | None:
    """Latest value at-or-before ``on`` over an ascending series, else None.

    The FX callers' view: a rate is not re-expressed by a corporate action, so they need
    the value alone. One lookup, two projections — never two ``bisect_right`` calls.
    """
    entry = _entry_at_or_before(series, on)
    return entry[1] if entry is not None else None


def _fx_at(history: FxHistory, on: date, base: Currency, quote: Currency) -> Decimal | None:
    """Carry-forward rate: identity -> direct pair -> inverted pair -> None."""
    if base == quote:
        return _ONE
    direct = history.get((base, quote))
    if direct is not None:
        rate = _at_or_before(direct, on)
        if rate is not None:
            return rate
    inverse = history.get((quote, base))
    if inverse is not None:
        rate = _at_or_before(inverse, on)
        if rate is not None:
            return _ONE / rate
    return None


def build_reporting_flows(
    bundle: LedgerBundle,
    fx_history: FxHistory,
    reporting: Currency,
    *,
    cash_movements: Sequence[CashMovementFlow],
    refused_dividends: Iterable[DividendIdentity],
) -> list[ReportingFlow] | None:
    """Every net-invested flow, in the reporting currency at its OWN trade-date FX.

    Signs mirror the XIRR conventions, negated: opening ``+cost``, buy ``+gross`` (incl.
    fees + tax), sell ``−net``, cash dividend ``−net``. Returns ``None`` when any flow's date
    has no on-or-before FX rate for its pair — the same honest bail as the trend itself, and
    the same rule XIRR uses.

    Raises :class:`~portfolio_dash.portfolio.cost_basis.UnbookableLedgerError` (never a raw
    ``ArithmeticError``) when a row's numbers cannot be computed at all — a MISSING RATE and
    an UNCOMPUTABLE ROW are different answers on purpose: the first degrades the whole series
    honestly, the second is a row the owner must go and fix, and the sentence names it.

    ⚠ This is the ONE definition of 「what counts as money put in」, and it is extracted rather
    than duplicated because :mod:`portfolio.benchmark_counterfactual` spends exactly this
    stream on an index instead. Two copies would let the portfolio and its counterfactual
    quietly disagree about which flows exist, and the whole comparison rests on them being the
    same money on the same dates. Each flow carries its ``market`` so the counterfactual can
    route it to that market's benchmark; the trend simply ignores that field.

    **Cash movements are included since AI-D48** (owner ruling 2026-08-27), on exactly the
    three kinds ``xirr_reporting`` has taken since AI-D42: ``REBATE`` / ``INTEREST_EXPENSE`` /
    ``BROKER_FEE`` — the costs of TRADING AND FINANCING. ``DEPOSIT`` / ``WITHDRAW`` /
    ``OPENING`` stay out as capital movements, and ``INTEREST`` stays out because the idle
    principal earning it never entered this figure. ``cash_movements`` is a REQUIRED keyword
    argument with no default: a default would let a caller silently ship the pre-AI-D48 number,
    which is a plausible figure quietly missing flows — invisible in the output, so it is made
    visible in the type instead (mypy error and ``TypeError``).

    Why it mattered: **A and B are printed side by side** (AI-D41) and their difference is
    labelled 「本金匯率效果」. While XIRR counted a 77% rebate and B did not, that label was
    false — the difference was 「本金匯率效果 ＋ 三類現金帳」. R4 then measured its excess
    return against B, so a broker fee B could not see read as beating the index.

    A cash movement carries **no market and no symbol** (``market=None``), and AI-D49 makes
    that load-bearing: :func:`portfolio.benchmark_counterfactual.counterfactual` leaves those
    flows out of both legs, so the costs are charged to the portfolio and never placed on the
    index. Sign: the movement's ``credit`` from the shared table, NEGATED like every other
    flow here — a fee (debit) therefore RAISES ``net_invested`` exactly as a buy-side fee
    always has, and a rebate lowers it.

    **A dividend the replay REFUSED is not a flow here** (K2, owner ruling 2026-10-05, with ⑪):
    ``refused_dividends`` is ``Book.refused_dividends`` of the replay over *bundle*, taken
    through ``returns.booked_dividends`` — the same rows XIRR skips (``xirr_reporting``'s own
    ``refused_dividends``). B (``total_value − net_invested``) was withheld only while the
    trend's last day was ``incomplete``, and two refusals leave it complete: a short covered
    after the payout landed on it, and a vacated ticker whose successor is flat — B then
    counted a payout 總報酬 and XIRR left out, and the index counterfactual was handed the
    same phantom withdrawal. Required with no default, like ``cash_movements``: a default
    would ship the old figure in silence.
    """
    def quote_ccy(symbol: str) -> Currency:
        inst = bundle.instruments.get(symbol)
        if inst is None:
            raise KeyError(f"unknown instrument: {symbol}")
        return inst.quote_ccy

    def market_of(symbol: str) -> Market:
        inst = bundle.instruments.get(symbol)
        if inst is None:
            raise KeyError(f"unknown instrument: {symbol}")
        return inst.market

    # R-1 (2026-08-29): this is the SECOND consumer of ledger quantities, and until then the
    # only guarded one was ``build_book``. That guard structurally cannot cover this line: the
    # dashboard replays with ``allow_oversell=True``, which sends an oversized SELL down the
    # 賣超 degradation branch — a branch that performs NO multiplication, so the replay returns
    # cleanly and the ``decimal.Overflow`` lands here instead, on ``t.quantity * t.price``.
    # Measured on one direct-SQL row: ``GET /api/dashboard`` answered 422 as a BUY and **500
    # 「系統發生未預期的錯誤」** as a SELL, and ``GET /api/performance/twr`` inherited both.
    #
    # ``booking`` names the offending row exactly as ``build_book``'s own wrap does, so both
    # sides of the same trade produce the SAME sentence.
    booking: object | None = None
    try:
        # The source event travels with each raw flow (the 4th element) so the CONVERSION loop
        # below can name the row too: ``amount * rate`` is that row's money meeting a stored
        # rate, and a message that stops naming rows halfway is one the owner cannot act on.
        raw: list[tuple[date, str, Decimal, object]] = []
        for o in bundle.opening:
            booking = o
            raw.append((o.build_date, o.symbol, o.original_cost_total, o))
        for t in bundle.transactions:
            booking = t
            gross = settled_notional(t.quantity, t.price, quote_ccy(t.symbol))  # 價金
            if t.side is Side.BUY:
                raw.append((t.trade_date, t.symbol, gross + t.fees + t.tax, t))
            else:
                raw.append((t.trade_date, t.symbol, -(gross - t.fees - t.tax), t))
        for dv in booked_dividends(bundle.dividends, refused_dividends):
            if dv.type in CASH_DIVIDEND_TYPES:  # CASH (TW) + NET (MY) — one definition
                booking = dv
                raw.append((dv.date, dv.symbol, -dv.net, dv))

        out: list[ReportingFlow] = []
        for d, symbol, amount, source in raw:
            booking = source
            rate = _fx_at(fx_history, d, quote_ccy(symbol), reporting)
            if rate is None:
                return None
            out.append(ReportingFlow(d, market_of(symbol), convert(amount, rate), symbol))
        # AI-D48. Each movement carries its OWN currency (there is no symbol to look one up
        # from) and its sign comes from the shared table, so a kind can never be a credit here
        # and a debit in the pool balance.
        for mv in cash_movements:
            booking = mv
            if mv.kind.upper() not in XIRR_CASH_KINDS:
                continue
            rate = _fx_at(fx_history, mv.date, mv.ccy, reporting)
            if rate is None:
                return None
            out.append(ReportingFlow(
                mv.date, None, convert(-movement_sign(mv.kind) * mv.amount, rate), ""))
        return out
    except ArithmeticError as exc:
        # The CLASS, not one cause — ``decimal.Overflow`` and ``decimal.DivisionByZero`` are
        # SIBLINGS of ``InvalidOperation``, not subclasses, and ``ZeroDivisionError`` is in it
        # too. ``UnbookableLedgerError`` is a ``ValueError`` on purpose, so every existing
        # ``except (ValueError, KeyError)`` degradation site and ``api/errors.py``'s 422 handler
        # both inherit this. ``from exc`` keeps the failing operation in the traceback: only the
        # sentence the owner reads is replaced, never the diagnosis.
        raise UnbookableLedgerError(_uncomputable_message(booking)) from exc
# ==== FROZEN END ====


# ==== FROZEN BEGIN 473fe5a:portfolio_dash/portfolio/timeseries.py L283-L469 ====
# sha256=24ffb4883518271b0bd6a3a46001dbe37facf757b23c2fa899a15f72aa5e025d
def daily_value_series(
    bundle: LedgerBundle,
    price_history: PriceHistory,
    fx_history: FxHistory,
    reporting: Currency,
    *,
    end: date,
    cash_movements: Sequence[CashMovementFlow],
) -> TrendSeries:
    """Replay the ledgers day by day from the first event to ``end``.

    Returns ``available=False`` (empty points) when there are no ledger events, or
    when any flow date lacks an on-or-before FX rate for its needed pair.

    Raises :class:`~portfolio_dash.portfolio.cost_basis.UnbookableLedgerError` (never a raw
    ``ArithmeticError``) when a day cannot be valued at all — see :func:`build_reporting_flows`.
    """
    # R-1: every arithmetic site in this module refuses the same way. The operands HERE are a
    # stored PRICE and a share count (``price * h.shares``), not one ledger line, so the
    # sentence deliberately names NO row — pointing at the last row read would send the owner
    # to edit something the replay booked without complaint (the discipline
    # ``_uncomputable_message`` states). A fault raised by ``build_reporting_flows`` below is
    # ALREADY an ``UnbookableLedgerError`` (a ``ValueError``) and passes straight through this
    # arm with its row-naming sentence intact — a second wrap must not flatten it.
    try:
        # R6: `effective_date`, matching `LedgerBundle.through` and `build_book`'s ordering.
        # Latent rather than live — a 配股's ex-date cannot precede the position that earns it,
        # so no real ledger starts on one — but three date filters spelling the rule three ways
        # is how two of them drift, and this was the third.
        event_dates = (
            [t.trade_date for t in bundle.transactions]
            + [d.effective_date for d in bundle.dividends]
            + [o.build_date for o in bundle.opening]
        )
        if not event_dates:
            return TrendSeries(points=[], reporting_currency=reporting, available=False)
        start = min(event_dates)

        # ONE ActionIndex for the whole replay (trap #21) — never one per day and never one per
        # lookup. Built from the FULL action list rather than per-day: `split_factor`'s window
        # is `(pd, day]`, so an action dated after `day` is excluded by the window itself and a
        # per-day rebuild would be the same answer at hundreds of times the cost.
        actions = ActionIndex.build(bundle.actions)

        # K2 (2026-10-05): the payouts the replay refused are not money put in or taken out.
        # One whole-bundle replay, taken here rather than from the caller so this function's
        # signature (and every caller's) is unchanged; it costs one replay beside the
        # per-day ones below. A refusal is decided from the events BEFORE the dividend, so the
        # whole bundle refuses exactly the rows each day's `bundle.through(day)` refuses.
        reporting_flows = build_reporting_flows(bundle, fx_history, reporting,
                                                cash_movements=cash_movements,
                                                refused_dividends=replay_refusals(bundle))
        if reporting_flows is None:
            return TrendSeries(points=[], reporting_currency=reporting, available=False)
        converted: list[tuple[date, Decimal]] = [(f.on, f.amount) for f in reporting_flows]

        points: list[TrendPoint] = []
        # Options spec §5 / R3: an open contract has no daily price series (R2 values it at its
        # last trade, a point-in-time figure), so its value is left OUT of every day — without
        # marking the day incomplete — and the series says so once.
        options_excluded = False
        # The book changes only on a day some row counts from (2026-10-13). `through(day)` is a
        # date-PREFIX cut, so between two such days it holds the same rows and `build_book`, a
        # pure function of the bundle, returns the same book: replay on those days and reuse
        # the book on the others. Measured on the owner's 6-year ledger: 2,181 replays → 461,
        # the dashboard 26.1 s → 7.7 s locally, payload byte-identical. Rebuilding every
        # calendar day was quadratic in the history length — invisible on the demo corpus,
        # minutes per page on the prod VM. The loop below only READS `book`; anything that
        # starts mutating it must rebuild every day instead.
        rebuild_on = bundle.counts_on_dates()
        book: Book | None = None
        day = start
        while day <= end:
            # allow_oversell (2026-07-02): an acked-oversold ledger must NEVER 500 the
            # dashboard through the trend replay either — mirror the main book's
            # degradation. An oversold (negative-share) day has no honest value, so it
            # marks the point incomplete instead of contributing a fabricated number.
            if book is None or day in rebuild_on:
                book = build_book(bundle.through(day), allow_oversell=True)
            total = _ZERO
            incomplete = False
            # Read the BOOK, and BEFORE the holdings loop, because that loop structurally
            # cannot see two of the three ways a corporate action goes unapplied: `_reject`
            # writes `unbookable_action` onto the SOURCE position, and the source is `None`
            # when it never existed (a missing buy) and a ZERO-share position when an earlier
            # action already emptied it (a duplicated EXCHANGE row) — and a zero-share holding
            # is dropped by `if h.shares == _ZERO: continue` two lines below, taking the flag
            # with it. Both refusals therefore reached the per-holding check with NOTHING
            # flagged, and the day was published `incomplete=False` over shares the replay
            # knowingly left in pre-action terms. Same blindness the XIRR gate had before
            # `dashboard.py` was moved onto this same book-level record (audit F-47 / F-49).
            #
            # DATE-SCOPED, not retroactive: `book` is rebuilt for each day from
            # `bundle.through(day)`, whose `actions` filter is `a.date <= day`, so a refusal
            # dated 6/3 cannot mark 6/2 — the days before the action stay complete.
            #
            # CONTAINMENT (D38 invariant 1): this branch is unreachable for a ledger with no
            # corporate actions. `unapplied_actions` is appended only by `_reject`, reachable
            # only from `_apply_action`, called only for an "action" event — which exists only
            # if `bundle.actions` is non-empty. So an action-free ledger takes the pre-existing
            # path exactly, with no equal-answer computation that could later drift.
            if book.unapplied_actions:
                incomplete = True  # 無法套用的公司行動 — 當日估值待釐清
            # CR-01 / CR-02 / CR-11 (2026-10-04): the same blindness, two more shapes. A 賣超
            # netted back to exactly zero shares is dropped by the holdings loop below, flag and
            # all, so the per-holding `h.oversold` test never saw it — and a dividend refused for
            # having no position has no holding at all. Both stay on the BOOK; read them here.
            # DATE-SCOPED the same way: `bundle.through(day)` only holds what has happened by
            # `day`, so the days before the oversell / the refused payout stay complete.
            # K3 (2026-10-05): the third shape — a payout refused on an open short or on a
            # ticker an EXCHANGE vacated flags its carrier, and a FLAT carrier (successor sold
            # out, short covered to 0) is dropped below with that flag. The held one was always
            # caught by `h.unbookable_dividend`; this makes the flat one agree with it.
            if book.oversold_positions or book.orphan_dividends or book.unbookable_dividends:
                incomplete = True  # 賣超 / 股利無對應持倉 / 股利無法入帳 — 當日估值待釐清
            # Options spec §4.4: a close row the replay refused is on the BOOK (it may have no
            # position at all), and B is withheld while one exists — date-scoped like the rest.
            if book.refused_closes:
                incomplete = True  # 選擇權平倉列無法入帳 — 當日估值待釐清
            for h in book.holdings:
                if h.shares == _ZERO:
                    continue
                inst = bundle.instruments.get(h.symbol)
                if inst is not None and inst.is_option:
                    options_excluded = True   # R3: excluded, NOT incomplete
                    continue
                # `unbookable_action` belongs here for a STRONGER reason than 賣超: a skipped
                # corporate action leaves `shares` in PRE-action terms while `price_history` is
                # global and already POST-action, so `price * shares` is not merely incomplete,
                # it is wrong by the action's whole ratio (a 3-for-1 understates the position
                # threefold). Omitting it let that product into the trend and net-worth series
                # as though valid, and unflagged.
                if h.oversold or h.unbookable_action or h.unbookable_close:
                    incomplete = True  # 賣超 / 股數待釐清 — value undefined, so not counted
                    continue
                # Y3 (owner ruling 2026-10-05): `unbookable_dividend` is NOT in that branch any
                # more. It means a payout was refused (an open short, a vacated ticker); the
                # SHARES are right and the price is global and current, so the value is real —
                # `Holding`'s taxonomy and `pnl.value_holdings` (which never suppresses it, so the
                # KPI total counts it) already said so. Dropping it here made this series the
                # one reader that disagreed: the trend's last point read 0 against a 530,000 KPI
                # total for the same day, and net worth fell by the position on the payout day
                # with no cash or share moving. The day is still marked — the payout is 待釐清,
                # and B is withheld on it — but the position is counted below like any other.
                if h.unbookable_dividend:
                    incomplete = True  # 股利無法入帳 — value kept (also on the book, K3 above)
                # A DECLARED short is NOT excluded (2026-07-31 ruling): it is a real priced
                # position and its NEGATIVE market value is a liability the series must carry.
                # Dropping it (the old `shares < 0` test) overstated both the trend and net worth
                # by the short's full market value while cash still held the proceeds — the two
                # halves of one trade counted asymmetrically.
                entry = _entry_at_or_before(price_history.get(h.symbol, []), day)
                if entry is None:
                    incomplete = True
                    continue
                # §5.1(d): a stored close is as traded on ITS OWN date, so a carried-forward
                # price meets a share count the replay has already re-denominated. Re-express
                # it into `day`'s terms — divide by the splits in `(pd, day]` — instead of
                # multiplying two mismatched units. The trend cannot precompute this the way
                # `dashboard.py` does: the factor depends on the valuation day, which varies
                # per point, so it is applied at lookup time.
                #
                # ⚠ NEVER mark the split day `incomplete` here (trap #8). This loop omits the
                # HOLDING, not the day, and still emits `total_value` below — so "flag it
                # instead" would drop the position's entire market value on every split date
                # (measured: a 95% net-worth cliff on a 1-for-20). An error of `ratio` replaced
                # by an error of 100% is not an improvement, and it breaks §2.1 outright.
                priced_on, as_traded = entry
                price = price_in(actions, h.symbol, as_traded,
                                 priced_on=priced_on, valued_on=day)
                rate = _fx_at(fx_history, day, h.quote_ccy, reporting)
                if rate is None:
                    incomplete = True
                    continue
                total += convert(price * h.shares, rate)
            net_invested = _ZERO
            for d, amt in converted:
                if counts_by(d, day):
                    net_invested += amt
            points.append(TrendPoint(date=day, total_value=total,
                                     net_invested=net_invested, incomplete=incomplete))
            day += timedelta(days=1)

        return TrendSeries(points=points, reporting_currency=reporting, available=True,
                           options_value_excluded=options_excluded)
    except ArithmeticError as exc:
        raise UnbookableLedgerError(_uncomputable_message(None)) from exc
# ==== FROZEN END ====


# ==== FROZEN BEGIN 473fe5a:portfolio_dash/portfolio/returns.py L281-L308 ====
# sha256=55fefbdbeaa5492b60a4dc4e4f25351191fca54d703d34695c3156b8c4b82913
def replay_dividend_refusals(bundle: LedgerBundle) -> DividendRefusals:
    """Replay *bundle* once and return its :class:`DividendRefusals` (⑪ / X1).

    For a reader that holds the ledgers but not a book — the cash API's one snapshot
    (``api/routers/cash.py::load_cash_ledgers``), the 現金收支明細 exports, the ledger tab, the
    printed ledger report, the drawer's 配息史, the AI-facing dividend rows, the trend. A
    reader that already replayed the same bundle calls :func:`dividend_refusals` on its own
    book instead and pays for no second replay.

    The replay is run exactly as ``build_dashboard`` runs it: unregistered rows dropped first
    (they cannot be booked, and ``cash_balances`` skips them too), ``allow_oversell=True`` so a
    賣超 or a refused dividend is recorded on the book rather than raised. A ledger the replay
    refuses OUTRIGHT (a DRIP row with no share count, an uncomputable value — the dashboard
    answers 422 naming the row) yields no refusal: there is none to read, so every payout
    counts and none is marked, as before ⑪, on every surface and every guard alike, because
    they all read this one answer (owner ruling X2, 2026-10-05: keep).
    """
    try:
        book = build_book(bundle.without_unregistered(), allow_oversell=True)
    except (ValueError, KeyError, ArithmeticError):
        return DividendRefusals()
    return dividend_refusals(book)


def replay_refusals(bundle: LedgerBundle) -> list[Dividend]:
    """``Book.refused_dividends`` of the dashboard-path replay of *bundle* (⑪) — the rows of
    :func:`replay_dividend_refusals`, for a reader that needs no reasons (the trend)."""
    return list(replay_dividend_refusals(bundle).refused)
# ==== FROZEN END ====


# ==== FROZEN BEGIN 473fe5a:portfolio_dash/portfolio/networth.py L61-L169 ====
# sha256=3d7ca7f98107da55887a97b0a2b1732d68427af3ccfbc32be622794ade1fc6af
def _day_steps(statement: list[tuple[CashLine, Decimal]]) -> list[tuple[date, Decimal]]:
    """Collapse a date-ordered running statement to end-of-day balances.

    ``running_statement`` is chronological (date asc, same-day credits before debits),
    so the LAST running balance carrying a given date is that day's end-of-day balance.
    Returns an ascending ``(date, balance)`` step list (one entry per distinct date)."""
    by_day: dict[date, Decimal] = {}
    for line, running in statement:
        by_day[line.date] = running
    return sorted(by_day.items())


def _balance_on(steps: list[tuple[date, Decimal]], on: date) -> Decimal:
    """End-of-day running balance at-or-before ``on`` (carry-forward); 0 before day one."""
    idx = bisect_right(steps, on, key=lambda item: item[0])
    if idx == 0:
        return _ZERO
    return steps[idx - 1][1]


def daily_cash_series(
    movements: Sequence[_MovementRow],
    fx_conversions: Sequence[_FxRow],
    transactions: Sequence[_TxRow],
    dividends: Sequence[_DivRow],
    instruments: dict[str, Instrument],
    fx_history: FxHistory,
    reporting: Currency,
    *,
    end: date,
) -> dict[date, CashDay]:
    """Per-day reporting-currency cash total, replaying each pool's dated lines.

    Returns a dense ``{day: CashDay}`` over ``[first_line_date, end]``. For each day
    every pool's carry-forward end-of-day balance is converted at that day's FX and
    summed; a non-zero pool with no on-or-before FX marks the day incomplete and is
    excluded from the sum (a zero-balance pool missing FX is silently skipped). The
    reporting pool needs no FX. Unregistered symbols are skipped exactly as
    ``cash_balances`` skips them (an unbookable row never poisons the series)."""
    # Enumerate every touched (account, ccy) pool via the verified balance engine.
    pool_keys = sorted(
        cash_balances(movements, fx_conversions, transactions, dividends, instruments),
        key=lambda k: (k[0], k[1].value),
    )
    if not pool_keys:
        return {}

    # Per pool: its currency + ascending end-of-day balance step list.
    pools: list[tuple[Currency, list[tuple[date, Decimal]]]] = []
    first_date: date | None = None
    for account_id, ccy in pool_keys:
        steps = _day_steps(running_statement(pool_lines(
            account_id, ccy, movements, fx_conversions, transactions, dividends,
            instruments)))
        if not steps:
            continue  # pool with only unregistered rows -> no lines
        pools.append((ccy, steps))
        pool_start = steps[0][0]
        first_date = pool_start if first_date is None else min(first_date, pool_start)

    if first_date is None or end < first_date:
        return {}

    out: dict[date, CashDay] = {}
    day = first_date
    while day <= end:
        total = _ZERO
        incomplete = False
        for ccy, steps in pools:
            balance = _balance_on(steps, day)
            if balance == _ZERO:
                continue  # a zero pool contributes nothing and needs no FX
            if ccy == reporting:
                total += balance
                continue
            rate = _fx_at(fx_history, day, ccy, reporting)
            if rate is None:
                incomplete = True  # non-zero pool, no on-or-before FX -> honest gap
                continue
            total += convert(balance, rate)
        out[day] = CashDay(reporting_total=total, incomplete=incomplete)
        day += timedelta(days=1)
    return out


def compose_net_worth(
    trend: TrendSeries, cash_by_date: dict[date, CashDay]
) -> TrendSeries:
    """Enrich each trend point with ``net_worth = total_value + cash_that_day``.

    Pure composition: aligns on the trend's date axis (cash before its first line = 0),
    and returns a NEW series whose points differ from the input ONLY in ``net_worth``
    (every pre-existing field is copied byte-identically). ``net_worth`` is left None on
    a cash-incomplete day (a non-zero pool lacked FX) so the frontend draws a gap rather
    than a fabricated total; on a holdings-incomplete day it mirrors ``total_value`` (the
    same partial value the existing lines draw, flagged by the shared incomplete marker).
    """
    if not trend.points:
        return trend
    new_points: list[TrendPoint] = []
    for point in trend.points:
        cash = cash_by_date.get(point.date)
        if cash is None:
            cash_total, cash_incomplete = _ZERO, False  # date precedes first cash line
        else:
            cash_total, cash_incomplete = cash.reporting_total, cash.incomplete
        net_worth = None if cash_incomplete else point.total_value + cash_total
        new_points.append(point.model_copy(update={"net_worth": net_worth}))
    return trend.model_copy(update={"points": new_points})
# ==== FROZEN END ====


# ==== FROZEN BEGIN 473fe5a:portfolio_dash/portfolio/benchmark_counterfactual.py L111-L200 ====
# sha256=84eeac6a4fc72ca410679eb15de69f8defb23e8030097a921ca5d349133397a5
_NO_FLOWS = "尚無投入流量，無法計算基準對照"
_NONE_PLACEABLE = "所有投入流量都落在基準指數的歷史之外或缺少基準，無法計算基準對照"


def _close_at_or_before(
    closes: Sequence[tuple[date, Decimal]], on: date
) -> Decimal | None:
    """The latest close at-or-before ``on``; ``None`` when the series starts later.

    Returns ``None`` for a non-positive close as well: a zero or negative index level is a
    data defect, and it can be neither a denominator (units bought) nor an honest valuation.
    """
    i = bisect_right([d for d, _ in closes], on)
    if i == 0:
        return None
    value = closes[i - 1][1]
    return value if value > _ZERO else None


def counterfactual(
    flows: Sequence[ReportingFlow],
    closes_by_market: Mapping[Market, Sequence[tuple[date, Decimal]]],
) -> Counterfactual:
    """Spend ``flows`` on each market's benchmark instead, and value the result today.

    ``closes_by_market`` holds each market's benchmark closes, ascending, **already in the
    reporting currency**. A market absent from the mapping has no benchmark and its flows are
    reported as uncovered rather than dropped.
    """
    # AI-D49: cash-movement flows (``market is None``) are excluded from BOTH legs and from
    # the coverage ratio. ``uncovered_ratio`` answers 「how much of the money has no index to
    # compare against」 — a market this app cannot cover (MY today). A broker fee is not
    # uncovered money; it is money that was never going to buy an index at all, and folding
    # it in would degrade the headline's label for a reason that has nothing to do with
    # benchmark coverage.
    placeable = [f for f in flows if f.market is not None]
    gross_all = sum((abs(f.amount) for f in placeable), _ZERO)
    if not placeable:
        return Counterfactual(
            available=False, reason=_NO_FLOWS, terminal_value=None, net_invested=None,
            benchmark_return=None, uncovered_markets=(), uncovered_ratio=None, by_market=(),
        )

    units: dict[Market, Decimal] = {}
    invested: dict[Market, Decimal] = {}
    uncovered_gross = _ZERO
    uncovered: set[str] = set()

    for flow in placeable:
        assert flow.market is not None  # `placeable` filtered these out; narrows the type
        closes = closes_by_market.get(flow.market)
        level = None if not closes else _close_at_or_before(closes, flow.on)
        if level is None:
            # No benchmark for this market, or the flow predates its history, or the close
            # on that date is not a usable level. Named, never silently omitted.
            uncovered_gross += abs(flow.amount)
            uncovered.add(flow.market.value)
            continue
        units[flow.market] = units.get(flow.market, _ZERO) + flow.amount / level
        invested[flow.market] = invested.get(flow.market, _ZERO) + flow.amount

    ratio = (uncovered_gross / gross_all) if gross_all > _ZERO else _ZERO

    legs: list[MarketLeg] = []
    for market in sorted(units, key=lambda m: m.value):
        closes = closes_by_market[market]
        last_on, last_level = closes[-1]
        legs.append(MarketLeg(
            market=market,
            units=units[market],
            terminal_value=units[market] * last_level,
            net_invested=invested[market],
            last_close_on=last_on,
        ))

    if not legs:
        return Counterfactual(
            available=False, reason=_NONE_PLACEABLE, terminal_value=None, net_invested=None,
            benchmark_return=None, uncovered_markets=tuple(sorted(uncovered)),
            uncovered_ratio=ratio, by_market=(),
        )

    terminal = sum((leg.terminal_value for leg in legs), _ZERO)
    net = sum((leg.net_invested for leg in legs), _ZERO)
    return Counterfactual(
        available=True, reason=None, terminal_value=terminal, net_invested=net,
        benchmark_return=terminal - net,
        uncovered_markets=tuple(sorted(uncovered)), uncovered_ratio=ratio,
        by_market=tuple(legs),
    )
# ==== FROZEN END ====

# ---- the public reference names (not part of any frozen region) --------------------------

ref_entry_at_or_before = _entry_at_or_before
ref_fx_at = _fx_at
ref_build_reporting_flows = build_reporting_flows
ref_daily_value_series = daily_value_series
ref_replay_dividend_refusals = replay_dividend_refusals
ref_replay_refusals = replay_refusals
ref_daily_cash_series = daily_cash_series
ref_compose_net_worth = compose_net_worth
ref_counterfactual = counterfactual

__all__ = [
    "FxHistory",
    "PriceHistory",
    "ref_build_reporting_flows",
    "ref_compose_net_worth",
    "ref_counterfactual",
    "ref_daily_cash_series",
    "ref_daily_value_series",
    "ref_entry_at_or_before",
    "ref_fx_at",
    "ref_replay_dividend_refusals",
    "ref_replay_refusals",
]
