"""Dashboard contract models — the data shape web_ui (and later llm_insight) binds to.

All money/quantity/rate fields are Decimal at full precision; display formatting
(thousands separators, decimal places) is a template concern, never done here.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import (
    BaseModel,
    Field,
    PrivateAttr,
    SerializerFunctionWrapHandler,
    model_serializer,
)

from portfolio_dash.forex.results import FXSummary
from portfolio_dash.portfolio.results import (
    CombinedView,
    OpenedPosition,
    OversoldPosition,
    RealizedPnL,
    RealizedRow,
    RefusedClose,
    ReturnSummary,
    SectorAllocation,
    UnappliedAction,
    UnbookableDividend,
)
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.ledger import Dividend
from portfolio_dash.shared.money import quantize_amount


class HoldingRow(BaseModel):
    """Flattened holding row: all ``Holding`` fields + instrument/account enrichment."""

    account_id: str
    #: The account as a ``{account:<id>}`` token (``shared/account_ref.py``, I-10) — resolved
    #: to the display name by the fetch layer (``web/api.js``), never an English name here.
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
    # QA-06: the position was FLAT when a DRIP/STOCK dividend added shares, so it exists only
    # because of that payout and its basis is zero (see Holding.revived_by_dividend). Unlike
    # the two flags above nothing is wrong or missing — `unrealized_pnl` really is the whole
    # market value — so the UI states the CAUSE, not a warning that the numbers are unreliable.
    revived_by_dividend: bool = False
    # M1-03 / D21: whose dividends `payback_ratio` is built on, when a SPINOFF carved this
    # position's basis in (see Holding). The UI labels the ratio 「承接自 <parent>：分拆時承接
    # 配息 X・自身配息 Y」 wherever it shows it; all three are None on a position no SPINOFF fed.
    payback_from_symbol: str | None = None
    payback_carried_dividends: Decimal | None = None
    payback_own_dividends: Decimal | None = None
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
    grand cell. On a ledger without an option contract it EQUALS
    ``KpiSummary.total_market_value`` / ``.unrealized_total`` by construction (identical
    terms, merely regrouped); on an option ledger the cells are STOCK-ONLY (OPT-D5) and the
    KPI is the grand cell PLUS ``OptionsBlock.market_value`` / ``.unrealized`` (spec §6.2,
    「其中選擇權」). 缺價 (missing-price) holdings are
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
    # 累計總損益（含匯兌） as SERVED (owner rulings 2026-10-22): ``total_value − net_invested`` at
    # the reporting currency's minor unit (:func:`trend_pnl`), so the trend tooltip only
    # formats it — and None (the tooltip's 「—」) on an ``incomplete`` day, whose value is not
    # one to show. The name is short on purpose: the dashboard serves one point per day of the
    # ledger's life (2,179 on a prod-sized ledger). Set on the dashboard's SERVED series only,
    # once per build — on the days from the settlement store as they are built
    # (``dashboard._stored_point``) and on the live days by :func:`with_trend_pnl`; None on
    # every internal series too (the replay, the net-worth composition, the settlement worker),
    # which the engine-equality pins compare with v0.1.31's own. Display-only: never an input,
    # never stored.
    pnl: Decimal | None = None


def trend_pnl(total_value: Decimal, net_invested: Decimal, reporting: Currency, *,
              incomplete: bool) -> Decimal | None:
    """A trend day's 累計總損益（含匯兌）: ``total_value − net_invested``, quantized to the
    reporting currency's minor unit for DISPLAY (``quantize_amount``: TWD 0 dp, USD / MYR 2 dp,
    ROUND_HALF_UP). The ONE owner of the tooltip's figure (owner ruling 2026-10-22): the
    tooltip used to subtract the two Decimal strings in JS floats.

    **None on an ``incomplete`` day** (owner ruling 2026-10-22, the KPI B gate's own rule: an
    unreliable figure is not shown). Such a day's ``total_value`` leaves out a holding whose
    value is unknown or 待釐清 — a missing price, a 賣超, a dividend the replay refused, a
    corporate action it could not apply, an option close it refused — so the difference is not
    the day's result. The tooltip prints 「—」 and keeps its 「部分標的當日無價格」 note.

    AI-D41: every flow in ``net_invested`` is converted at its own date's rate and the value at
    that day's, so this is figure B as of that day — the FX-COMPLETE lifetime result, never the
    浮動損益 it was once labelled. Its relation to the KPI (``kpis.total_return_fx_complete``,
    ``dashboard.fx_complete_return``): the SAME subtraction on the trend's LAST point, which B
    keeps at full precision and this rounds for display. B is withheld when that point is
    incomplete — and then this is None too — and also whenever the book records a refused
    option close; the trend marks every day from such a close's date incomplete, so on the last
    day the two gates agree. A 賣超, a refused dividend and an unapplied action withhold B only
    through that same last-day ``incomplete`` (they choose the reason, not the gate). So: on the
    last point this is either B quantized, or None exactly when B is withheld. On earlier days
    there is no KPI to compare with. A loss rounded away to zero reads ``0``, never ``-0``: a
    minus sign in front of a zero is noise, and every sign test would colour it as a loss.
    """
    if incomplete:
        return None
    pnl = quantize_amount(total_value - net_invested, reporting)
    return abs(pnl) if pnl.is_zero() else pnl


class TrendSeries(BaseModel):
    """Daily replay series; ``available=False`` means points is empty + reason in freshness."""

    points: list[TrendPoint]
    reporting_currency: Currency
    available: bool = True
    # Options spec §5 / R3: True when at least one day skipped an open option contract's value
    # (the trend excludes it WITHOUT marking the day incomplete). ``exclude=True`` — never on
    # the wire (the golden payload is unchanged); the dashboard copies it into
    # ``options.trend_excludes_value``, the one place the note is published.
    options_value_excluded: bool = Field(default=False, exclude=True)


def with_trend_pnl(series: TrendSeries) -> TrendSeries:
    """*series* with every point's ``pnl`` set (:func:`trend_pnl`, in the series' own reporting
    currency; None on an incomplete day): the series as SERVED. A copy; *series* is not
    changed.

    The dashboard applies it to its LIVE days, once per build; the days it serves from the
    settlement store get theirs as they are built (``dashboard._stored_point``), so the
    prefix — most of a long ledger's days — is never copied a second time. Derived per build
    rather than while the payload is dumped because a dump runs on EVERY request (a
    result-cache hit included): measured 2026-10-22 at 2,179 points, a serializer deriving it
    per dump added about three times what a precomputed field adds to a request."""
    ccy = series.reporting_currency
    return series.model_copy(update={"points": [
        p.model_copy(update={"pnl": trend_pnl(p.total_value, p.net_invested, ccy,
                                              incomplete=p.incomplete)})
        for p in series.points]})


class PriceFreshness(BaseModel):
    symbol: str
    as_of: date | None  # None = no stored price at all
    stale: bool


class FxFreshness(BaseModel):
    base: Currency
    quote: Currency
    as_of: date | None  # None = pair never stored
    stale: bool
    # Where the rate came from — a provider name, or "derived:USD" for a cross rate
    # computed by `pricing/cross.py` (owner ruling 2026-09-16, M1). Additive; the
    # freshness panel labels a derived row so the reader knows it is not a market quote.
    source: str | None = None


class FxTriangle(BaseModel):
    """One stored-rate triangle: does ``A/B × B/C`` agree with the stored ``A/C``?

    M1 (measured on the demo 2026-09-16, all three rows freshly refreshed and dated
    2026-09-15, none stale): USD/TWD 31.834999 · MYR/TWD 7.803300 · USD/MYR 4.082500, so
    USD/MYR × MYR/TWD = 31.856972 — **+0.0690% above** the stored USD/TWD. The three rates
    are fetched independently and no derivation ties them together, so the reporting-
    currency total depends on WHICH PATH a conversion happens to take: converting
    4,000 MYR → 983.28 USD at spot moved ``reporting_total`` by −40.88 TWD, and deleting
    the row restored it bit-identically. That is a phantom P&L produced by the conversion
    path alone.

    This model is the **check and the disclosure only**. Whether a base currency should be
    picked and the third rate DERIVED as a cross rate (which would make the triangle close
    by construction) is an owner decision, not one to take in a read path. Until it is
    taken, the gap is measured from the rates as stored and shown — never silently
    corrected, and the rates themselves are never mutated (``data-and-pricing.md``: the
    stored rate is the provider's number of record).

    ``implied`` / ``direct`` are quantized to 6 dp and ``gap_pct`` to 4 dp **for display**;
    the underlying reads keep full precision. ``ok`` is judged on the QUANTIZED gap so the
    verdict can never disagree with the number printed beside it.

    **DEF-077 (owner ruling ⑥ A, 2026-09-26) — judged on ONE day.** The three legs are
    compared on their latest COMMON date (not after the valuation day), never latest row
    against latest row: a refresh that has moved one leg to a new day while another is still
    on the previous one measured the market's move between two days and reported it as a
    data inconsistency (verifier J-01 on 3be67db: gap −0.0938%, ``ok: false``, no data
    error). ``compared_on`` is that day and ``implied`` / ``direct`` / ``gap_pct`` are the
    values ON it. ``dates_differ`` says the legs' latest dates are not all the same (the
    page labels it 「日期不同」; ``leg_dates`` names each). With no common date in the lookback
    the triangle **cannot be compared**: ``ok`` is ``None`` (never ``False`` — nothing was
    found to disagree), the three figures are ``None`` and ``reason`` says why.
    """

    via: str  # the two-leg path, e.g. "USD/MYR × MYR/TWD"
    pair: str  # the directly stored pair the path is compared against, e.g. "USD/TWD"
    implied: Decimal | None  # None ⇔ ok is None (no common date)
    direct: Decimal | None
    gap_pct: Decimal | None  # (implied / direct - 1) * 100, in PERCENT
    ok: bool | None  # |gap_pct| <= 0.05 on ``compared_on``; None = cannot be compared
    as_of: date | None = None  # the OLDEST of the three reads (the triangle is only as
    # current as its stalest leg)
    stale: bool = False  # any of the three legs is stale
    # DEF-077 — additive fields (a construction that predates them still validates).
    compared_on: date | None = None  # the latest common date the verdict is judged on
    dates_differ: bool = False  # the three legs' latest dates are not all the same
    leg_dates: dict[str, date] = Field(default_factory=dict)  # "A/B" → that leg's latest
    reason: str | None = None  # zh, set only when ok is None


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


@dataclass(frozen=True)
class UnconvertibleFlow:
    """A flow of the trend (``timeseries.build_reporting_flows``) with no rate on or before its
    own date: the flow's date and its currency, against the reporting currency. Found by
    ``timeseries.first_unconvertible_flow``; its sentences are ``timeseries``' too."""

    on: date
    ccy: Currency
    reporting: Currency

    @property
    def pair(self) -> str:
        return f"{self.ccy.value}/{self.reporting.value}"


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
    # M1 — triangular consistency of the stored FX rates (see FxTriangle). Empty whenever
    # fewer than three pairs closing a triangle were read, which is the normal single- or
    # dual-currency case; a non-empty list with ok=False means a conversion's reporting-
    # currency effect depends on the path it takes. Additive with an empty default so
    # FreshnessReport constructions that predate it still validate.
    fx_triangulation: list[FxTriangle] = Field(default_factory=list)
    # Router-fed exactly like last_backup_at above (process state, not pure calc):
    # build_dashboard leaves this None; the dashboard router fills it from the scheduler
    # router's scheduler_state(). False means nothing on this instance refreshes on a
    # schedule — every 「最後更新」 stamp on the page is only as new as the last manual run.
    scheduler_running: bool | None = None
    # NOT on the wire (a private attribute: never serialized, so every payload is unchanged):
    # the flow ``trend_unavailable_reason`` names, for the 月度成績 live closes, which word a
    # close dated before it differently (``timeseries.trend_unavailable_reason(as_at=)``).
    # Set by the build (``dashboard._build_dashboard``); kept by every copy of the model.
    _trend_unconvertible: UnconvertibleFlow | None = PrivateAttr(default=None)

    @property
    def trend_unconvertible(self) -> UnconvertibleFlow | None:
        """The first flow with no rate on or before its date, when the trend is unavailable
        for want of one (``None`` otherwise, and on a model not built by the dashboard)."""
        return self._trend_unconvertible

    def set_trend_unconvertible(self, flow: UnconvertibleFlow | None) -> None:
        self._trend_unconvertible = flow


