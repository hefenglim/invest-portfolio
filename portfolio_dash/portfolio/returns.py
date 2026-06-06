"""Returns: per-currency total return + blended reporting total, and reporting XIRR."""

import math
from collections.abc import Callable
from datetime import date
from decimal import Decimal

from pyxirr import InvalidPaymentsError
from pyxirr import xirr as _xirr

from portfolio_dash.portfolio.results import (
    Book,
    CurrencyReturn,
    Holding,
    ReturnSummary,
)
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.fx import convert
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.ledger import Dividend, OpeningInventory, Transaction

_ZERO = Decimal("0")
FxRate = Callable[[Currency, Currency], Decimal]


def total_return(
    book: Book,
    valued_holdings: list[Holding],
    current_fx: FxRate,
    reporting: Currency,
) -> ReturnSummary:
    """Per-currency realized+unrealized and rate (vs gross invested); blended at spot.

    Expects ``valued_holdings`` already passed through ``value_holdings`` (a holding with
    ``unrealized_pnl is None`` — stale or never valued — is skipped). Note: a stale
    holding's unrealized is excluded from the numerator while its cost stays in
    ``gross_invested`` (denominator), so the simple rate UNDERSTATES returns when stale
    positions are present. The rate is a secondary glance metric; XIRR is the rigorous one.
    """
    unrealized: dict[Currency, Decimal] = {}
    for h in valued_holdings:
        if h.unrealized_pnl is not None:
            unrealized[h.quote_ccy] = unrealized.get(h.quote_ccy, _ZERO) + h.unrealized_pnl

    ccys = set(book.gross_invested) | set(book.realized.by_currency) | set(unrealized)
    by_ccy: dict[Currency, CurrencyReturn] = {}
    reporting_total = _ZERO
    for ccy in ccys:
        realized_c = book.realized.by_currency.get(ccy, _ZERO)
        unreal_c = unrealized.get(ccy, _ZERO)
        gross_c = book.gross_invested.get(ccy, _ZERO)
        total_c = realized_c + unreal_c
        by_ccy[ccy] = CurrencyReturn(
            realized=realized_c,
            unrealized=unreal_c,
            total_return=total_c,
            gross_invested=gross_c,
            rate=(total_c / gross_c) if gross_c != _ZERO else None,
        )
        reporting_total += convert(total_c, current_fx(ccy, reporting))

    return ReturnSummary(
        by_currency=by_ccy,
        reporting_currency=reporting,
        reporting_total_return=reporting_total,
    )


DateFxRate = Callable[[date, Currency, Currency], Decimal]


def xirr_reporting(
    transactions: list[Transaction],
    dividends: list[Dividend],
    opening: list[OpeningInventory],
    holdings: list[Holding],
    instruments: dict[str, Instrument],
    fx_at: DateFxRate,
    current_prices: dict[str, Decimal],
    current_fx: FxRate,
    as_of: date,
    reporting: Currency,
) -> Decimal | None:
    """Reporting-currency money-weighted XIRR. Returns None if it cannot be computed.

    Flows: buy - (gross incl. fees+tax), sell + (net), cash dividend + (net), DRIP/stock
    neutral, opening - (original_cost_total at build_date), final market value + at as_of.
    Each flow converted at its trade-date FX; final value at current spot.

    All-or-nothing on missing prices: if ANY held symbol lacks a current price this
    returns None (the terminal value can't be formed), unlike total_return/allocation
    which degrade partially. Also returns None on non-convergence / non-finite results.
    """

    def ccy_of(symbol: str) -> Currency:
        inst = instruments.get(symbol)
        if inst is None:
            raise KeyError(f"unknown instrument: {symbol}")
        return inst.quote_ccy

    dates: list[date] = []
    amounts: list[float] = []

    def add(d: date, ccy: Currency, native: Decimal) -> None:
        dates.append(d)
        amounts.append(float(convert(native, fx_at(d, ccy, reporting))))

    for oi in opening:
        add(oi.build_date, ccy_of(oi.symbol), -oi.original_cost_total)
    for tx in transactions:
        ccy = ccy_of(tx.symbol)
        if tx.side is Side.BUY:
            add(tx.trade_date, ccy, -(tx.quantity * tx.price + tx.fees + tx.tax))
        else:
            add(tx.trade_date, ccy, tx.quantity * tx.price - tx.fees - tx.tax)
    for dv in dividends:
        if dv.type is DividendType.CASH:
            add(dv.date, ccy_of(dv.symbol), dv.net)
        # DRIP / STOCK are neutral (no external cashflow)

    final = Decimal("0")
    for h in holdings:
        price = current_prices.get(h.symbol)
        if price is None:
            return None
        final += convert(price * h.shares, current_fx(h.quote_ccy, reporting))
    if final != _ZERO:
        dates.append(as_of)
        amounts.append(float(final))

    try:
        rate = _xirr(dates, amounts)
    except InvalidPaymentsError:
        # No sign change in the cashflow series (e.g. all outflows) — not computable.
        return None
    if rate is None or not math.isfinite(rate):
        # Non-finite (e.g. conflicting same-date flows yield inf) — never surface it.
        return None
    return Decimal(str(rate))
