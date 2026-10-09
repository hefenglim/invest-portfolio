"""The 選擇權 section — valuation at the last trade price (R2) and the dashboard block (§7.1).

Spec ``docs/spec/2026-10-09-options-ledger.md`` §5 and §7.1. Calculation core, pure: ledger
rows, a replayed ``Book`` and already-valued holdings in; Decimal out. No connection, no
clock — ``portfolio/dashboard.py`` reads the one price per underlying (OPT-D17) and the FX
rate and hands them in.

What lives here, each with ONE definition (plan invariant 6):

* :func:`option_symbols` / :func:`ledger_has_options` — which instruments are contracts, and
  whether a ledger carries one (the block exists iff it does, decided on the UNCUT ledger);
* :func:`last_trade_prices` — R2: an open contract's price is its last ORDINARY trade
  (``close_reason`` rows excluded), per symbol across accounts, on or before the valuation
  day; after an ``OPTION_ADJUST`` the LATER of the destination's own last trade and its
  predecessor's last price ÷ ``ratio_to`` (a refused adjustment propagates nothing). Never a
  quote and never written to ``prices`` (plan invariant 4);
* :func:`split_holdings` / :func:`stock_realized` — the stock-only lists every existing
  dashboard surface keeps (OPT-D5): holdings, weights, allocation, subtotals and the
  ``realized`` section are stocks; the KPI totals add the block;
* :func:`option_summary` — the block itself.

The contract's terms are parsed from its symbol (``Instrument.option``, OPT-D22); the
multiplier is ``shared/options.py``'s alone.
"""

from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from portfolio_dash.portfolio.dashboard_models import (
    OptionContractRealized,
    OptionMoney,
    OptionObligationSubtotal,
    OptionOpenRow,
    OptionRealized,
    OptionRealizedRow,
    OptionRealizedUnderlying,
    OptionRealizedYear,
    OptionsBlock,
)
from portfolio_dash.portfolio.results import Book, Holding, RealizedPnL, UnappliedAction
from portfolio_dash.shared.corporate_actions import CorporateAction, CorporateActionKind
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.fx import convert
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.ledger import LedgerBundle, Transaction
from portfolio_dash.shared.options import (
    ADJUST_KIND_ZH,
    CLOSE_REASON_ZH,
    PRINCIPAL_FX_OPTION_NOTE,
    TREND_EXCLUDES_OPTIONS_NOTE,
    OptionSide,
    OptionTerms,
    option_display_name,
    parse_option_symbol,
    shares_to_contracts,
)

_ZERO = Decimal("0")
FxRate = Callable[[Currency, Currency], Decimal]


@dataclass(frozen=True)
class LastTrade:
    """An open contract's R2 price: the last ordinary trade's premium per share, and its date."""

    price: Decimal
    on: date


def option_symbols(instruments: Mapping[str, Instrument]) -> frozenset[str]:
    """Every registered option contract (``Instrument.is_option``)."""
    return frozenset(s for s, inst in instruments.items() if inst.is_option)


def ledger_has_options(bundle: LedgerBundle) -> bool:
    """Whether *bundle* carries at least one transaction or corporate action on an option
    contract — the condition for ``DashboardData.options`` (spec §7.1). Pass the UNCUT
    ledger: a contract traded only after the valuation day still makes the section appear.
    A registered contract nobody traded does not."""
    opts = option_symbols(bundle.instruments)
    if not opts:
        return False
    return (any(t.symbol in opts for t in bundle.transactions)
            or any(a.from_symbol in opts or a.to_symbol in opts for a in bundle.actions))


#: How an ``OPTION_ADJUST`` row is recognised among the replay's refusals: its stored id, or
#: — for an action only being validated, which has none — its terms.
_ActionKey = tuple[object, ...]


def _action_key(account_id: str, on: date, from_symbol: str, to_symbol: str,
                action_id: int | None) -> _ActionKey:
    if action_id is not None:
        return ("id", action_id)
    return ("terms", account_id, on, from_symbol, to_symbol)