class InsightCardStub(BaseModel):
    """Placeholder card shape (llm_insight not built yet; the combiner returns [])."""

    id: str
    title: str
    body: str
    generated_at: datetime


# ---------------------------------------------------------------------------
# The 選擇權 section (options spec §7.1). EVERY new dashboard wire field lives in this block,
# so a ledger with no option serves the golden payload byte-identically (invariant 1).
# ---------------------------------------------------------------------------


class OptionMoney(BaseModel):
    """A per-currency amount and its reporting-currency total (``None`` when FX is missing)."""

    by_currency: dict[Currency, Decimal] = Field(default_factory=dict)
    reporting: Decimal | None = None


class OptionOpenRow(BaseModel):
    """One open contract position, per account (spec §7.1 ``open[]``)."""

    account_id: str
    symbol: str
    #: The display name 「XYZ 2026-12-18 50 Put」.
    contract: str
    underlying: str
    expiry: date
    days_to_expiry: int
    strike: Decimal
    right: Literal["C", "P"]
    direction: Literal["long", "short"]
    contracts: Decimal
    share_equivalents: Decimal
    average_premium: Decimal
    premium_total: Decimal
    #: R2: the last ordinary trade's price (never a quote, never written to ``prices``).
    #: ``None`` only on a degenerate ledger with no ordinary trade behind the position.
    last_price: Decimal | None = None
    last_price_date: date | None = None
    #: ``None`` when the position is 待釐清 (賣超 / a refused close — ``pnl.value_holdings``
    #: suppresses it exactly as it does a stock) or has no last price.
    market_value: Decimal | None = None
    unrealized: Decimal | None = None
    #: A sold put's strike × 100 × contracts; ``None`` otherwise.
    obligation: Decimal | None = None
    #: OPT-D17 — the registered underlying's latest price and its date; both keys are
    #: OMITTED from the wire when the underlying is unregistered or unpriced.
    underlying_price: Decimal | None = None
    underlying_price_date: date | None = None
    past_expiry: bool = False
    unbookable_close: bool = False
    #: 賣超 (an undeclared sell beyond the long lot — ``Holding.oversold``): the count is
    #: negative but this is NOT a sold contract, and its value is 待釐清. Added in W2 beyond the
    #: §7.1 list because a contract has no ``holdings`` row to wear the stock 賣超 badge.
    oversold: bool = False

    @model_serializer(mode="wrap")
    def _omit_unpriced_underlying(self, handler: SerializerFunctionWrapHandler) -> Any:
        out = handler(self)
        if isinstance(out, dict) and self.underlying_price is None:
            out.pop("underlying_price", None)
            out.pop("underlying_price_date", None)
        return out


