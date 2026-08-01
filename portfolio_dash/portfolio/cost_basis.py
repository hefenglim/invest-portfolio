"""Chronological ledger replay → open holdings (cost basis) + realized P&L."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from portfolio_dash.portfolio.results import Book, Holding, RealizedPnL, RealizedRow
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import CASH_DIVIDEND_TYPES, Side
from portfolio_dash.shared.models.ledger import Dividend, OpeningInventory, Transaction

_ZERO = Decimal("0")


class OversellError(Exception):
    """A sell quantity exceeds held shares (input error vs short sale — require confirm)."""


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
    # A dividend arrived while the short was open — skipped, never booked (see the dividend
    # branch). Surfaced so the position is visibly 待釐清 instead of quietly incomplete.
    unbookable_dividend: bool = False


def build_book(
    transactions: list[Transaction],
    dividends: list[Dividend],
    opening: list[OpeningInventory],
    instruments: dict[str, Instrument],
    *,
    allow_oversell: bool = False,
) -> Book:
    """Replay the ledger in date order; return open holdings, realized P&L, gross invested.

    Same-day ordering: opening (0) -> buy (1) -> sell (2) -> dividend (3).

    Oversell (a sell exceeding holdings): by default raise ``OversellError`` (validation
    callers — e.g. the 重算/rebuild action — want to reject it). With ``allow_oversell=True``
    (the dashboard path), DEGRADE GRACEFULLY instead of crashing: net the position to
    negative shares, drop its (now-undefined) cost basis, and emit NO realized row — the
    resulting holding is flagged ``oversold`` with 待釐清 value (decided 2026-06-18). This
    keeps the dashboard alive after an acked oversell; the user fixes it by recording the
    missing opening inventory / buy. It is NOT short-position accounting.
    """

    def quote_ccy(symbol: str) -> Currency:
        inst = instruments.get(symbol)
        if inst is None:
            raise KeyError(f"unknown instrument: {symbol}")
        return inst.quote_ccy

    positions: dict[tuple[str, str], _Position] = {}
    realized_rows: list[RealizedRow] = []
    gross: dict[Currency, Decimal] = defaultdict(lambda: Decimal("0"))

    events: list[tuple[date, int, str, object]] = []
    for oi in opening:
        events.append((oi.build_date, 0, "open", oi))
    for tx in transactions:
        events.append((tx.trade_date, 1 if tx.side is Side.BUY else 2, "tx", tx))
    for dv in dividends:
        events.append((dv.date, 3, "div", dv))
    events.sort(key=lambda e: (e[0], e[1]))

    for _d, _p, kind, ev in events:
        if kind == "open":
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
                            f"sell {ev.quantity} > held {pos.shares} for {ev.symbol}"
                        )
                    # Graceful: net to a negative (賣超) position; its cost basis is
                    # undefined, so drop it and emit no realized row (待釐清). UNCHANGED —
                    # only the marker below is new, and it never clears.
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
        payback = dividend_portion / original_total if original_total != _ZERO else _ZERO
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
                short_open=pos.short_shares > _ZERO,
                unbookable_dividend=pos.unbookable_dividend,
            )
        )

    realized_by_ccy: dict[Currency, Decimal] = defaultdict(lambda: Decimal("0"))
    for r in realized_rows:
        realized_by_ccy[r.quote_ccy] += r.realized

    return Book(
        holdings=holdings,
        realized=RealizedPnL(rows=realized_rows, by_currency=dict(realized_by_ccy)),
        gross_invested=dict(gross),
    )