def refused_adjustments(unapplied: Iterable[UnappliedAction]) -> frozenset[_ActionKey]:
    """The ``OPTION_ADJUST`` rows the replay REFUSED (``Book.unapplied_actions``), matched by
    action id (by terms for an unsaved action) — they moved nothing, so they move no price."""
    return frozenset(
        _action_key(u.account_id, u.date, u.from_symbol, u.to_symbol, u.action_id)
        for u in unapplied if u.kind == CorporateActionKind.OPTION_ADJUST)


def _is_refused(a: CorporateAction, refused: frozenset[_ActionKey]) -> bool:
    return _action_key(a.account_id, a.date, a.from_symbol, a.to_symbol, a.id) in refused


def last_trade_prices(bundle: LedgerBundle, as_of: date, *,
                      unapplied: Collection[UnappliedAction]) -> dict[str, LastTrade]:
    """R2 (spec §5): every option contract's price as of *as_of*.

    One walk in the replay's order — an adjustment precedes the same day's trades. An ordinary
    trade (``close_reason is None``, any account) sets its symbol's price to its own premium
    and date. An ``OPTION_ADJUST`` derives the predecessor's last price ÷ ``ratio_to``, dated
    the predecessor's trade, and the destination keeps the LATER (by date) of that and its own
    last trade — on a tie its own trade (final review R1-M2, 2026-10-10): a destination traded
    after the predecessor's last trade carries the more recent market evidence. A later trade
    on the destination always replaces a derived price, which is dated before the adjustment.
    An adjustment the replay refused (*unapplied* — pass ``Book.unapplied_actions``; required,
    so a forgotten argument is a ``TypeError``, never a silently propagated price) moves no
    position, so it moves no price either. A close row (price 0) never sets a price: an expiry
    is not a market. Rows after *as_of* are ignored.
    """
    opts = option_symbols(bundle.instruments)
    if not opts:
        return {}
    refused = refused_adjustments(unapplied)
    # (date, rank, sequence, event): an action ranks before the day's trades (the replay's
    # EventPriority order), and each list keeps its own ledger order within a rank.
    events: list[tuple[date, int, int, Transaction | CorporateAction]] = []
    for seq, t in enumerate(bundle.transactions):
        if t.symbol in opts and t.close_reason is None and t.trade_date <= as_of:
            events.append((t.trade_date, 1, seq, t))
    for seq, a in enumerate(bundle.actions):
        if (a.kind is CorporateActionKind.OPTION_ADJUST and a.date <= as_of
                and not _is_refused(a, refused)):
            events.append((a.date, 0, seq, a))
    events.sort(key=lambda e: (e[0], e[1], e[2]))
    out: dict[str, LastTrade] = {}
    for _day, _rank, _seq, ev in events:
        if isinstance(ev, Transaction):
            out[ev.symbol] = LastTrade(price=ev.price, on=ev.trade_date)
            continue
        before = out.get(ev.from_symbol)
        if before is None:
            continue
        own = out.get(ev.to_symbol)
        if own is None or before.on > own.on:      # the later wins; a tie keeps the own trade
            out[ev.to_symbol] = LastTrade(price=before.price / ev.ratio_to, on=before.on)
    return out


def split_holdings(
    holdings: Sequence[Holding], options: frozenset[str],
) -> tuple[list[Holding], list[Holding]]:
    """``(stocks, options)`` in the input order — the stock list is what every pre-options
    surface keeps (holdings, weights, allocation, subtotals: OPT-D5)."""
    stocks = [h for h in holdings if h.symbol not in options]
    opts = [h for h in holdings if h.symbol in options]
    return stocks, opts


