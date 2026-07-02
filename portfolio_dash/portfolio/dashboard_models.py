"""Dashboard contract models — the data shape web_ui (and later llm_insight) binds to.

All money/quantity/rate fields are Decimal at full precision; display formatting
(thousands separators, decimal places) is a template concern, never done here.
"""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from portfolio_dash.forex.results import FXSummary
from portfolio_dash.portfolio.results import (
    CombinedView,
    RealizedPnL,
    ReturnSummary,
    SectorAllocation,
)
from portfolio_dash.shared.enums import Currency, Market


class HoldingRow(BaseModel):
    """Flattened holding row: all ``Holding`` fields + instrument/account enrichment."""

    account_id: str
    account_name: str
    symbol: str
    name: str
    market: Market
    sector: str
    board: str
    quote_ccy: Currency
    shares: Decimal
    original_avg: Decimal
    adjusted_avg: Decimal
    original_cost_total: Decimal
    adjusted_cost_total: Decimal
    dividend_portion: Decimal
    payback_ratio: Decimal
    market_price: Decimal | None = None
    market_value: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    capital_gain: Decimal | None = None
    price_stale: bool = False
    price_as_of: date | None = None
    weight: Decimal | None = None
    oversold: bool = False  # 賣超: negative shares, 待釐清 value (see Holding.oversold)


class KpiSummary(BaseModel):
    """Blended reporting-currency KPIs; every figure Optional (honest degradation).

    XIRR is surfaced only here; ``ReturnSummary.xirr`` stays None (single-sourced).
    """

    reporting_currency: Currency
    total_market_value: Decimal | None = None
    total_return: Decimal | None = None
    total_return_rate: Decimal | None = None
    realized_total: Decimal | None = None
    unrealized_total: Decimal | None = None
    xirr: Decimal | None = None
    fx_realized: Decimal | None = None
    fx_unrealized: Decimal | None = None


class DividendYearRow(BaseModel):
    year: int
    by_currency: dict[Currency, Decimal]


class DividendSummary(BaseModel):
    """Native-currency net dividend totals (no FX conversion — exact)."""

    by_year: list[DividendYearRow]
    total_by_currency: dict[Currency, Decimal]


class ExDividendItem(BaseModel):
    """An upcoming dividend event for a held symbol (from pricing's reference data)."""

    symbol: str
    name: str
    ex_date: date
    pay_date: date | None = None
    cash_amount: Decimal | None = None
    stock_amount: Decimal | None = None
    currency: Currency | None = None
    source: str


class DividendProjectionCurrency(BaseModel):
    """Declared gross/net dividend cash flow for one currency (spec 05)."""

    declared_gross: Decimal
    declared_net: Decimal
    events: int


class DividendProjection(BaseModel):
    """Current-year declared dividend projection, per currency (never summed across)."""

    year: int
    by_currency: dict[Currency, DividendProjectionCurrency]
    basis: str = "declared_only"


class TrendPoint(BaseModel):
    date: date
    total_value: Decimal
    net_invested: Decimal
    incomplete: bool = False


class TrendSeries(BaseModel):
    """Daily replay series; ``available=False`` means points is empty + reason in freshness."""

    points: list[TrendPoint]
    reporting_currency: Currency
    available: bool = True


class PriceFreshness(BaseModel):
    symbol: str
    as_of: date | None  # None = no stored price at all
    stale: bool


class FxFreshness(BaseModel):
    base: Currency
    quote: Currency
    as_of: date | None  # None = pair never stored
    stale: bool


class FreshnessReport(BaseModel):
    prices: list[PriceFreshness]
    fx: list[FxFreshness]
    any_stale: bool
    missing_prices: list[str]
    missing_fx: list[str]
    xirr_unavailable_reason: str | None = None
    trend_unavailable_reason: str | None = None
    # Ledger symbols with no Instrument row: their events are EXCLUDED from all
    # computation (cannot be booked without a quote currency) and listed here so the
    # UI can prompt the user to register them (2026-07-02).
    unregistered_symbols: list[str] = Field(default_factory=list)
    # Router-fed (ops/file state, not pure calc): build_dashboard leaves this None;
    # the dashboard router fills it from ops.backup.latest_backup_at() after to_wire.
    last_backup_at: str | None = None


class InsightCardStub(BaseModel):
    """Placeholder card shape (llm_insight not built yet; the combiner returns [])."""

    id: str
    title: str
    body: str
    generated_at: datetime


class DashboardData(BaseModel):
    """One complete dashboard data model — the contract the UI binds to."""

    as_of: datetime
    reporting_currency: Currency
    kpis: KpiSummary
    holdings: list[HoldingRow]
    realized: RealizedPnL
    returns: ReturnSummary | None
    allocation: SectorAllocation | None
    currency_view: CombinedView | None
    fx: FXSummary | None
    dividends: DividendSummary
    ex_dividend_calendar: list[ExDividendItem]
    trend: TrendSeries
    freshness: FreshnessReport
    insights: list[InsightCardStub] = Field(default_factory=list)
    # Optional default: build_dashboard always populates it; the default only avoids
    # breaking direct DashboardData constructions that predate spec 05.
    dividend_projection: DividendProjection | None = None
