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
_ZERO = Decimal("0")


def check_amounts(gross: Decimal, withholding: Decimal, net: Decimal) -> str | None:
    """Reject IMPOSSIBLE dividend amount combinations. Returns a zh message, or None.

    The ONE shared gate for every write path (CSV/manual import preview AND the ledger edit
    endpoint) — audit M5, 2026-07-26. It used to exist on neither: the edit endpoint checked
    only "not negative" and stored the three numbers verbatim, so a row could claim a 100
    gross, a 30 withholding and a 50 net, with the missing 20 silently gone (only ``net``
    reaches the ledger).

    What it does NOT do is demand ``gross - withholding == net``. That identity belongs to the
    US DRIP model alone, not to dividends in general: a TW cash dividend legitimately nets
    less than gross through statutory levies and remittance charges this app DOES NOT MODEL,
    and a US payout can carry an ADR fee. Rejecting those would push the user to falsify the
    gross. The real invariant is conservation: a payout can never DELIVER more than it
    declared. (That wrong identity was genuinely proposed once — see ``LESSONS_LEARNED.md`` —
    which is why the reason this gate is lenient is written here rather than left to be
    re-derived by the next reader.)

    To be unambiguous, because the wording above has been misread once: **no levy other than
    the US 30% DRIP withholding is computed anywhere in this codebase, and none is planned.**
    There is no rate, threshold or field for any TW dividend levy in ``FEE_RULES`` or in the
    dividend model, and per the owner's ruling of 2026-08-27 (spec ``AI-D55``) that is a
    PERMANENT exclusion, not a deferral — fees and taxes stop at 手續費 + 證交稅. For a TW cash
    dividend the user types the net actually received and that number is what reaches the
    ledger; the gap is simply unexplained, not itemised.
    """
    if gross < _ZERO or withholding < _ZERO or net < _ZERO:
        return "股利金額不可為負"
    if withholding + net > gross:
        return (
            f"股利金額不自洽：預扣 {withholding} + 淨額 {net} 大於總額 {gross}"
            "（淨額與預扣的合計不可超過股利總額）"
        )
    return None


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
