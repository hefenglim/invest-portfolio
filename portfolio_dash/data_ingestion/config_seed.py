"""Account + fee-rule + LLM-model config seed (config-as-code defaults)."""

import sqlite3
from decimal import Decimal

from pydantic import BaseModel

from portfolio_dash.shared.enums import Currency, Market


class FeeRuleSet(BaseModel):
    """Fee and tax parameters for a single account rule set."""

    market: Market
    brokerage: Decimal = Decimal("0")  # rate of notional
    discount: Decimal = Decimal("1")
    min_fee: Decimal = Decimal("0")
    tax_normal: Decimal = Decimal("0")  # sell-side
    tax_etf: Decimal = Decimal("0")
    tax_daytrade: Decimal = Decimal("0")
    sec_fee: Decimal = Decimal("0")  # US sell-side regulatory fee rate
    flat_fee: Decimal = Decimal("0")  # per-trade fixed fee (e.g. Moomoo US platform fee)
    clearing: Decimal = Decimal("0")  # MY
    clearing_cap: Decimal | None = None
    stamp_duty_rate: Decimal = Decimal("0")  # MY: rate of notional (was a flat constant)
    stamp_duty_cap: Decimal | None = None
    sst: Decimal = Decimal("0")
    round_integer: bool = False  # TW rounds fee/tax to integer NT$


class AccountConfig(BaseModel):
    """Static configuration for a broker account."""

    account_id: str
    name: str
    broker: str
    settlement_ccy: Currency
    funding_ccy: Currency
    fee_rule_set: str
    dividend_model: str


FEE_RULES: dict[str, FeeRuleSet] = {
    "tw": FeeRuleSet(
        market=Market.TW,
        brokerage=Decimal("0.001425"),
        discount=Decimal("1"),
        min_fee=Decimal("20"),
        tax_normal=Decimal("0.003"),
        tax_etf=Decimal("0.001"),
        tax_daytrade=Decimal("0.0015"),
        round_integer=True,
    ),
    # Rates per spec 18.0 truth table; pending real-statement confirmation
    # (SEC fee, MY stamp-duty cap, Moomoo platform fee buy/sell).
    "schwab": FeeRuleSet(market=Market.US, sec_fee=Decimal("0.0000278")),
    "moomoo_us": FeeRuleSet(
        market=Market.US, flat_fee=Decimal("0.99"), sec_fee=Decimal("0.0000278")
    ),
    "moomoo_my": FeeRuleSet(
        market=Market.MY,
        brokerage=Decimal("0.0008"),
        min_fee=Decimal("3"),
        clearing=Decimal("0.0003"),
        clearing_cap=Decimal("1000"),
        stamp_duty_rate=Decimal("0.001"),
    ),
}

DEFAULT_ACCOUNTS: list[AccountConfig] = [
    AccountConfig(
        account_id="tw_broker",
        name="TW Broker",
        broker="TW Broker",
        settlement_ccy=Currency.TWD,
        funding_ccy=Currency.TWD,
        fee_rule_set="tw",
        dividend_model="cash_cost_reduction",
    ),
    AccountConfig(
        account_id="schwab",
        name="Charles Schwab",
        broker="Schwab",
        settlement_ccy=Currency.USD,
        funding_ccy=Currency.TWD,
        fee_rule_set="schwab",
        dividend_model="drip_us",
    ),
    AccountConfig(
        account_id="moomoo_my_us",
        name="Moomoo MY (US)",
        broker="Moomoo MY",
        settlement_ccy=Currency.USD,
        funding_ccy=Currency.MYR,
        fee_rule_set="moomoo_us",
        dividend_model="drip_us",
    ),
    AccountConfig(
        account_id="moomoo_my_my",
        name="Moomoo MY (MY)",
        broker="Moomoo MY",
        settlement_ccy=Currency.MYR,
        funding_ccy=Currency.MYR,
        fee_rule_set="moomoo_my",
        dividend_model="cash",
    ),
]

def get_fee_rule_set(name: str) -> FeeRuleSet:
    """Return the named FeeRuleSet; raises KeyError if not found."""
    return FEE_RULES[name]


def seed_accounts(conn: sqlite3.Connection) -> None:
    """Insert DEFAULT_ACCOUNTS into the accounts table; idempotent via upsert."""
    conn.executemany(
        """INSERT INTO accounts
               (account_id, name, broker, settlement_ccy, funding_ccy,
                fee_rule_set, dividend_model)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(account_id) DO UPDATE SET
               name           = excluded.name,
               broker         = excluded.broker,
               settlement_ccy = excluded.settlement_ccy,
               funding_ccy    = excluded.funding_ccy,
               fee_rule_set   = excluded.fee_rule_set,
               dividend_model = excluded.dividend_model""",
        [
            (
                a.account_id,
                a.name,
                a.broker,
                a.settlement_ccy.value,
                a.funding_ccy.value,
                a.fee_rule_set,
                a.dividend_model,
            )
            for a in DEFAULT_ACCOUNTS
        ],
    )
    conn.commit()
