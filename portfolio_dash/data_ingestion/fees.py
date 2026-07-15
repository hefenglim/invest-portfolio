"""Per-account fee and tax computation with rule snapshot (fee-engine **v2**, 2026-07-15).

Translates the owner's broker schedules (``docs/reference/broker-fee-schedules-2026-07.md``)
into a single pure function ``compute_fees``. Money is ``Decimal`` end to end; rates come
from the account's :class:`FeeRuleSet` (config, never hard-coded).

Rounding (per rule set):
  * TW (``rounding="floor"``): ROUND_DOWN to integer NT$ for fee AND tax; the min-NT$20
    floor is applied AFTER the floor (財政部 FE-D3; 群益 example 142.5 → 142).
  * US / MY (``rounding="half_up"``): each fee COMPONENT quantized to the 2-dp minor unit
    ROUND_HALF_UP, then summed (documented assumption pending statement verification).

FE-D2 (MY stamp on US trades): the stamp is computed in MYR from the USD notional and the
trade-date USD/MYR rate, then converted back to USD for booking. ``compute_fees`` stays
PURE — the caller seam resolves the rate and passes it as ``stamp_fx``; when it is ``None``
the stamp is 0 and the seam surfaces the soft issue 「無 USD/MYR 匯率,印花稅未計」.
"""

from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation

from pydantic import BaseModel

from portfolio_dash.data_ingestion.config_seed import FeeRuleSet
from portfolio_dash.shared.enums import Market
from portfolio_dash.shared.models.enums import Side

_ZERO = Decimal("0")
_CENT = Decimal("0.01")
_INT = Decimal("1")


class FeeComputationError(ValueError):
    """Fee/tax could not be computed (e.g. an overflow-sized notional).

    Raised at the quantize seam so a pathological input surfaces as a validation
    issue at the callers (manual entry, CSV import) rather than a 500 (audit M4).
    """


class FeeResult(BaseModel):
    """Computed fee + tax for a single transaction, with the rate snapshot used."""

    fee: Decimal
    tax: Decimal
    snapshot: dict[str, str]


