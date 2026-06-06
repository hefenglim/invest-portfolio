"""Returns: per-currency total return + blended reporting total, and reporting XIRR."""

from collections.abc import Callable
from decimal import Decimal

from portfolio_dash.portfolio.results import (
    Book,
    CurrencyReturn,
    Holding,
    ReturnSummary,
)
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.fx import convert

_ZERO = Decimal("0")
FxRate = Callable[[Currency, Currency], Decimal]


def total_return(
    book: Book,
    valued_holdings: list[Holding],
    current_fx: FxRate,
    reporting: Currency,
) -> ReturnSummary:
    """Per-currency realized+unrealized and rate (vs gross invested); blended at spot."""
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
