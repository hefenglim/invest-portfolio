"""Per-account fee and tax computation with rule snapshot."""

from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel

from portfolio_dash.data_ingestion.config_seed import FeeRuleSet
from portfolio_dash.shared.enums import Market
from portfolio_dash.shared.models.enums import Side


class FeeResult(BaseModel):
    """Computed fee + tax for a single transaction, with the rate snapshot used."""

    fee: Decimal
    tax: Decimal
    snapshot: dict[str, str]


def _round(value: Decimal, *, integer: bool) -> Decimal:
    """Quantize to integer (TW NT$) or 2 dp (USD/MYR), using ROUND_HALF_UP."""
    return value.quantize(
        Decimal("1") if integer else Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )


def compute_fees(
    rules: FeeRuleSet,
    side: Side,
    quantity: Decimal,
    price: Decimal,
    *,
    is_etf: bool = False,
    daytrade: bool = False,
) -> FeeResult:
    """Compute the fee and tax for a transaction given the account's FeeRuleSet.

    Args:
        rules:     The account's fee rule set (from config_seed).
        side:      BUY or SELL.
        quantity:  Number of shares/units traded.
        price:     Per-unit price in the instrument's quote currency.
        is_etf:    True if the instrument is an ETF (affects TW sell-side tax rate).
        daytrade:  True if this is a same-day round-trip trade (affects TW sell-side tax).

    Returns:
        FeeResult with fee, tax (both Decimal), and snapshot dict recording rates used.
    """
    notional = quantity * price
    snap: dict[str, str] = {}

    if rules.market is Market.TW:
        raw_fee = rules.brokerage * rules.discount * notional
        fee = max(raw_fee, rules.min_fee) if notional > 0 else Decimal("0")
        snap["brokerage"] = str(rules.brokerage)
        snap["discount"] = str(rules.discount)
        snap["min_fee"] = str(rules.min_fee)

        tax = Decimal("0")
        if side is Side.SELL:
            rate = (
                rules.tax_daytrade
                if daytrade
                else rules.tax_etf
                if is_etf
                else rules.tax_normal
            )
            tax = rate * notional
            snap["tax_rate"] = str(rate)

        return FeeResult(
            fee=_round(fee, integer=rules.round_integer),
            tax=_round(tax, integer=rules.round_integer),
            snapshot=snap,
        )

    if rules.market is Market.US:
        fee = rules.flat_fee + rules.brokerage * notional
        snap["flat_fee"] = str(rules.flat_fee)
        snap["brokerage"] = str(rules.brokerage)
        if side is Side.SELL:
            fee = fee + rules.sec_fee * notional
            snap["sec_fee"] = str(rules.sec_fee)
        if notional > 0 and rules.min_fee > 0:
            fee = max(fee, rules.min_fee)
            snap["min_fee"] = str(rules.min_fee)
        return FeeResult(fee=_round(fee, integer=False), tax=Decimal("0.00"), snapshot=snap)

    # Market.MY
    brokerage = rules.brokerage * notional
    if notional > 0 and rules.min_fee > 0:
        brokerage = max(brokerage, rules.min_fee)
    clearing = rules.clearing * notional
    if rules.clearing_cap is not None and clearing > rules.clearing_cap:
        clearing = rules.clearing_cap
    fee = brokerage + clearing + rules.sst
    tax = rules.stamp_duty_rate * notional
    if rules.stamp_duty_cap is not None and tax > rules.stamp_duty_cap:
        tax = rules.stamp_duty_cap
    snap["brokerage"] = str(rules.brokerage)
    snap["min_fee"] = str(rules.min_fee)
    snap["clearing"] = str(rules.clearing)
    snap["stamp_duty_rate"] = str(rules.stamp_duty_rate)
    snap["sst"] = str(rules.sst)
    if rules.clearing_cap is not None:
        snap["clearing_cap"] = str(rules.clearing_cap)
    if rules.stamp_duty_cap is not None:
        snap["stamp_duty_cap"] = str(rules.stamp_duty_cap)
    return FeeResult(
        fee=_round(fee, integer=False),
        tax=_round(tax, integer=False),
        snapshot=snap,
    )
