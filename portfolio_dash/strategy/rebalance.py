"""Rebalance preview (spec 03 §3.3): a compute-only target-weight trade planner.

Given target reporting-currency weights per symbol, compute the integer-share trades
(MY-market trades snap to 100-unit board lots) needed to reach them, with fees/tax via
the REAL fee engine (``data_ingestion.fees.compute_fees``) and a portfolio summary. It
NEVER writes to any ledger table — a pure projection of "what trades would reach these
weights".

Combined cross-account engine (owner ruling 2026-07-13, sign-off): a symbol's target
weight applies to its COMBINED position across ALL accounts. Symbol-level targets over the
combined cross-account position; buys route to the most-shares account, sells allocate
greedily most-shares-first (Option 2 per-account targets rejected). Concretely, for each
targeted symbol the engine:

1. aggregates the symbol's shares + reporting-currency MV over every priced account
   holding, and sizes ``delta = target_weight × portfolio_total − combined_MV``;
2. routes the executed trade to concrete accounts (fees/taxes bind to the ACCOUNT — core
   invariant #5):
   - a **BUY** is one leg routed to the account holding the MOST shares of that symbol
     (deterministic tie-break: account_id order);
   - a **SELL** allocates greedily, most-shares account first, each leg bounded by that
     account's shares, until the delta is covered — so a target of 0 liquidates EVERY
     account's shares, and an oversized sell can never exceed the shares actually held;
3. snaps each leg by the LEG's market rules (MY → 100-unit board lot; others → integer)
   and prices fees/taxes per leg via THAT account's fee rule set;
4. reports the flat aggregate per row (side / total shares / total amount / total fee+tax /
   combined ``current_weight`` / combined ``new_weight``) PLUS the ``accounts`` constituents
   and the executing ``legs``.

Conventions / honest degradation:
- Uses the SAME current spot rates as the dashboard (``RateResolver``) and the same
  valuation (``build_dashboard``). A target symbol with NO current price (unknown,
  unheld-and-unpriced, or in ``freshness.missing_prices``) is EXCLUDED — never faked.
- v1 acts ONLY on symbols present in ``targets``: held symbols absent from ``targets``
  are left untouched and do not appear in the output. (A future version may treat the
  full portfolio; documented here so the partial-weight behaviour is intentional.)
- ``new_weight`` is the resulting COMBINED position's reporting MV divided by the ORIGINAL
  total reporting MV (weights are relative to today's book, not a recomputed post-trade
  total); this keeps each row independent and is the honest, simplest choice for a preview.
- ``summary.over_allocated`` flags Σ(submitted targets) > 1 (informational — no hard
  block). ``summary.excluded_with_target`` surfaces symbols carrying a stored 目標配置
  weight that do not appear in the preview (not held / unpriced) so the UI never silently
  drops them.
- Money is ``Decimal`` end to end; the router serializes to wire strings.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from portfolio_dash.data_ingestion.config_seed import FeeRuleSet, get_fee_rule_set
from portfolio_dash.data_ingestion.rules_binding import fee_rule_for
from portfolio_dash.data_ingestion.store import list_instruments
from portfolio_dash.portfolio.dashboard import RateResolver, build_dashboard
from portfolio_dash.portfolio.dashboard_models import DashboardData, HoldingRow
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.fx import convert
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.strategy import target_weights as tw

_ZERO = Decimal("0")
_ONE = Decimal("1")
_HUNDRED = Decimal("100")
_THOUSAND = Decimal("1000")


def _round_shares(raw: Decimal, market: Market) -> Decimal:
    """Round raw shares to an integer; MY market snaps to the nearest 100-unit lot."""
    if market is Market.MY:
        return (raw / _HUNDRED).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * _HUNDRED
    return raw.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def _fee_rule_set_name(
    conn: sqlite3.Connection, account_id: str, market: Market
) -> str | None:
    """The account's fee-rule-set name for *market* (Batch B: market-aware via the
    (account, market) binding table). An unknown account -> None (degrade, don't crash).
    Every constituent of one symbol shares that symbol's single market, so the per-account
    rule stays unambiguous."""
    try:
        return fee_rule_for(conn, account_id, market)
    except KeyError:  # unknown account — preserve the pre-swap None return
        return None


def _priced_constituents(holdings: list[HoldingRow], symbol: str) -> list[HoldingRow]:
    """All priced, long (shares > 0) holdings of *symbol*, most-shares first.

    Tie-break by account_id (ascending) so the buy target and the sell greedy order are
    deterministic. Constituents share one quote currency and one market price (the price
    is keyed by symbol), so aggregation is exact. A non-positive price is treated as NO
    usable price (a degenerate/halted quote): such a symbol yields no constituents and is
    EXCLUDED — never fabricated, and never a divide-by-zero when sizing raw shares.
    """
    cons = [
        h
        for h in holdings
        if h.symbol == symbol
        and h.shares > _ZERO
        and h.market_price is not None
        and h.market_price > _ZERO
        and h.market_value is not None
    ]
    cons.sort(key=lambda h: (-h.shares, h.account_id))
    return cons


@dataclass
class _Leg:
    """One executing trade against a concrete account (fees bound to that account)."""

    account_id: str
    account_name: str
    side: Side
    shares: Decimal
    price: Decimal
    fee: Decimal
    tax: Decimal
    market: Market

    @property
    def amount(self) -> Decimal:
        return self.shares * self.price

    @property
    def odd_lot(self) -> bool:
        """Display hint: a TW leg whose shares are not a whole 1,000-share 張 (零股)."""
        return self.market is Market.TW and self.shares % _THOUSAND != _ZERO

    def wire(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "account_name": self.account_name,
            "side": self.side.value.lower(),
            "shares": self.shares,
            "amount": self.amount,
            "fee": self.fee,
            "tax": self.tax,
            "odd_lot": self.odd_lot,
        }


def _excluded_with_target(
    data: DashboardData, stored: dict[str, Decimal], missing: set[str]
) -> list[str]:
    """Stored-target symbols that will NOT appear as a preview row (not held / unpriced).

    A symbol is "held-and-priced" (and so IS a preview row) when it has at least one
    holding carrying a current price. Any stored 目標配置 symbol outside that set is
    surfaced so the UI can note it instead of dropping it silently.
    """
    priced_syms = {
        h.symbol
        for h in data.holdings
        if h.market_price is not None
        and h.market_price > _ZERO
        and h.symbol not in missing
    }
    return sorted(sym for sym in stored if sym not in priced_syms)


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
    from portfolio_dash.data_ingestion.fees import (  # local: avoid cycle risk
        compute_fees,
        forecast_tw_rebate,
    )
    from portfolio_dash.data_ingestion.fx_lookup import resolve_stamp_fx

    data = build_dashboard(conn, now=now, reporting=reporting)
    # FE-D2 estimate path: resolve the current USD/MYR rate once for any Moomoo US leg's MY
    # stamp (silent omit + snapshot note if unavailable). Ignored by non-US-stamp rules.
    stamp_fx = resolve_stamp_fx(conn, now.date())
    resolver = RateResolver(conn, now=now)
    total = data.kpis.total_market_value

    instruments = {i.symbol: i for i in list_instruments(conn)}
    missing = set(data.freshness.missing_prices)
    stored_targets = tw.load_target_weights(conn)

    # over_allocated: submitted targets exceed 100% (flag only — the preview stays
    # informational; the router does not hard-block Σ > 1).
    submitted_sum = _ZERO
    for ratio in targets.values():
        submitted_sum += ratio
    over_allocated = submitted_sum > _ONE
    excluded_with_target = _excluded_with_target(data, stored_targets, missing)

    rows: list[dict[str, object]] = []
    excluded: list[str] = []

    # Degrade honestly: with no priced total there is nothing to rebalance against.
    if total is None or total == _ZERO:
        excluded = list(targets)
        return {
            "rows": rows,
            "summary": {
                "turnover_reporting": _ZERO,
                "total_fees_reporting": _ZERO,
                "cash_after": _ZERO,
                "excluded": excluded,
                "over_allocated": over_allocated,
                "excluded_with_target": excluded_with_target,
                "rebate_estimate_total": None,
                "note": "total market value unavailable; nothing to rebalance",
            },
        }

    turnover_reporting = _ZERO
    total_fees_reporting = _ZERO
    cash_after = _ZERO
    # FE-D1 forecast HINT (不計入成本): Σ per-TW-leg floor(fee × rebate_rate). None when no leg
    # rebates (every non-TW account) so the drawer/report footnote only shows where it applies.
    rebate_estimate_total = _ZERO

    for symbol, target_ratio in targets.items():
        cons = _priced_constituents(data.holdings, symbol)
        # Exclude any symbol without a usable current price (never fabricate one).
        if not cons or symbol in missing:
            excluded.append(symbol)
            continue

        quote_ccy = cons[0].quote_ccy
        price = cons[0].market_price
        assert price is not None  # _priced_constituents guarantees a price
        try:
            rate = resolver.rate(quote_ccy, reporting)
        except KeyError:
            excluded.append(symbol)
            continue

        # Every constituent's account must resolve a fee rule set (a seeded account always
        # does; degrade honestly rather than crash if the row is somehow missing).
        rules_by_acct: dict[str, FeeRuleSet] = {}
        missing_rule = False
        for h in cons:
            rn = _fee_rule_set_name(conn, h.account_id, h.market)
            if rn is None:
                missing_rule = True
                break
            rules_by_acct[h.account_id] = get_fee_rule_set(rn, conn)
        if missing_rule:
            excluded.append(symbol)
            continue

        # Aggregate the COMBINED position across all accounts (exact: one price/ccy).
        combined_shares = _ZERO
        combined_mv_quote = _ZERO
        for h in cons:
            combined_shares += h.shares
            assert h.market_value is not None  # _priced_constituents guarantees a value
            combined_mv_quote += h.market_value

        current_mv_reporting = convert(combined_mv_quote, rate)
        current_weight = current_mv_reporting / total
        target_mv_reporting = target_ratio * total
        delta_reporting = target_mv_reporting - current_mv_reporting

        accounts_field: list[dict[str, object]] = [
            {"account_id": h.account_id, "account_name": h.account_name,
             "shares": h.shares}
            for h in cons
        ]

        if delta_reporting == _ZERO:
            continue  # already on target — no trade row

        side = Side.BUY if delta_reporting > _ZERO else Side.SELL
        # reporting -> quote is 1/rate; share count then divides by quote-ccy price.
        delta_quote = abs(delta_reporting) / rate
        raw_shares = delta_quote / price

        is_etf = instruments[symbol].is_etf
        legs: list[_Leg] = []
        if side is Side.BUY:
            # One leg routed to the most-shares account (cons[0]; tie-break account_id).
            target_h = cons[0]
            leg_shares = _round_shares(raw_shares, target_h.market)
            if leg_shares > _ZERO:
                fr = compute_fees(rules_by_acct[target_h.account_id], side, leg_shares,
                                  price, is_etf=is_etf, stamp_fx=stamp_fx)
                legs.append(_Leg(account_id=target_h.account_id,
                                 account_name=target_h.account_name, side=side,
                                 shares=leg_shares, price=price, fee=fr.fee, tax=fr.tax,
                                 market=target_h.market))
        else:
            # Greedy sell: most-shares account first, each leg bounded by its shares, until
            # the delta is covered. Snapping is per leg; a full-liquidation leg sells the
            # account's exact shares (target 0 -> every account emptied).
            remaining = raw_shares
            for h in cons:  # already most-shares first
                if remaining <= _ZERO:
                    break
                cap = h.shares
                if remaining >= cap:
                    leg_shares = cap  # sell the whole account
                else:
                    leg_shares = _round_shares(remaining, h.market)
                    if leg_shares > cap:
                        leg_shares = cap  # a snap-up can never exceed the held shares
                if leg_shares <= _ZERO:
                    continue
                fr = compute_fees(rules_by_acct[h.account_id], side, leg_shares, price,
                                  is_etf=is_etf, stamp_fx=stamp_fx)
                legs.append(_Leg(account_id=h.account_id, account_name=h.account_name,
                                 side=side, shares=leg_shares, price=price, fee=fr.fee,
                                 tax=fr.tax, market=h.market))
                remaining -= leg_shares

        total_shares = _ZERO
        total_amount = _ZERO
        total_fee = _ZERO
        total_tax = _ZERO
        for lg in legs:
            total_shares += lg.shares
            total_amount += lg.amount
            total_fee += lg.fee
            total_tax += lg.tax
        if total_shares == _ZERO:
            continue  # rounds to no trade

        signed = total_shares if side is Side.BUY else -total_shares
        new_combined_shares = combined_shares + signed
        new_position_reporting = convert(new_combined_shares * price, rate)
        new_weight = new_position_reporting / total  # vs ORIGINAL total (documented)

        rows.append({
            "symbol": symbol,
            "current_weight": current_weight,   # COMBINED across accounts
            "target_weight": target_ratio,
            "side": side.value.lower(),
            "shares": total_shares,
            "amount": total_amount,
            "ccy": quote_ccy.value,
            "fee": total_fee,
            "tax": total_tax,
            "new_weight": new_weight,
            "accounts": accounts_field,
            "legs": [lg.wire() for lg in legs],
        })

        for lg in legs:
            leg_rate = rules_by_acct[lg.account_id].rebate_rate
            if leg_rate > _ZERO:  # convert to reporting ccy (leg fee is in the row's quote ccy)
                rebate_estimate_total += convert(forecast_tw_rebate(lg.fee, leg_rate), rate)

        turnover_reporting += convert(total_amount, rate)
        total_fees_reporting += convert(total_fee + total_tax, rate)
        if side is Side.SELL:
            cash_after += convert(total_amount - total_fee - total_tax, rate)  # net in
        else:
            cash_after -= convert(total_amount + total_fee + total_tax, rate)  # cost out

    return {
        "rows": rows,
        "summary": {
            "turnover_reporting": turnover_reporting,
            "total_fees_reporting": total_fees_reporting,
            "cash_after": cash_after,
            "excluded": excluded,
            "over_allocated": over_allocated,
            "excluded_with_target": excluded_with_target,
            "rebate_estimate_total": (
                rebate_estimate_total if rebate_estimate_total > _ZERO else None
            ),
        },
    }