class OptionObligationSubtotal(BaseModel):
    """「履約義務合計」 per account and currency."""

    account_id: str
    ccy: Currency
    amount: Decimal


class OptionRealizedRow(RealizedRow):
    """One of the replay's own realized rows on an option symbol — the stock
    ``realized.rows`` shape, plus the close reason and the contract's display name."""

    close_reason: str | None = None
    contract: str


class OptionContractRealized(BaseModel):
    """``realized.by_contract[]`` — one contract's life in one account."""

    account_id: str
    symbol: str
    contract: str
    ccy: Currency
    opened: date | None = None
    closed: date | None = None
    #: How it closed: ``expired`` / ``assigned`` / ``exercised``, or ``None`` for a trade.
    close_reason: str | None = None
    #: The zh word for how it closed (§6.1), or ``None`` while it is still open.
    closed_how: str | None = None
    premium_in: Decimal
    premium_out: Decimal
    realized: Decimal


class OptionRealizedYear(BaseModel):
    """``realized.by_year[]``."""

    year: int
    by_currency: dict[Currency, Decimal] = Field(default_factory=dict)
    reporting: Decimal | None = None


class OptionRealizedUnderlying(BaseModel):
    """``realized.by_underlying[]``."""

    underlying: str
    by_currency: dict[Currency, Decimal] = Field(default_factory=dict)
    reporting: Decimal | None = None