def stock_realized(realized: RealizedPnL, options: frozenset[str]) -> RealizedPnL:
    """The ``realized`` section without option rows (OPT-D5: the option line is separate,
    never summed into it); totals re-summed per currency in row order."""
    rows = [r for r in realized.rows if r.symbol not in options]
    by_ccy: dict[Currency, Decimal] = {}
    for r in rows:
        by_ccy[r.quote_ccy] = by_ccy.get(r.quote_ccy, _ZERO) + r.realized
    return RealizedPnL(rows=rows, by_currency=by_ccy)


def has_open_option(holdings: Iterable[Holding], options: frozenset[str]) -> bool:
    """OPT-D20: an option position is open on the valuation day (any non-zero count)."""
    return any(h.symbol in options and h.shares != _ZERO for h in holdings)


def is_past_expiry(terms: OptionTerms, as_of: date) -> bool:
    """A contract whose expiry is BEFORE *as_of* (spec §5). Held open past it, the position
    keeps its last-trade value (R2) and is prompted by the expiry inbox (§7.2)."""
    return terms.expiry < as_of


def past_expiry_holdings(holdings: Iterable[Holding], instruments: Mapping[str, Instrument],
                         as_of: date) -> list[Holding]:
    """Every option position the BOOK still holds (non-zero count) whose contract expired
    before *as_of*, in the input order — what the 「待確認到期」 inbox lists (spec §7.2: the
    book's position, per account × contract; skips are the inbox's own state)."""
    out: list[Holding] = []
    for h in holdings:
        inst = instruments.get(h.symbol)
        terms = inst.option if inst is not None else None
        if terms is not None and h.shares != _ZERO and is_past_expiry(terms, as_of):
            out.append(h)
    return out


def _money(amounts: Mapping[Currency, Decimal], fx_rate: FxRate,
           reporting: Currency) -> OptionMoney:
    """Per-currency amounts and their reporting-currency total (``None`` on a missing rate —
    the KPI's all-or-nothing rule, never a partial sum)."""
    by_ccy = {c: amounts[c] for c in sorted(amounts, key=lambda c: c.value)}
    total: Decimal | None
    try:
        total = sum((convert(amount, fx_rate(ccy, reporting)) for ccy, amount in by_ccy.items()),
                    _ZERO)
    except KeyError:
        total = None
    return OptionMoney(by_currency=by_ccy, reporting=total)


def _open_row(h: Holding, terms: OptionTerms, last: LastTrade | None,
              underlying: tuple[Decimal, date] | None, as_of: date) -> OptionOpenRow:
    short = h.shares < _ZERO
    shares = abs(h.shares)            # share-equivalents held (contracts × 100)
    # 待釐清 (spec §7.1's sign pin): a 賣超 discarded the basis and a refused close left the
    # count unreconciled, so the position has no figure to state — no price, no value (already
    # None from `value_holdings`), and no 履約義務 either: strike × a count that is itself in
    # question is not an obligation (final review R1-M1 / N2).
    unresolved = h.oversold or h.unbookable_close
    priced = last if not unresolved else None
    obligation = (terms.strike * shares
                  if short and terms.right == "P" and not unresolved
                  else None)   # strike × 100 × contracts — a SOLD put only, never a 待釐清
    return OptionOpenRow(
        account_id=h.account_id, symbol=h.symbol, contract=option_display_name(terms),
        underlying=terms.underlying, expiry=terms.expiry,
        days_to_expiry=(terms.expiry - as_of).days, strike=terms.strike, right=terms.right,
        direction="short" if short else "long",
        contracts=shares_to_contracts(shares), share_equivalents=h.shares,
        # Magnitudes (spec §7.1): the premium received or paid per share. avg = total / shares
        # on read is already positive in both directions, except on a 賣超, whose discarded
        # basis (0) over a negative count is -0 — the abs keeps the wire at "0" (R1-N1).
        average_premium=abs(h.adjusted_avg), premium_total=abs(h.adjusted_cost_total),
        last_price=priced.price if priced is not None else None,
        last_price_date=priced.on if priced is not None else None,
        market_value=h.market_value, unrealized=h.unrealized_pnl, obligation=obligation,
        underlying_price=underlying[0] if underlying is not None else None,
        underlying_price_date=underlying[1] if underlying is not None else None,
        past_expiry=is_past_expiry(terms, as_of), unbookable_close=h.unbookable_close,
        oversold=h.oversold)


