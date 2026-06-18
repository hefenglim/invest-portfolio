"""Valuation: fill market fields and unrealized P&L from a current-price map."""

from decimal import Decimal

from portfolio_dash.portfolio.results import Holding


def value_holdings(holdings: list[Holding], price_map: dict[str, Decimal]) -> list[Holding]:
    """Return new Holdings with market fields filled. Missing price -> stale, never faked."""
    out: list[Holding] = []
    for h in holdings:
        price = price_map.get(h.symbol)
        if h.oversold:
            # 賣超 (negative shares): value + P&L are 待釐清 (not computed). Keep the current
            # price for display, but null the value fields so all aggregate code that gates on
            # `market_value is not None` excludes this position automatically.
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
