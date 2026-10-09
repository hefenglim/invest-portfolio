"""Valuation: fill market fields and unrealized P&L from a current-price map."""

from decimal import Decimal

from portfolio_dash.portfolio.results import Holding


def value_holdings(holdings: list[Holding], price_map: dict[str, Decimal]) -> list[Holding]:
    """Return new Holdings with market fields filled. Missing price -> stale, never faked.

    **Which 待釐清 flags suppress valuation, and why** (audit F-49, 2026-08-10 — the rule,
    stated here rather than left to be inferred from whichever flag is listed first):

    ``market_value = price × shares``, and ``price`` is global, current and already reflects
    every corporate action and split. So the ONLY question is **are the shares right?**

    * ``oversold`` — shares are negative and the cost basis was discarded. SUPPRESS.
    * ``unbookable_action`` — a corporate action was skipped, so ``shares`` is still in
      PRE-action terms while the price is POST-action: the product is wrong by the action's
      whole ratio (a 3-for-1 understates the position threefold). SUPPRESS.
    * ``unbookable_dividend`` — shares are RIGHT; only ``adjusted_cost_total`` is short one
      payout. Its market value is genuinely correct, so it must NOT be suppressed — nulling
      it would hide a real position from every aggregate over a cost-side gap. It carries
      the display flag only.
    * ``short_open`` — a real, priced position with a signed quantity. Never suppressed.

    A new flag is decided against THAT question, not by copying the neighbouring branch.

    **Containment (owner requirement 2026-08-10).** The suppression is PER POSITION and must
    stay that way: nulling one holding's market fields makes every aggregate exclude that one
    holding and keep computing for all the others, so a defect anywhere in the corporate-
    action flow can damage at most the one stock that has a corporate action. Never widen
    this into a portfolio-wide bail-out — the only figure that legitimately blanks whole is
    XIRR, and it does so at its own gate in ``dashboard.py``, where the cost is spelt out.
    """
    out: list[Holding] = []
    for h in holdings:
        price = price_map.get(h.symbol)
        if h.oversold or h.unbookable_action:
            # 賣超 / 無法套用的公司行動: value + P&L are 待釐清 (not computed). Keep the
            # current price for display, but null the value fields so all aggregate code
            # that gates on `market_value is not None` excludes this position automatically.
            out.append(
                h.model_copy(
                    update={
                        "market_price": price,
                        "market_value": None,
                        "unrealized_pnl": None,
                        "capital_gain": None,
                        "price_stale": price is None,
                    }
                )
            )
            continue
        if price is None:
            out.append(
                h.model_copy(
                    update={
                        "market_price": None,
                        "market_value": None,
                        "unrealized_pnl": None,
                        "capital_gain": None,
                        "price_stale": True,
                    }
                )
            )
        else:
            out.append(
                h.model_copy(
                    update={
                        "market_price": price,
                        "market_value": price * h.shares,
                        "unrealized_pnl": (price - h.adjusted_avg) * h.shares,
                        "capital_gain": (price - h.original_avg) * h.shares,
                        "price_stale": False,
                    }
                )
            )
    return out
