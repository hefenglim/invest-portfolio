"""Account + fee-rule + LLM-model config seed (config-as-code defaults).

Fee-engine **v2** (2026-07-15): the FeeRuleSet carries the full per-broker schedule
from ``docs/reference/broker-fee-schedules-2026-07.md`` (owner-provided). Rates that
adjust over time (SEC / TAF / commission / stamp) live here as ``Decimal`` config, never
hard-coded in ``fees.py`` (developer note §1 of the reference doc). The TW rebate
(``rebate_rate``) is FORECAST-ONLY and is NEVER read by ``compute_fees`` (FE-D1).
"""

import sqlite3
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from portfolio_dash.shared.enums import Currency, Market


class FeeRuleSet(BaseModel):
    """Fee and tax parameters for a single account rule set (fee-engine v2).

    All monetary parameters are ``Decimal``. A field left at its default is simply not
    charged, so each rule set enables only the components its broker levies (config over
    hard-coding). ``rounding`` picks the quantization mode: ``"floor"`` (TW, ROUND_DOWN to
    integer NT$ per 財政部 FE-D3) or ``"half_up"`` (US/MY, ROUND_HALF_UP to the 2-dp minor
    unit, applied per component).
    """

    market: Market
    rounding: Literal["floor", "half_up"] = "half_up"

    # --- TW commission + securities-transaction-tax ---
    brokerage: Decimal = Decimal("0")  # commission rate of notional (TW)
    discount: Decimal = Decimal("1")  # charge-first: 1 = full price; rebate applied off-ledger
    min_fee: Decimal = Decimal("0")  # TW min NT$20 (floor applies before this compare)
    tax_normal: Decimal = Decimal("0")  # sell-side 現股
    tax_etf: Decimal = Decimal("0")  # sell-side ETF
    tax_daytrade: Decimal = Decimal("0")  # sell-side 當沖
    rebate_rate: Decimal = Decimal("0")  # TW monthly 折讓款 (FORECAST-ONLY; never in compute_fees)

    # --- US regulatory components (Schwab + Moomoo US share these formulas) ---
    commission_rate: Decimal = Decimal("0")  # US commission rate of notional
    commission_min: Decimal = Decimal("0")  # US commission floor (e.g. $0.01)
    platform_fee: Decimal = Decimal("0")  # per-order fixed platform fee (Moomoo US $0.99)
    settlement_per_share: Decimal = Decimal("0")  # $0.003 / share
    settlement_cap_rate: Decimal = Decimal("0")  # cap = rate × notional (1% => 0.01)
    cat_per_share: Decimal = Decimal("0")  # Consolidated Audit Trail, both sides
    sec_rate: Decimal = Decimal("0")  # SEC reg fee rate, SELL-only
    sec_min: Decimal = Decimal("0")  # SEC min ($0.01)
    taf_per_share: Decimal = Decimal("0")  # FINRA TAF per share, SELL-only
    taf_min: Decimal = Decimal("0")  # TAF min ($0.01)
    taf_cap: Decimal | None = None  # TAF cap ($9.79)
    broker_assisted_surcharge: Decimal = Decimal("0")  # Schwab $25 (config; not applied)

    # --- MY commission / clearing / SST (Moomoo MY market) ---
    clearing_rate: Decimal = Decimal("0")  # MY clearing fee rate of notional
    clearing_cap: Decimal | None = None  # MY clearing cap (RM1,000)
    sst_rate: Decimal = Decimal("0")  # SST on (commission + platform + clearing)

    # --- Stamp duty: MY market native (MYR); US->MY cross-currency per FE-D2 ---
    stamp_unit: Decimal = Decimal("0")  # RM step granularity (per this notional); 0 => no stamp
    stamp_per_unit: Decimal = Decimal("0")  # RM charged per (ceil) unit (RM1)
    stamp_cap_stock: Decimal | None = None  # stamp cap for ordinary stock
    stamp_cap_etf: Decimal | None = None  # ETF cap: MY = 0 (exempt); US = RM200

    @property
    def has_us_stamp(self) -> bool:
        """True when this US rule levies the MY cross-currency stamp (Moomoo US, FE-D2).

        The caller seam resolves the trade-date USD/MYR rate only when this is True, so
        Schwab (no stamp configured) never triggers an FX lookup.
        """
        return self.market is Market.US and self.stamp_unit > 0


class AccountConfig(BaseModel):
    """Static configuration for a broker account."""

    account_id: str
    name: str
    broker: str
    settlement_ccy: Currency
    funding_ccy: Currency
    fee_rule_set: str
    dividend_model: str


