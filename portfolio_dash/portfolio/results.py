"""Computed result models produced by the calculation core."""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.ledger import Dividend


class Holding(BaseModel):
    """An open position with cost basis and (once valued) market fields.

    **Adding a 待釐清 flag? Read this first — it is the rule, not the neighbouring flag.**

    The system has THREE independent mechanisms for "this position exists but something
    about it is not trustworthy", and a new flag must be reasoned onto each one separately:

    1. **the display flag itself** — the boolean below, carried to the wire so the UI can
       name the problem. EVERY flag uses this one.
    2. **valuation suppression** (``portfolio/pnl.py``) — null ``market_value`` /
       ``unrealized_pnl`` / ``capital_gain`` so every aggregate that gates on
       ``market_value is not None`` drops the position automatically.
    3. **XIRR suppression** (``portfolio/dashboard.py``) — declare the money-weighted
       return not computable, with a stated reason.

    The deciding question for (2) and (3) is **"are the SHARES right?"**, because
    ``market_value = price × shares`` and prices are global, current and already reflect
    every corporate action:

    * ``unbookable_dividend`` — shares are RIGHT; only ``adjusted_cost_total`` is short one
      payout. Its market value is genuinely correct, so it takes (1) ONLY.
    * ``oversold`` — shares are negative and the basis was discarded: all three.
    * ``unbookable_action`` — the shares are in PRE-action terms against a POST-action
      price, so the market value is wrong by the action's whole ratio: all three. It gets
      (3) via ``Book.unapplied_actions`` rather than via this flag — see that field for why.
    * ``short_open`` — NOT a 待釐清 flag at all. A declared short is a real, priced position
      whose signed quantity every formula already handles; it takes none of the three.

    (Audit F-49, 2026-08-10: ``unbookable_action`` shipped with (1) only, because the brief
    that produced it said "wire it exactly where ``unbookable_dividend`` is wired". Mirroring
    a neighbour is how the gap was created — hence this paragraph.)
    """

    account_id: str
    symbol: str
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
    # 賣超/oversold: an UNDECLARED sell exceeded holdings (written after an acked oversell).
    # Cost basis + P&L are 待釐清 (discarded — see cost_basis.build_book); the position is
    # excluded from portfolio aggregates and flagged for the user to fix (e.g. record the
    # missing opening inventory / buy). STICKY since 2026-07-31: the discarded basis is not
    # restored by a later buy, so the warning must not be cleared by one either.
    oversold: bool = False
    # WHERE the first oversell happened. NOT a fourth trust mechanism — the docstring's three
    # (display flag / valuation suppression / XIRR suppression) all belong to `oversold` and
    # are unchanged; these are detail ON that flag, so a message can name the day the ledger
    # broke instead of the position's final net quantity (F-16, 2026-08-27: 「部位將為 9.5 股」,
    # a POSITIVE number, offered as proof of a shortfall). None on a ledger replayed strictly,
    # and on a position flagged only by the `shares < 0` arm.
    oversold_on: date | None = None
    oversold_sold: Decimal | None = None
    oversold_held: Decimal | None = None
    # A DECLARED short (`short_sale` on the sell) that is still open. ``shares`` is negative
    # and the cost fields hold the proceeds received, so avg/market_value/unrealized are all
    # meaningful — unlike `oversold`, this is a real, priced position, not an unresolved
    # data problem. Covering it books realized P&L at the covering buy's per-share cost.
    short_open: bool = False
    # A dividend row landed while this position was short. It was NOT booked (a short pays
    # the dividend in lieu — see cost_basis.build_book), so the position's figures are
    # incomplete by exactly that payout and the consumer must say so.
    unbookable_dividend: bool = False
    # A corporate action could not be applied to this position and was SKIPPED on the
    # dashboard path (the strict path raises instead). The share count is therefore in
    # PRE-action terms while prices are global and post-action, so market value and every
    # figure derived from it are wrong until the ledger is fixed. Same 待釐清 posture as
    # the two flags above: never silently correct, never a 500 (E1a/E2/E3/E5/E18/E22).
    # Mechanisms (1) + (2); mechanism (3) is driven by `Book.unapplied_actions`, which sees
    # the two cases this flag structurally cannot (see that field).
    unbookable_action: bool = False


class RealizedRow(BaseModel):
    """One realized event: a sell, or a cash dividend paid after the position closed.

    ``kind`` (audit H2, 2026-07-26) distinguishes the two, because a post-close dividend is
    realized INCOME, not a capital gain:

    * ``"sale"`` — the original meaning. ``shares_sold`` > 0; ``realized`` = net proceeds −
      adjusted cost removed.
    * ``"dividend"`` — a CASH-family dividend whose payment date falls after the position
      already reached zero shares (TW/MY pay weeks after the ex-date, so selling out in
      between is ordinary). There is no cost left to reduce, so the payout is booked here
      instead of vanishing with the closed position. ``shares_sold`` and both cost fields
      are 0; ``proceeds_net`` == ``realized`` == the dividend net; ``sell_date`` is the
      dividend's payment date.

    Consumers that mean CAPITAL GAIN specifically (the tax package's realized-gains sheet)
    MUST filter on ``kind == "sale"`` — the dividend is reported there on its own sheet,
    from the dividend ledger, and counting it twice would misstate taxable income.
    """

    account_id: str
    symbol: str
    quote_ccy: Currency
    sell_date: date
    shares_sold: Decimal
    proceeds_net: Decimal
    original_cost_removed: Decimal
    adjusted_cost_removed: Decimal
    realized: Decimal
    # "short_cover" (2026-07-31): a declared short bought back. The proceeds were received
    # on the SELL date but the gain only realizes when the position is covered, so the row is
    # dated the COVER date — `proceeds_net` is the short's weighted-average sale value and
    # `*_cost_removed` is what the covering buy actually cost, all-in.
    kind: Literal["sale", "dividend", "short_cover"] = "sale"


