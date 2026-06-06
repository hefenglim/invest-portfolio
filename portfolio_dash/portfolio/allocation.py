"""Sector allocation and combined multi-currency value views (reporting currency)."""

from collections import defaultdict
from collections.abc import Callable
from decimal import Decimal

from portfolio_dash.portfolio.results import CombinedView, Holding, SectorAllocation
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.fx import convert
from portfolio_dash.shared.models.assets import Instrument

_ZERO = Decimal("0")
FxRate = Callable[[Currency, Currency], Decimal]


def sector_allocation(
    valued_holdings: list[Holding],
    instruments: dict[str, Instrument],
    current_fx: FxRate,
    reporting: Currency,
) -> SectorAllocation:
    """Reporting-currency value and weight per sector. Stale (unpriced) holdings skipped."""
    by_sector: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    total = _ZERO
    for h in valued_holdings:
        if h.market_value is None:
            continue
        inst = instruments.get(h.symbol)
        if inst is None:
            raise KeyError(f"unknown instrument: {h.symbol}")
        value = convert(h.market_value, current_fx(h.quote_ccy, reporting))
        by_sector[inst.sector] += value
        total += value
    weights = {
        sector: (value / total if total != _ZERO else _ZERO)
        for sector, value in by_sector.items()
    }
    return SectorAllocation(
        by_sector=dict(by_sector), weights=weights, reporting_currency=reporting
    )


def combined_view(
    valued_holdings: list[Holding],
    current_fx: FxRate,
    reporting: Currency,
) -> CombinedView:
    """Per-currency market value plus a blended reporting-currency total."""
    by_ccy: dict[Currency, Decimal] = defaultdict(lambda: Decimal("0"))
    reporting_total = _ZERO
    for h in valued_holdings:
        if h.market_value is None:
            continue
        by_ccy[h.quote_ccy] += h.market_value
        reporting_total += convert(h.market_value, current_fx(h.quote_ccy, reporting))
    return CombinedView(
        by_currency_value=dict(by_ccy),
        reporting_total_value=reporting_total,
        reporting_currency=reporting,
    )