# Annually-adjusted US regulatory rates (reference doc §肆.1 — configurable, not hard-coded).
_US_SEC_RATE = Decimal("0.0000206")
_US_TAF_PER_SHARE = Decimal("0.000195")
_US_TAF_MIN = Decimal("0.01")
_US_TAF_CAP = Decimal("9.79")
_US_REG_MIN = Decimal("0.01")

FEE_RULES: dict[str, FeeRuleSet] = {
    # TW — 群益 charge-first (先收後退) 2.3折: full 0.1425% at settlement, 77% rebate next
    # month (rebate_rate is FORECAST-ONLY, never charged here). Floor to integer NT$ (FE-D3).
    "tw": FeeRuleSet(
        market=Market.TW,
        rounding="floor",
        brokerage=Decimal("0.001425"),
        discount=Decimal("1"),
        min_fee=Decimal("20"),
        tax_normal=Decimal("0.003"),
        tax_etf=Decimal("0.001"),
        tax_daytrade=Decimal("0.0015"),
        rebate_rate=Decimal("0.77"),
    ),
    # Schwab US — $0 online commission; SELL-only SEC + TAF. Broker-assisted $25 is config
    # only (default off — the app has no channel flag, so it is never applied).
    "schwab": FeeRuleSet(
        market=Market.US,
        rounding="half_up",
        sec_rate=_US_SEC_RATE,
        sec_min=_US_REG_MIN,
        taf_per_share=_US_TAF_PER_SHARE,
        taf_min=_US_TAF_MIN,
        taf_cap=_US_TAF_CAP,
        broker_assisted_surcharge=Decimal("25.00"),
    ),
    # Moomoo MY (US) — commission + platform + settlement + CAT (+ SELL: SEC + TAF), plus
    # the MY stamp on US trades (FE-D2): computed in MYR, booked in USD.
    "moomoo_us": FeeRuleSet(
        market=Market.US,
        rounding="half_up",
        commission_rate=Decimal("0.0003"),
        commission_min=Decimal("0.01"),
        platform_fee=Decimal("0.99"),
        settlement_per_share=Decimal("0.003"),
        settlement_cap_rate=Decimal("0.01"),
        cat_per_share=Decimal("0.000003"),
        sec_rate=_US_SEC_RATE,
        sec_min=_US_REG_MIN,
        taf_per_share=_US_TAF_PER_SHARE,
        taf_min=_US_TAF_MIN,
        taf_cap=_US_TAF_CAP,
        stamp_unit=Decimal("1000"),
        stamp_per_unit=Decimal("1"),
        stamp_cap_stock=Decimal("1000"),
        stamp_cap_etf=Decimal("200"),
    ),
    # Moomoo MY (MY) — commission + platform + clearing + SST (both sides); stamp step
    # function ceil(amount/1000)×RM1 capped RM1,000, ETF exempt (cap 0).
    "moomoo_my": FeeRuleSet(
        market=Market.MY,
        rounding="half_up",
        commission_rate=Decimal("0.0003"),
        commission_min=Decimal("0.01"),
        platform_fee=Decimal("3.00"),
        clearing_rate=Decimal("0.0003"),
        clearing_cap=Decimal("1000"),
        sst_rate=Decimal("0.08"),
        stamp_unit=Decimal("1000"),
        stamp_per_unit=Decimal("1"),
        stamp_cap_stock=Decimal("1000"),
        stamp_cap_etf=Decimal("0"),
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

def get_fee_rule_set(
    name: str, conn: sqlite3.Connection | None = None
) -> FeeRuleSet:
    """Return the EFFECTIVE FeeRuleSet for *name*; raises KeyError if not found.

    ``conn=None`` -> the pure fee-engine v2 defaults (deterministic; keeps the oracle and the
    unit tests hermetic). With a ``conn`` -> the v2 defaults merged with the user's DB overlay
    (FU-D1, :mod:`data_ingestion.fee_overrides`). EVERY money call site must pass its ``conn``
    so user rate edits actually take effect (the "engine supports it but the entry never passes
    it" bug class, LESSONS_LEARNED.md).
    """
    base = FEE_RULES[name]
    if conn is None:
        return base
    # Local import: fee_overrides imports FeeRuleSet from this module (avoid a cycle).
    from portfolio_dash.data_ingestion.fee_overrides import apply_overlay

    return apply_overlay(conn, name, base)


def get_effective_fee_rules(conn: sqlite3.Connection) -> dict[str, FeeRuleSet]:
    """Every rule set with the user's overlay applied (bulk: accounts wire, export dump)."""
    from portfolio_dash.data_ingestion.fee_overrides import apply_overlay

    return {name: apply_overlay(conn, name, rs) for name, rs in FEE_RULES.items()}


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