def _realized_rows(book: Book, options: frozenset[str]) -> list[OptionRealizedRow]:
    out: list[OptionRealizedRow] = []
    for i, row in enumerate(book.realized.rows):
        if row.symbol not in options:
            continue
        terms = parse_option_symbol(row.symbol)
        out.append(OptionRealizedRow(
            **row.model_dump(), close_reason=book.realized_close_reasons.get(i),
            contract=option_display_name(terms) if terms is not None else row.symbol))
    return out


def _closed_how(row: OptionRealizedRow) -> str:
    if row.close_reason is not None:
        return CLOSE_REASON_ZH.get(row.close_reason, row.close_reason)
    return OptionSide.BTC.value if row.kind == "short_cover" else OptionSide.STC.value


def _by_contract(rows: Sequence[OptionRealizedRow], bundle: LedgerBundle,
                 open_keys: set[tuple[str, str]],
                 refused: frozenset[_ActionKey]) -> list[OptionContractRealized]:
    """One entry per (account, contract) with at least one realized row. Premium in / out are
    the realized rows' own two sides (``proceeds_net`` / ``adjusted_cost_removed``: the
    premium received and paid for the contracts that closed), so ``realized`` is exactly
    their difference — an adjusted contract's carried premium included.

    How a flat contract closed is the LAST event that emptied it: its last realized row, or —
    when an applied ``OPTION_ADJUST`` moved the remaining position away after that row — the
    adjustment (「選擇權合約調整」, dated the adjustment, no ``close_reason``; final review
    R1-N3). A same-day trade follows the adjustment (the replay's order), so only an
    adjustment dated AFTER the last realized row closed the contract; a refused one
    (*refused*, :func:`refused_adjustments`) moved nothing."""
    opened: dict[tuple[str, str], date] = {}
    for t in bundle.transactions:
        key = (t.account_id, t.symbol)
        if key not in opened or t.trade_date < opened[key]:
            opened[key] = t.trade_date
    moved_away: dict[tuple[str, str], date] = {}
    for a in bundle.actions:
        if a.kind is CorporateActionKind.OPTION_ADJUST:
            key = (a.account_id, a.to_symbol)
            if key not in opened or a.date < opened[key]:
                opened[key] = a.date
            if not _is_refused(a, refused):
                src = (a.account_id, a.from_symbol)
                if src not in moved_away or a.date > moved_away[src]:
                    moved_away[src] = a.date
    grouped: dict[tuple[str, str], list[OptionRealizedRow]] = {}
    for r in rows:
        grouped.setdefault((r.account_id, r.symbol), []).append(r)
    out: list[OptionContractRealized] = []
    for key, group in grouped.items():
        last = group[-1]
        closed = key not in open_keys
        adjusted_on = moved_away.get(key)
        by_adjustment = adjusted_on is not None and adjusted_on > last.sell_date
        out.append(OptionContractRealized(
            account_id=key[0], symbol=key[1], contract=last.contract, ccy=last.quote_ccy,
            opened=opened.get(key),
            closed=(None if not closed else adjusted_on if by_adjustment
                    else last.sell_date),
            close_reason=last.close_reason if closed and not by_adjustment else None,
            closed_how=(None if not closed else ADJUST_KIND_ZH if by_adjustment
                        else _closed_how(last)),
            premium_in=sum((r.proceeds_net for r in group), _ZERO),
            premium_out=sum((r.adjusted_cost_removed for r in group), _ZERO),
            realized=sum((r.realized for r in group), _ZERO)))
    return out


