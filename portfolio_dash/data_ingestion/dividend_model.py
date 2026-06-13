"""Per-account dividend model: derive withholding, net, and reinvest shares from gross."""

from decimal import Decimal

from pydantic import BaseModel


class DividendAmounts(BaseModel):
    """Computed dividend amounts after applying the account's dividend model."""

    gross: Decimal
    withholding: Decimal
    net: Decimal
    reinvest_shares: Decimal | None = None
    reinvest_price: Decimal | None = None


_US_WITHHOLDING = Decimal("0.30")


def apply_dividend_model(
    div_type: str,
    *,
    gross: Decimal,
    withholding: Decimal | None = None,
    net: Decimal | None = None,
    reinvest_shares: Decimal | None = None,
    reinvest_price: Decimal | None = None,
) -> DividendAmounts:
    """Compute withholding, net, and reinvest_shares based on the dividend type.

    Args:
        div_type:       One of ``DRIP``, ``STOCK``, ``cash`` (case-insensitive).
        gross:          Pre-withholding dividend amount.
        withholding:    Override the computed withholding (optional).
        net:            Override the computed net (optional).
        reinvest_shares: Override the computed reinvested shares (optional).
        reinvest_price: Price per share used to compute reinvest_shares when not given.

    Returns:
        :class:`DividendAmounts` with all computed fields populated.
    """
    t = div_type.upper()
    if t == "DRIP":
        wh = withholding if withholding is not None else gross * _US_WITHHOLDING
        n = net if net is not None else gross - wh
        rs = reinvest_shares
        if rs is None and reinvest_price is not None and reinvest_price > 0:
            rs = n / reinvest_price
        return DividendAmounts(
            gross=gross,
            withholding=wh,
            net=n,
            reinvest_shares=rs,
            reinvest_price=reinvest_price,
        )
    if t == "STOCK":
        return DividendAmounts(
            gross=gross,
            withholding=Decimal("0"),
            net=Decimal("0"),
            reinvest_shares=reinvest_shares,
            reinvest_price=None,
        )
    # cash (TW) or net (MY single-tier): recorded amount is net received, no withholding
    wh = withholding if withholding is not None else Decimal("0")
    n = net if net is not None else gross - wh
    return DividendAmounts(gross=gross, withholding=wh, net=n)