class OptionRealized(BaseModel):
    """The section's 已實現 — separate from the stock ``realized``, never summed into it."""

    rows: list[OptionRealizedRow] = Field(default_factory=list)
    by_contract: list[OptionContractRealized] = Field(default_factory=list)
    by_year: list[OptionRealizedYear] = Field(default_factory=list)
    by_underlying: list[OptionRealizedUnderlying] = Field(default_factory=list)
    realized_total: OptionMoney = Field(default_factory=OptionMoney)


class OptionsBlock(BaseModel):
    """``DashboardData.options`` (spec §7.1) — present only when the UNCUT ledger has at
    least one transaction or action on an option instrument."""

    open: list[OptionOpenRow] = Field(default_factory=list)
    obligation_subtotals: list[OptionObligationSubtotal] = Field(default_factory=list)
    market_value: OptionMoney = Field(default_factory=OptionMoney)
    unrealized: OptionMoney = Field(default_factory=OptionMoney)
    refused_closes: list[RefusedClose] = Field(default_factory=list)
    realized: OptionRealized = Field(default_factory=OptionRealized)
    #: R3 — True when at least one trend day skipped an option holding's value.
    trend_excludes_value: bool = False
    #: ``shared/options.py::TREND_EXCLUDES_OPTIONS_NOTE`` when ``trend_excludes_value``.
    note: str | None = None
    #: OPT-D20 — ``shared/options.py::PRINCIPAL_FX_OPTION_NOTE`` while an option is open on
    #: the valuation day (本金匯率效果 is then withheld); ``None`` otherwise.
    principal_fx_note: str | None = None