def _cent(value: Decimal) -> Decimal:
    """Quantize a US/MY component to the 2-dp minor unit, ROUND_HALF_UP.

    Overflow-sized values exceed the Decimal context precision and raise
    ``InvalidOperation``; re-raise as :class:`FeeComputationError` (audit M4).
    """
    try:
        return value.quantize(_CENT, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise FeeComputationError("數值過大,無法計算費用/稅") from exc


def _floor_int(value: Decimal) -> Decimal:
    """Quantize a TW fee/tax to integer NT$, ROUND_DOWN (財政部 FE-D3, 角以下免收)."""
    try:
        return value.quantize(_INT, rounding=ROUND_DOWN)
    except InvalidOperation as exc:
        raise FeeComputationError("數值過大,無法計算費用/稅") from exc


def _tw(
    rules: FeeRuleSet, side: Side, notional: Decimal, is_etf: bool, daytrade: bool,
    snap: dict[str, str],
) -> FeeResult:
    """TW: floor(notional×brokerage×discount) then max(·, min_fee); sell tax floored."""
    snap["engine"] = "v2"
    snap["rounding"] = rules.rounding
    snap["brokerage"] = str(rules.brokerage)
    snap["discount"] = str(rules.discount)
    snap["min_fee"] = str(rules.min_fee)
    snap["rebate_rate"] = str(rules.rebate_rate)  # forecast-only; recorded, never charged

    if notional <= _ZERO:
        return FeeResult(fee=_ZERO, tax=_ZERO, snapshot=snap)

    floored = _floor_int(rules.brokerage * rules.discount * notional)
    fee = max(floored, rules.min_fee)  # min applies AFTER the floor (群益 142.5→142, 5→20)

    tax = _ZERO
    if side is Side.SELL:
        rate = (
            rules.tax_daytrade if daytrade else rules.tax_etf if is_etf else rules.tax_normal
        )
        tax = _floor_int(rate * notional)
        snap["tax_rate"] = str(rate)
    return FeeResult(fee=fee, tax=tax, snapshot=snap)


def _us_stamp(
    rules: FeeRuleSet, notional: Decimal, is_etf: bool, stamp_fx: Decimal | None,
    snap: dict[str, str],
) -> Decimal:
    """MY stamp on a US (USD) trade, booked in USD (FE-D2). 0 when no FX rate given."""
    if not rules.has_us_stamp:
        return _ZERO
    if stamp_fx is None or stamp_fx <= _ZERO:
        snap["stamp_fx_missing"] = "1"
        return _ZERO
    amount_myr = notional * stamp_fx
    lots = (amount_myr / rules.stamp_unit).quantize(_INT, rounding=ROUND_CEILING)
    stamp_myr = lots * rules.stamp_per_unit
    cap = rules.stamp_cap_etf if is_etf else rules.stamp_cap_stock
    if cap is not None and stamp_myr > cap:
        stamp_myr = cap
    stamp_usd = _cent(stamp_myr / stamp_fx)
    snap["stamp_fx_rate"] = str(stamp_fx)
    snap["stamp_myr"] = str(stamp_myr)
    snap["stamp_usd"] = str(stamp_usd)
    return stamp_usd


def _us(
    rules: FeeRuleSet, side: Side, quantity: Decimal, notional: Decimal, is_etf: bool,
    stamp_fx: Decimal | None, snap: dict[str, str],
) -> FeeResult:
    """US (Schwab + Moomoo): sum per-component (each cent-quantized) + MY stamp as tax."""
    snap["engine"] = "v2"
    snap["rounding"] = rules.rounding
    if notional <= _ZERO:
        return FeeResult(fee=_ZERO, tax=_ZERO, snapshot=snap)

    fee = _ZERO
    # commission = max(rate×notional, min) — only meaningful when configured
    if rules.commission_rate > _ZERO or rules.commission_min > _ZERO:
        commission = _cent(max(rules.commission_rate * notional, rules.commission_min))
        fee += commission
        snap["commission"] = str(commission)
        snap["commission_rate"] = str(rules.commission_rate)
    if rules.platform_fee > _ZERO:
        platform = _cent(rules.platform_fee)
        fee += platform
        snap["platform"] = str(platform)
    if rules.settlement_per_share > _ZERO:
        cap = rules.settlement_cap_rate * notional
        settlement = _cent(min(rules.settlement_per_share * quantity, cap))
        fee += settlement
        snap["settlement"] = str(settlement)
        snap["settlement_per_share"] = str(rules.settlement_per_share)
    if rules.cat_per_share > _ZERO:
        cat = _cent(rules.cat_per_share * quantity)
        fee += cat
        snap["cat"] = str(cat)
    if side is Side.SELL:
        if rules.sec_rate > _ZERO:
            sec = _cent(max(rules.sec_rate * notional, rules.sec_min))
            fee += sec
            snap["sec"] = str(sec)
            snap["sec_rate"] = str(rules.sec_rate)
        if rules.taf_per_share > _ZERO:
            taf_raw = max(rules.taf_per_share * quantity, rules.taf_min)
            if rules.taf_cap is not None and taf_raw > rules.taf_cap:
                taf_raw = rules.taf_cap
            taf = _cent(taf_raw)
            fee += taf
            snap["taf"] = str(taf)
            snap["taf_per_share"] = str(rules.taf_per_share)

    tax = _us_stamp(rules, notional, is_etf, stamp_fx, snap)
    return FeeResult(fee=fee, tax=tax, snapshot=snap)


def _my(
    rules: FeeRuleSet, notional: Decimal, is_etf: bool, snap: dict[str, str],
) -> FeeResult:
    """MY (Moomoo MY market): commission + platform + clearing + SST; stamp step as tax."""
    snap["engine"] = "v2"
    snap["rounding"] = rules.rounding
    if notional <= _ZERO:
        return FeeResult(fee=_ZERO, tax=_ZERO, snapshot=snap)

    commission = _cent(max(rules.commission_rate * notional, rules.commission_min))
    platform = _cent(rules.platform_fee)
    clearing_raw = rules.clearing_rate * notional
    if rules.clearing_cap is not None and clearing_raw > rules.clearing_cap:
        clearing_raw = rules.clearing_cap
    clearing = _cent(clearing_raw)
    # SST on the (quantized) commission + platform + clearing (documented assumption).
    sst = _cent(rules.sst_rate * (commission + platform + clearing))
    fee = commission + platform + clearing + sst
    snap["commission"] = str(commission)
    snap["platform"] = str(platform)
    snap["clearing"] = str(clearing)
    snap["sst"] = str(sst)
    snap["commission_rate"] = str(rules.commission_rate)
    snap["clearing_rate"] = str(rules.clearing_rate)
    snap["sst_rate"] = str(rules.sst_rate)

    # Stamp (tax): ceil(notional / unit) × per_unit, capped; ETF cap 0 => exempt.
    tax = _ZERO
    if rules.stamp_unit > _ZERO:
        lots = (notional / rules.stamp_unit).quantize(_INT, rounding=ROUND_CEILING)
        stamp = lots * rules.stamp_per_unit
        cap = rules.stamp_cap_etf if is_etf else rules.stamp_cap_stock
        if cap is not None and stamp > cap:
            stamp = cap
        tax = _cent(stamp)
        snap["stamp"] = str(tax)
    return FeeResult(fee=fee, tax=tax, snapshot=snap)


def compute_fees(
    rules: FeeRuleSet,
    side: Side,
    quantity: Decimal,
    price: Decimal,
    *,
    is_etf: bool = False,
    daytrade: bool = False,
    stamp_fx: Decimal | None = None,
) -> FeeResult:
    """Compute the fee and tax for a transaction given the account's FeeRuleSet (v2).

    Args:
        rules:     The account's fee rule set (from config_seed).
        side:      BUY or SELL.
        quantity:  Number of shares/units traded.
        price:     Per-unit price in the instrument's quote currency.
        is_etf:    True if the instrument is an ETF (TW sell tax rate; MY/US stamp cap).
        daytrade:  True if this is a same-day round-trip trade (TW sell tax rate).
        stamp_fx:  Trade-date USD/MYR rate for the Moomoo US MY stamp (FE-D2). Resolved and
                   passed by the caller seam; ``None`` -> stamp 0 (seam surfaces a soft issue).

    Returns:
        FeeResult with fee, tax (both Decimal) and the rate/component snapshot (``engine="v2"``).
    """
    notional = quantity * price
    snap: dict[str, str] = {}

    if rules.market is Market.TW:
        return _tw(rules, side, notional, is_etf, daytrade, snap)

    if rules.market is Market.US:
        return _us(rules, side, quantity, notional, is_etf, stamp_fx, snap)

    return _my(rules, notional, is_etf, snap)


def forecast_tw_rebate(fee: Decimal, rebate_rate: Decimal) -> Decimal:
    """FORECAST-ONLY: the expected TW monthly rebate for one trade = floor(fee × rebate_rate).

    NEVER money of record and NEVER used by ``compute_fees`` (FE-D1): the 群益 2.3折 model
    charges the full 0.1425% at settlement and refunds 77% next month, confirmed off-ledger.
    Wave B surfaces this as a preview hint and a pending-confirmation inbox item. Floored per
    the 券商 convention (遇小數點無條件捨去): floor(142×0.77)=109, floor(156×0.77)=120.
    """
    return (fee * rebate_rate).quantize(_INT, rounding=ROUND_DOWN)