def option_summary(
    *,
    book: Book,
    valued: Sequence[Holding],
    bundle: LedgerBundle,
    last_trades: Mapping[str, LastTrade],
    underlying_prices: Mapping[str, tuple[Decimal, date]],
    fx_rate: FxRate,
    reporting: Currency,
    as_of: date,
    trend_excludes_value: bool,
) -> OptionsBlock:
    """The ``options`` block (spec §7.1) from the request's ONE replay.

    *book* is the valuation-day replay, *valued* its holdings after ``value_holdings`` (so a
    contract carrying a 待釐清 flag has ``market_value is None``, exactly like a stock), *bundle*
    the valuation-day ledger, *last_trades* :func:`last_trade_prices`, *underlying_prices* the
    latest stored price of each registered underlying (OPT-D17 — never in freshness).
    """
    options = option_symbols(bundle.instruments)
    open_rows: list[OptionOpenRow] = []
    mv: dict[Currency, Decimal] = defaultdict(lambda: _ZERO)
    ur: dict[Currency, Decimal] = defaultdict(lambda: _ZERO)
    obligations: dict[tuple[str, Currency], Decimal] = {}
    for h in valued:
        if h.symbol not in options or h.shares == _ZERO:
            continue
        terms = parse_option_symbol(h.symbol)
        if terms is None:
            continue
        row = _open_row(h, terms, last_trades.get(h.symbol),
                        underlying_prices.get(terms.underlying), as_of)
        open_rows.append(row)
        if h.market_value is not None:
            mv[h.quote_ccy] += h.market_value
        if h.unrealized_pnl is not None:
            ur[h.quote_ccy] += h.unrealized_pnl
        if row.obligation is not None:
            key = (h.account_id, h.quote_ccy)
            obligations[key] = obligations.get(key, _ZERO) + row.obligation
    open_rows.sort(key=lambda r: (r.days_to_expiry, r.account_id, r.symbol))

    rows = _realized_rows(book, options)
    total: dict[Currency, Decimal] = {}
    years: dict[int, dict[Currency, Decimal]] = {}
    unders: dict[str, dict[Currency, Decimal]] = {}
    for r in rows:
        total[r.quote_ccy] = total.get(r.quote_ccy, _ZERO) + r.realized
        per_year = years.setdefault(r.sell_date.year, {})
        per_year[r.quote_ccy] = per_year.get(r.quote_ccy, _ZERO) + r.realized
        terms = parse_option_symbol(r.symbol)
        und = terms.underlying if terms is not None else r.symbol
        per_und = unders.setdefault(und, {})
        per_und[r.quote_ccy] = per_und.get(r.quote_ccy, _ZERO) + r.realized
    open_keys = {(h.account_id, h.symbol) for h in valued if h.shares != _ZERO}
    return OptionsBlock(
        open=open_rows,
        obligation_subtotals=[
            OptionObligationSubtotal(account_id=a, ccy=c, amount=amount)
            for (a, c), amount in sorted(obligations.items(), key=lambda kv: (kv[0][0],
                                                                               kv[0][1].value))
        ],
        market_value=_money(mv, fx_rate, reporting),
        unrealized=_money(ur, fx_rate, reporting),
        refused_closes=list(book.refused_closes),
        realized=OptionRealized(
            rows=rows,
            by_contract=_by_contract(rows, bundle, open_keys,
                                     refused_adjustments(book.unapplied_actions)),
            by_year=[OptionRealizedYear(year=y, **_money(years[y], fx_rate, reporting)
                                        .model_dump())
                     for y in sorted(years)],
            by_underlying=[OptionRealizedUnderlying(underlying=u,
                                                    **_money(unders[u], fx_rate, reporting)
                                                    .model_dump())
                           for u in sorted(unders)],
            realized_total=_money(total, fx_rate, reporting)),
        trend_excludes_value=trend_excludes_value,
        note=TREND_EXCLUDES_OPTIONS_NOTE if trend_excludes_value else None,
        # The SAME predicate the dashboard withholds 本金匯率效果 by (OPT-D20).
        principal_fx_note=(PRINCIPAL_FX_OPTION_NOTE if has_open_option(valued, options)
                           else None),
    )

