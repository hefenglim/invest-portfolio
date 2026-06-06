"""Chronological ledger replay → open holdings (cost basis) + realized P&L."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from portfolio_dash.portfolio.results import Book, Holding, RealizedPnL, RealizedRow
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.ledger import Dividend, OpeningInventory, Transaction

_ZERO = Decimal("0")


class OversellError(Exception):
    """A sell quantity exceeds held shares (input error vs short sale — require confirm)."""


@dataclass
class _Position:
    quote_ccy: Currency
    shares: Decimal = field(default_factory=lambda: Decimal("0"))
    original_total: Decimal = field(default_factory=lambda: Decimal("0"))
    adjusted_total: Decimal = field(default_factory=lambda: Decimal("0"))


def build_book(
    transactions: list[Transaction],
    dividends: list[Dividend],
    opening: list[OpeningInventory],
    instruments: dict[str, Instrument],
) -> Book:
    """Replay the ledger in date order; return open holdings, realized P&L, gross invested.

    Same-day ordering: opening (0) -> buy (1) -> sell (2) -> dividend (3).
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
                cost = ev.quantity * ev.price + ev.fees + ev.tax
                pos.shares += ev.quantity
                pos.original_total += cost
                pos.adjusted_total += cost
                gross[ccy] += cost
            else:
                if ev.quantity > pos.shares:
                    raise OversellError(
                        f"sell {ev.quantity} > held {pos.shares} for {ev.symbol}"
                    )
                frac = ev.quantity / pos.shares
                original_removed = pos.original_total * frac
                adjusted_removed = pos.adjusted_total * frac
                proceeds_net = ev.quantity * ev.price - ev.fees - ev.tax
                realized_rows.append(
                    RealizedRow(
                        account_id=ev.account_id,
                        symbol=ev.symbol,
                        quote_ccy=ccy,
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
            if ev.type is DividendType.CASH:
                existing.adjusted_total -= ev.net
            else:  # DRIP / STOCK add shares at zero cost
                existing.shares += (
                    ev.reinvest_shares if ev.reinvest_shares is not None else _ZERO
                )

    holdings: list[Holding] = []
    for (account_id, symbol), pos in positions.items():
        if pos.shares == _ZERO:
            continue
        original_avg = pos.original_total / pos.shares
        adjusted_avg = pos.adjusted_total / pos.shares
        dividend_portion = pos.original_total - pos.adjusted_total
        payback = dividend_portion / pos.original_total if pos.original_total != _ZERO else _ZERO
        holdings.append(
            Holding(
                account_id=account_id,
                symbol=symbol,
                quote_ccy=pos.quote_ccy,
                shares=pos.shares,
                original_avg=original_avg,
                adjusted_avg=adjusted_avg,
                original_cost_total=pos.original_total,
                adjusted_cost_total=pos.adjusted_total,
                dividend_portion=dividend_portion,
                payback_ratio=payback,
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