class RealizedPnL(BaseModel):
    """All realized rows plus per-currency totals."""

    rows: list[RealizedRow]
    by_currency: dict[Currency, Decimal]


class UnappliedAction(BaseModel):
    """One corporate-action row the replay REFUSED to book, recorded at Book level.

    Not a flag on a Holding, and that is the whole point: a corporate action going unapplied
    is a property of the REPLAY, and there are three ways it happens, two of which have no
    surviving position to hang a flag on (audit F-47 + E1, 2026-08-10):

    1. the source survives — ``Holding.unbookable_action`` marks it, and this row as well;
    2. the source is *already empty* (an EXCHANGE moved it away earlier the same day), so
       ``cost_basis`` flags a zero-share position that the holdings loop then DROPS — the
       flag is discarded with its carrier and the skipped action leaves no trace;
    3. the source never existed at all, so there is nothing to flag anywhere.

    Case 2 is E19's laundering one level up: E19 stops a flag being *transferred away*,
    nothing stopped it being *dropped*. Cases 2 and 3 both produce a dashboard that looks
    entirely clean while an action in the ledger was silently ignored.

    Every field exists so the consumer can NAME the problem. A bare count would force the UI
    to say "something went wrong", and this repo's whole 待釐清 vocabulary is built on saying
    which row, in which account, on which date, and why — ``reason`` is the same zh sentence
    the strict path raises, so the two paths explain the refusal identically.
    """

    account_id: str
    date: date
    # `CorporateActionKind | str`, because one of the refusal reasons IS an unknown kind
    # (2026-08-11): a stored row the loader could not convert lands here carrying whatever
    # string the ledger actually holds. Narrowing this to the enum would make the model
    # refuse to describe exactly the row it exists to describe — and, since this is a
    # StrEnum, both branches serialise identically on the wire.
    kind: CorporateActionKind | str
    from_symbol: str
    to_symbol: str
    reason: str


class Book(BaseModel):
    """Output of the ledger replay: open holdings, realized, gross capital deployed."""

    holdings: list[Holding]
    realized: RealizedPnL
    gross_invested: dict[Currency, Decimal]
    # Corporate actions the replay could not apply, in replay (date, then ledger) order.
    # ALWAYS EMPTY on the strict path (``allow_oversell=False``), which raises instead — so a
    # non-empty list means the dashboard path degraded and the book is 待釐清.
    #
    # Consumers: a non-empty list means the SHARE COUNTS in this book are not trustworthy
    # (the position kept its pre-action shares while prices are global and post-action), so
    # anything derived from `shares` — market value, weights, the XIRR terminal value — must
    # be withheld, not merely annotated. `Holding.unbookable_action` cannot serve as that
    # gate: cases 2 and 3 above emit no flagged holding at all.
    #
    # Default empty so every existing ``build_book`` consumer keeps working unchanged, and
    # so a directly-constructed ``Book`` (tests, fixtures) still validates.
    unapplied_actions: list[UnappliedAction] = Field(default_factory=list)
    # Dividend events this replay REFUSED to book, in replay order (review 2026-08-24).
    #
    # ``Holding.unbookable_dividend`` says a POSITION is short one payout; it cannot say WHICH
    # payout, and a consumer that needs to exclude the event itself needs the event. That
    # consumer is ``returns.xirr_reporting``: the payment 總報酬 refuses was still counted as a
    # positive cashflow there, so one dividend got three answers on three screens (excluded
    # from total return, counted by XIRR, and the trend flagging the day 待釐清).
    #
    # Excluding by POSITION would over-correct — it would drop that symbol's other, perfectly
    # bookable dividends too — which is why this carries the rows and not just a flag.
    #
    # ALWAYS EMPTY on the strict path (``allow_oversell=False``), which raises instead. Default
    # empty so every existing consumer and every hand-built ``Book`` keeps working.
    refused_dividends: list[Dividend] = Field(default_factory=list)


class CurrencyReturn(BaseModel):
    """Per-currency return breakdown."""

    realized: Decimal
    unrealized: Decimal
    total_return: Decimal
    gross_invested: Decimal
    rate: Decimal | None


class ReturnSummary(BaseModel):
    """Per-currency returns + blended reporting-currency total + XIRR."""

    by_currency: dict[Currency, CurrencyReturn]
    reporting_currency: Currency
    reporting_total_return: Decimal
    xirr: Decimal | None = None


class SectorAllocation(BaseModel):
    """Reporting-currency value and weight per sector."""

    by_sector: dict[str, Decimal]
    weights: dict[str, Decimal]
    reporting_currency: Currency


class CombinedView(BaseModel):
    """Per-currency market value + blended reporting-currency total."""

    by_currency_value: dict[Currency, Decimal]
    # The SAME values converted to the reporting currency (R5). ``by_currency_value`` above is
    # NATIVE, so its entries cannot be compared or summed across currencies — 10,000 USD and
    # 300,000 TWD are 31 : 300 natively and roughly half-and-half in reality. Anything that
    # ranks or weights currencies must read THIS field. Computed in the same loop as the
    # native leg and the blended total so the three can never disagree; additive with an empty
    # default so CombinedView constructions predating it still validate.
    by_currency_reporting: dict[Currency, Decimal] = Field(default_factory=dict)
    reporting_total_value: Decimal
    reporting_currency: Currency
