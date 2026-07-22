"""Account and Instrument models."""

from decimal import Decimal

from pydantic import BaseModel

from portfolio_dash.shared.enums import Currency, Market


class MarketRule(BaseModel):
    """Fee + dividend rule set bound to one (account, market) pair.

    Carried on :class:`Account` so the PURE compute layer can read per-market rules
    WITHOUT a DB connection (architecture rule: ``portfolio/`` is pure). Additive; the
    account-level scalar fields remain the fallback when a market has no binding.
    """

    fee_rule_set: str
    dividend_model: str


class Account(BaseModel):
    """A broker account (first-class entity; fee/dividend rules bind here)."""

    account_id: str
    name: str
    broker: str
    settlement_ccy: Currency
    funding_ccy: Currency
    dividend_model: str  # DB truth; per-account dividend rule (e.g. drip_us, cash)
    # (account, market) rule bindings, keyed by market VALUE ("US"/"TW"/"MY"). Populated
    # from account_market_rules by store.list_accounts; empty {} for readers that don't
    # carry it — the scalar fields above stay the fallback. NOTHING consumes this yet.
    market_rules: dict[str, MarketRule] = {}


class Instrument(BaseModel):
    """A tradable instrument; knows its market and quote currency."""

    symbol: str
    market: Market
    quote_ccy: Currency
    sector: str
    name: str
    board: str = ""  # "TWSE" | "TPEx" | ".KL" | "" (US / unresolved)
    target_low: Decimal | None = None  # price-alert floor (spec 10)
    target_high: Decimal | None = None  # price-alert ceiling (FU-D28)
    is_etf: bool = False  # single source of truth for ETF (never derive from sector)
    archived: bool = False  # FU-D13: stop-tracking flag; stays registered, off fetch scopes
    industry: str | None = None  # GICS industry (R6): nullable free text, filled by the
    # next wave's AI service; backend plumbing only this wave (no frontend form yet).
