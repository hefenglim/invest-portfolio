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
    UnappliedAction,
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
    # A DECLARED short still open (see Holding.short_open). Unlike `oversold` this is a real,
    # priced position: `shares` is negative, the cost fields hold the proceeds received, and
    # every valuation formula works on the signed quantity. The UI must label the two
    # differently — one is a position, the other is an unresolved data problem.
    short_open: bool = False
    # A dividend row landed while this position was short; it was NOT booked (a short
    # pays the dividend in lieu). The position's figures are incomplete by that payout.
    unbookable_dividend: bool = False
    # A corporate action could not be applied and was SKIPPED (see Holding.unbookable_action).
    # Strictly worse than the flag above: `shares` is in PRE-action terms while `market_price`
    # is global and already POST-action, so `market_value`, `unrealized_pnl`, `weight` and
    # `unrealized_pct` are wrong by the action's ratio — not short by one payout. The UI must
    # therefore say something different about it than it says about a missed dividend.
    unbookable_action: bool = False
    # Unrealized return rate for THIS holding, server-computed (audit H1, 2026-07-26):
    # ``unrealized_pnl / original_cost_total``. The denominator is deliberately the ORIGINAL
    # invested cost — the same basis as ``KpiSummary.total_return_rate`` (domain-ledger.md:
    # "total return / original invested cost") and as ``payback_ratio``, so every percentage
    # the UI shows for a holding shares ONE basis. It is also the only SAFE basis: adjusted
    # cost MAY be <= 0 once cumulative cash dividends exceed cost (explicitly legal, never
    # floored), and dividing by it silently FLIPS the sign of the ratio; original cost is a
    # sum of non-negative costs and can never be negative. None when unpriced/oversold (no
    # unrealized) or when original cost is zero.
    unrealized_pct: Decimal | None = None


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
    # AI-D41 (2026-08-24) — the A · B · B−A decomposition. ``total_return`` above is A:
    # Σ_ccy (realized + unrealized) × TODAY'S spot, so the rate is applied to each
    # currency's GAIN and never to its PRINCIPAL. B is the trend's
    # ``total_value − net_invested``, where every flow was converted at ITS OWN
    # trade-date rate — the FX-complete lifetime figure, which the UI used to label
    # 浮動損益. B − A was the principal-FX effect (the content of the 換匯損益 card) until
    # AI-D48 put the trading/financing cash kinds into B; it is now a TWO-term difference,
    # split into the two fields below.
    #
    # ⚠ These are PRESENTED SIDE BY SIDE, never summed. Adding the 換匯損益 figure to A
    # double-counts the cross term (MV − C)(spot − acq) — the same red line
    # domain-ledger.md draws for XIRR, applied to the figure that rule did not cover.
    # ``total_return``'s own definition is UNCHANGED; only its label and B's presence
    # beside it are new, so every golden payload and stored snapshot still reconciles.
    total_return_fx_complete: Decimal | None = None
    principal_fx_effect: Decimal | None = None
    # AI-D48 (2026-08-27) made the decomposition THREE terms, because B now counts the
    # trading/financing cash kinds (REBATE / INTEREST_EXPENSE / BROKER_FEE) and A never did:
    #
    #     B = A + principal_fx_effect + trading_financing_cost
    #
    # Sign is the P&L one — a broker fee is NEGATIVE here (it lowers B) while it RAISES
    # `net_invested`. Without this field `principal_fx_effect` would have gone on being
    # labelled 「本金匯率效果」 while silently carrying the costs too, which is exactly the
    # mislabelling AI-D48 was raised to remove.
    trading_financing_cost: Decimal | None = None
    # Why B is absent, when it is (owner ruling 2026-08-25). Without it the UI could
    # only drop the row, and a row that silently vanishes is indistinguishable from a
    # feature that was never built. Server-owned wording — web never composes it.
    fx_complete_reason: str | None = None
    xirr: Decimal | None = None
    # Observation window (days) the XIRR was measured over; a short window (< 365) is a
    # low-confidence hint surfaced by the UI. None when there are no flows or no XIRR run.
    xirr_window_days: int | None = None
    fx_realized: Decimal | None = None
    fx_unrealized: Decimal | None = None


class HoldingSubtotal(BaseModel):
    """One holdings-filter cell's reporting-currency subtotal (server-computed money).

    A re-AGGREGATION of the SAME per-holding computed values that feed ``KpiSummary`` —
    NOT a new money-of-record formula. ``account_id`` / ``market`` are the filter
    selectors; ``None`` on an axis means "all" on that axis, so ``(None, None)`` is the
    grand cell and EQUALS ``KpiSummary.total_market_value`` / ``.unrealized_total`` by
    construction (identical terms, merely regrouped). 缺價 (missing-price) holdings are
    excluded per cell exactly as the KPI excludes them; oversold positions carry a null
    market value and drop out the same way. Every figure is in the reporting currency via
    the shared FX helper. Money is ``Decimal | None`` — None honours the same
    all-or-nothing FX degradation the KPI uses (the reporting-currency blend could not be
    formed), never a fabricated number. The frontend's 合計 footer and the filtered
    CSV/report SELECT the matching cell and print it; they never sum money client-side.
    """

    account_id: str | None = None
    market: Market | None = None
    total_market_value: Decimal | None = None
    unrealized_total: Decimal | None = None


class DividendYearRow(BaseModel):
    year: int
    by_currency: dict[Currency, Decimal]


