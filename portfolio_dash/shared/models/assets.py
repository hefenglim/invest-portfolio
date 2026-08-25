"""Account and Instrument models."""

from datetime import date
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
    # D44: the date the band above was last CHANGED. READ-ONLY on this model — like
    # ``archived``, the column is owned by one writer (``store.upsert_instrument``, which
    # derives it by comparing against the stored row), so setting it here has no effect and
    # no caller has to remember to. None = unknown, which the D44 finding reads as "make no
    # claim" rather than as "old".
    target_set_at: date | None = None
    is_etf: bool = False  # single source of truth for ETF (never derive from sector)
    # AI-D40 (2026-08-24): ``is_etf`` alone could not say "nobody has answered this yet",
    # so auto-registration silently meant "not an ETF" and a TW ETF was taxed at 現股 0.3%
    # instead of 0.1% on its first manual sell. When this is True the flag is UNSET, not
    # False: the fee engine still computes with False (a number must come out) but raises
    # ``etf_flag_unknown`` on a TW SELL so the rate is disclosed rather than assumed.
    etf_flag_unknown: bool = False
    archived: bool = False  # FU-D13: stop-tracking flag; stays registered, off fetch scopes
    industry: str | None = None  # GICS industry (R6): nullable free text, filled by the
    # next wave's AI service; backend plumbing only this wave (no frontend form yet).
