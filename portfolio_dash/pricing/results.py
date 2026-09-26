from datetime import date, datetime

from pydantic import BaseModel, Field

from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.types import Money


class PriceRow(BaseModel):
    instrument: str
    market: Market
    as_of: date
    close: Money
    open: Money | None = None
    high: Money | None = None
    low: Money | None = None
    volume: Money | None = None
    source: str


class FxRow(BaseModel):
    base: Currency
    quote: Currency
    as_of: date
    rate: Money
    source: str


class DividendEvent(BaseModel):
    instrument: str
    market: Market
    ex_date: date
    pay_date: date | None = None
    cash_amount: Money | None = None
    stock_amount: Money | None = None
    currency: Currency | None = None
    source: str


class PriceRead(BaseModel):
    value: Money
    as_of: date
    source: str
    stale: bool
    # Trading volume for the session, when stored (integer-valued Decimal; NOT money).
    # Additive/optional so existing consumers (spark_30d, dashboard price reads) are
    # unaffected; populated by ``get_price_history`` and fed to the technical volume signal.
    volume: Money | None = None
    # When this price row was fetched (provenance timestamp; NOT a market date). Additive/
    # optional so existing consumers are unaffected; populated by ``get_price_history`` and
    # surfaced by the digest movers tooltip (更新 <fetched_at>).
    fetched_at: datetime | None = None


class FxRead(BaseModel):
    rate: Money
    as_of: date
    source: str
    stale: bool


class RefreshSummary(BaseModel):
    ok: dict[str, str] = Field(default_factory=dict)  # key -> winning source
    failed: list[str] = Field(default_factory=list)  # keys with no data
    #: key -> WHY it failed, in zh (DEF-015, 2026-09-23). Additive and optional: only the
    #: dividend path records reasons today, and a summary without them still renders — the
    #: formatter then says the source gave none rather than inventing one.
    failed_reasons: dict[str, str] = Field(default_factory=dict)
    #: keys a source ANSWERED for with no data at all (DEF-047, owner ruling 2026-09-24).
    #: Additive, dividend path only: a symbol that never paid a dividend is the common case,
    #: not a failure, so it is kept OUT of ``failed`` (which drives every warn face) and
    #: reported on its own — 「1 檔無配息紀錄」. A quote/FX refresh never fills it (a listed
    #: symbol with no price IS a failed fetch); a HISTORY refresh fills it with the symbols a
    #: provider answered for with no bars in the window (DEF-067 ④, owner ruling 2026-09-26)
    #: — trusted by ``history_daily`` only with evidence from the same run, folded back into
    #: ``failed`` by the multi-year backfills.
    empty: list[str] = Field(default_factory=list)
    fetched_at: datetime