class DividendSummary(BaseModel):
    """Native-currency net dividend totals (no FX conversion — exact)."""

    by_year: list[DividendYearRow]
    total_by_currency: dict[Currency, Decimal]
    # Trailing-12-month (the 365 days ending at ``as_of``) net cash dividends, per
    # currency. DISPLAY-ONLY attribution: never summed across currencies, and NEVER
    # fed into returns — dividends are already folded into adjusted cost, so this is a
    # distribution surface, not a second income line. Additive field with an empty
    # default so DashboardData constructions that predate it still validate.
    ttm_net: dict[Currency, Decimal] = Field(default_factory=dict)


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
    # Total net worth = total_value + reporting-currency cash that day (FU-D29 / C8).
    # Additive + display-only: daily_value_series never sets it (stays None); the
    # dashboard combiner fills it via portfolio.networth.compose_net_worth after the
    # trend is built. None on a cash-incomplete day (a non-zero pool lacked FX) so the
    # frontend draws an honest gap. Never a money-of-record input.
    net_worth: Decimal | None = None


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


class BenchmarkMarketLeg(BaseModel):
    """One market's share of the benchmark counterfactual (which index, and what it did)."""

    market: Market
    label: str  # zh-TW benchmark name, server-owned (the web layer never names an index)
    terminal_value: Decimal
    net_invested: Decimal


class BenchmarkComparison(BaseModel):
    """R4 / AI-D43 — 「同一筆錢、同樣的日期，買指數會是多少？」, lifetime.

    ⚠ ``excess`` is measured against **B** (``total_return_fx_complete``), never against A
    (``total_return``). The counterfactual buys its units with reporting-currency money at
    each flow's own trade-date rate, exactly as ``trend.net_invested`` does; A applies FX to
    the gain only (AI-D41), so subtracting the counterfactual from A would contrast two
    different treatments of the principal's FX and call the difference market-beating skill.

    ⚠ ``uncovered_ratio > 0`` means the headline covers only part of the money (MY has no
    benchmark; a flow can also predate an index's stored history). The UI must degrade the
    label rather than print a bare 「超額報酬」 — the same discipline as ``covered_ratio``.
    """

    available: bool
    reason: str | None = None
    terminal_value: Decimal | None = None
    net_invested: Decimal | None = None
    benchmark_return: Decimal | None = None
    excess: Decimal | None = None
    uncovered_markets: list[str] = Field(default_factory=list)
    uncovered_ratio: Decimal | None = None
    by_market: list[BenchmarkMarketLeg] = Field(default_factory=list)


class FreshnessReport(BaseModel):
    prices: list[PriceFreshness]
    fx: list[FxFreshness]
    any_stale: bool
    missing_prices: list[str]
    missing_fx: list[str]
    xirr_unavailable_reason: str | None = None
    trend_unavailable_reason: str | None = None
    # Why the WHOLE ``fx`` block is null, when it is (QA-01, 2026-08-29). Additive with a
    # default of None = the section is present. It is the last-resort companion to
    # ``FXSummary.reporting_unavailable_reason``: that one explains a PARTIAL rollup while
    # the section still renders; this one explains its complete absence, which until now
    # was reported by no field at all — the silence that let an empty account's missing
    # rate erase a correct 33,000 TWD unnoticed. Same shape and same source (the resolver's
    # own message) as ``xirr_unavailable_reason`` directly above.
    fx_unavailable_reason: str | None = None
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
    # Per-(account, market) reporting-currency subtotals so the holdings 合計 footer and
    # the filtered CSV/report can follow the active filter WITHOUT any client money math
    # (see HoldingSubtotal). Additive field with an empty default so DashboardData
    # constructions that predate it still validate; build_dashboard always populates it.
    holdings_subtotals: list[HoldingSubtotal] = Field(default_factory=list)
    realized: RealizedPnL
    returns: ReturnSummary | None
    allocation: SectorAllocation | None
    currency_view: CombinedView | None
    fx: FXSummary | None
    dividends: DividendSummary
    ex_dividend_calendar: list[ExDividendItem]
    trend: TrendSeries
    # R4 / AI-D43: the lifetime index counterfactual. Additive with an honest default so
    # DashboardData constructions predating it still validate; build_dashboard always fills
    # it (with available=False + a reason when it cannot be computed).
    benchmark: BenchmarkComparison | None = None
    freshness: FreshnessReport
    insights: list[InsightCardStub] = Field(default_factory=list)
    # Optional default: build_dashboard always populates it; the default only avoids
    # breaking direct DashboardData constructions that predate spec 05.
    dividend_projection: DividendProjection | None = None
    # Corporate actions the replay REFUSED to book (``Book.unapplied_actions``), carried
    # onto the public read surface for W5 / audit F-17.
    #
    # It is NOT a duplicate of ``HoldingRow.unbookable_action`` and must not be collapsed
    # into one: TWO of the three ways an action goes unapplied leave **no surviving
    # position to flag** (an EXCHANGE that already emptied the source — the flag is dropped
    # with its zero-share carrier — and a source that never existed at all). See
    # :class:`UnappliedAction`. §6.3's reconciliation footer turns red on exactly those
    # cases, and without these rows the drawer would render ⚠ 對帳不一致 with nothing
    # beside it to name the cause, which is the specific defect D33 exists to prevent.
    #
    # Empty by construction on the strict path (``allow_oversell=False`` raises instead), so
    # a non-empty list always means the dashboard path degraded and the share counts in this
    # payload are 待釐清.
    unapplied_actions: list[UnappliedAction] = Field(default_factory=list)