class HistoryState(BaseModel):
    """Where this build's history came from (PERF spec 2026-10-18 §10 step 6) — internal.

    ``DashboardData.history_state`` is ``exclude=True``: never on the wire, so the dashboard
    payload stays byte-identical (PERF-D5); the browser reads the same facts from
    ``GET /api/settlement/status``. The in-process readers of history read it here:

    * ``strategy/alerts.py``'s ``portfolio_drawdown`` (a peak over the whole series, which
      pushes) is skipped while the history is stale;
    * ``api/insight_service.py::_portfolio_return`` defers (``pending_data``) when a card's
      ``[created, due]`` overlaps the stale range;
    * ``api/snapshots.py`` serves a period close from the store exactly when its end day lies
      before ``live_from`` — the day this build's trend starts its live part — so a 月度成績
      row and the trend never disagree about which version of a day they show.

    ``live_from`` ``None`` means every day of the build is live: ``PD_SETTLE=off``, an
    unusable store, an empty ledger. ``stale_from`` / ``stale_to`` are the days served from the
    PREVIOUS generation although a change dated on them is pending (PERF-D3), already clipped
    to the days this build served from the store. The last four fields are the store state the
    build read, so a second reader in another read snapshot can tell whether it reads the same
    one (``api/snapshots.py``).
    """

    live_from: date | None = None
    stale_from: date | None = None
    stale_to: date | None = None
    generation: int = 0
    settled_through: date | None = None
    held_through: date | None = None
    dirty_day: date | None = None
    seq: int | None = None

    @property
    def stale(self) -> bool:
        """Some served day is the previous generation's (更新前版本)."""
        return self.stale_from is not None

    def overlaps_stale(self, start: date, end: date) -> bool:
        """Whether ``[start, end]`` (either order) shares a day with the stale range."""
        if self.stale_from is None or self.stale_to is None:
            return False
        lo, hi = min(start, end), max(start, end)
        return lo <= self.stale_to and self.stale_from <= hi


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
    # ``Book.oversold_positions`` / ``Book.orphan_dividends`` (CR-01 / CR-02 / CR-11,
    # 2026-10-04), carried for IN-PROCESS readers — the drawer (``api/routers/symbol.py``)
    # names a 賣超 netted back to zero shares and a dividend with no position from these.
    #
    # ``exclude=True``: NOT on the dashboard wire, deliberately, so the spec-17 golden payload
    # is unchanged. The page already shows both, named, where it shows the consequence — the
    # XIRR reason, the FX-complete reason and the trend's incomplete days all read the same
    # book — and a dashboard banner for them is a follow-up that would add these keys to the
    # contract on purpose, not by accident of a model dump.
    oversold_positions: list[OversoldPosition] = Field(default_factory=list, exclude=True)
    orphan_dividends: list[Dividend] = Field(default_factory=list, exclude=True)
    # ``Book.opened_positions`` (S9, 2026-10-04): the drawer's 「部位已結清」 versus 「從未持有」
    # is the REPLAY's fact — did it ever open this position — not a reading of activity rows.
    # Excluded from the wire for the same reason as the two fields above.
    opened_positions: list[OpenedPosition] = Field(default_factory=list, exclude=True)
    # ``Book.unbookable_dividends`` (K3, 2026-10-05): dividends refused on an open short or a
    # vacated ticker, with the position carrying the flag — which may be flat and so have no
    # holding row. The drawer names each one (on the dividend's symbol and on the carrier) and
    # the reconciliation footer reads them; excluded from the wire like the fields above (the
    # dashboard names them through the FX-complete reason and the trend's incomplete days).
    unbookable_dividends: list[UnbookableDividend] = Field(default_factory=list, exclude=True)
    # The 選擇權 section (options spec §7.1). ``None`` on a ledger with no option, and the
    # dashboard router REMOVES the key then — so the golden payload is unchanged (invariant 1).
    options: OptionsBlock | None = None
    # PERF spec §10 step 6: which days came from the settlement store and which are stale
    # (:class:`HistoryState`). ``exclude=True`` — never on the wire (PERF-D5).
    history_state: HistoryState = Field(default_factory=HistoryState, exclude=True)
