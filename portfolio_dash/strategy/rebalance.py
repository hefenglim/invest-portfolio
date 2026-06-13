"""Rebalance preview (spec 03 §3.3): a compute-only target-weight trade planner.

Given target reporting-currency weights per symbol, compute the integer-share trades
(MY-market trades snap to 100-unit board lots) needed to reach them, with fees/tax via
the REAL fee engine (``data_ingestion.fees.compute_fees``) and a portfolio summary. It
NEVER writes to any ledger table — a pure projection of "what trades would reach these
weights".

Conventions / honest degradation:
- Uses the SAME current spot rates as the dashboard (``RateResolver``) and the same
  valuation (``build_dashboard``). A target symbol with NO current price (unknown,
  unheld-and-unpriced, or in ``freshness.missing_prices``) is EXCLUDED — never faked.
- v1 acts ONLY on symbols present in ``targets``: held symbols absent from ``targets``
  are left untouched and do not appear in the output. (A future version may treat the
  full portfolio; documented here so the partial-weight behaviour is intentional.)
- A held symbol binds fees to its Q1 account (the account holding the MOST shares).
- ``new_weight`` is the resulting position's reporting MV divided by the ORIGINAL total
  reporting MV (weights are relative to today's book, not a recomputed post-trade total);
  this keeps each row independent and is the honest, simplest choice for a preview.
- Money is ``Decimal`` end to end; the router serializes to wire strings.
"""

import sqlite3
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from portfolio_dash.data_ingestion.config_seed import get_fee_rule_set
from portfolio_dash.data_ingestion.store import list_instruments
from portfolio_dash.portfolio.dashboard import RateResolver, build_dashboard
from portfolio_dash.portfolio.dashboard_models import HoldingRow
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.fx import convert
from portfolio_dash.shared.models.enums import Side

_ZERO = Decimal("0")
_HUNDRED = Decimal("100")


def _round_shares(raw: Decimal, market: Market) -> Decimal:
    """Round raw shares to an integer; MY market snaps to the nearest 100-unit lot."""
    if market is Market.MY:
        return (raw / _HUNDRED).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * _HUNDRED
    return raw.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def _most_shares_holding(holdings: list[HoldingRow], symbol: str) -> HoldingRow | None:
    """The held row with the MOST shares of *symbol* (Q1 account binding), or None."""
    candidates = [h for h in holdings if h.symbol == symbol and h.shares > _ZERO]
    if not candidates:
        return None
    return max(candidates, key=lambda h: h.shares)


def _fee_rule_set_name(conn: sqlite3.Connection, account_id: str) -> str | None:
    """The account's fee-rule-set name (the Account model omits it; query directly)."""
    row = conn.execute(
        "SELECT fee_rule_set FROM accounts WHERE account_id=?", (account_id,)
    ).fetchone()
    return row["fee_rule_set"] if row is not None else None


def compute_rebalance(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    reporting: Currency,
    targets: dict[str, Decimal],
) -> dict[str, object]:
    """Compute the trades to reach *targets* (reporting-ccy weight ratios per symbol).

    Returns a dict ``{"rows": [...], "summary": {...}}`` with all money values as
    ``Decimal`` (the router converts to wire strings). Compute-only — no writes.
    """
    from portfolio_dash.data_ingestion.fees import compute_fees  # local: avoid cycle risk

    data = build_dashboard(conn, now=now, reporting=reporting)
    resolver = RateResolver(conn, now=now)
    total = data.kpis.total_market_value

    instruments = {i.symbol: i for i in list_instruments(conn)}
    missing = set(data.freshness.missing_prices)

    rows: list[dict[str, Decimal | str]] = []
    excluded: list[str] = []

    # Degrade honestly: with no priced total there is nothing to rebalance against.
    if total is None or total == _ZERO:
        excluded = [sym for sym in targets]
        return {
            "rows": rows,
            "summary": {
                "turnover_reporting": _ZERO,
                "total_fees_reporting": _ZERO,
                "cash_after": _ZERO,
                "excluded": excluded,
                "note": "total market value unavailable; nothing to rebalance",
            },
        }

    turnover_reporting = _ZERO
    total_fees_reporting = _ZERO
    cash_after = _ZERO

    for symbol, target_ratio in targets.items():
        holding = _most_shares_holding(data.holdings, symbol)
        # Exclude any symbol without a usable current price (never fabricate one).
        if (holding is None or holding.market_price is None
                or holding.market_value is None or symbol in missing):
            excluded.append(symbol)
            continue

        quote_ccy = holding.quote_ccy
        market = holding.market
        price = holding.market_price
        try:
            rate = resolver.rate(quote_ccy, reporting)
        except KeyError:
            excluded.append(symbol)
            continue

        target_weight = target_ratio
        current_mv_reporting = convert(holding.market_value, rate)
        target_mv_reporting = target_weight * total
        delta_reporting = target_mv_reporting - current_mv_reporting
        if delta_reporting == _ZERO:
            continue  # already on target
        side = Side.BUY if delta_reporting > _ZERO else Side.SELL

        # reporting -> quote is 1/rate; share count then divides by quote-ccy price.
        delta_quote = delta_reporting / rate
        raw_shares = abs(delta_quote) / price
        shares = _round_shares(raw_shares, market)
        if shares == _ZERO:
            continue  # rounds to no trade

        amount = shares * price  # quote ccy
        rule_name = _fee_rule_set_name(conn, holding.account_id)
        if rule_name is None:
            excluded.append(symbol)
            continue
        rules = get_fee_rule_set(rule_name)
        fr = compute_fees(rules, side, shares, price, is_etf=instruments[symbol].is_etf)
        fee, tax = fr.fee, fr.tax

        new_shares = (holding.shares + shares if side is Side.BUY
                      else holding.shares - shares)
        new_position_reporting = convert(new_shares * price, rate)
        new_weight = new_position_reporting / total  # vs ORIGINAL total (documented)

        rows.append({
            "symbol": symbol,
            "current_weight": holding.weight if holding.weight is not None else _ZERO,
            "target_weight": target_weight,
            "side": side.value.lower(),
            "shares": shares,
            "amount": amount,
            "ccy": quote_ccy.value,
            "fee": fee,
            "tax": tax,
            "new_weight": new_weight,
        })

        amount_reporting = convert(amount, rate)
        turnover_reporting += amount_reporting
        total_fees_reporting += convert(fee + tax, rate)
        if side is Side.SELL:
            cash_after += convert(amount - fee - tax, rate)   # proceeds net in
        else:
            cash_after -= convert(amount + fee + tax, rate)   # total cost out

    return {
        "rows": rows,
        "summary": {
            "turnover_reporting": turnover_reporting,
            "total_fees_reporting": total_fees_reporting,
            "cash_after": cash_after,
            "excluded": excluded,
        },
    }
