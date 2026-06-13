"""Holdings snapshot export (spec 02). Reuses build_dashboard; computes no numbers."""

import sqlite3
from datetime import datetime

from portfolio_dash.export.artifact import ExportArtifact, csv_artifact
from portfolio_dash.portfolio.dashboard import RateResolver, build_dashboard
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.fx import convert

_COLUMNS = [
    "symbol", "name", "market", "board", "account_id", "quote_ccy", "shares",
    "original_avg", "adjusted_avg", "original_cost_total", "adjusted_cost_total",
    "market_price", "price_as_of", "price_stale", "market_value", "unrealized_pnl",
    "capital_gain", "dividend_portion", "payback_ratio", "weight", "reporting_ccy_value",
]


def _s(value: object) -> str:
    """Raw cell: Decimal/str/bool/date -> str; None -> empty."""
    return "" if value is None else str(value)


def build_holdings_csv(
    conn: sqlite3.Connection, *, now: datetime, reporting: Currency
) -> ExportArtifact:
    data = build_dashboard(conn, now=now, reporting=reporting)
    resolver = RateResolver(conn, now=now)
    rows: list[list[str]] = []
    for h in data.holdings:
        reporting_value = ""
        if h.market_value is not None:
            try:
                reporting_value = str(convert(h.market_value,
                                              resolver.rate(h.quote_ccy, reporting)))
            except KeyError:
                reporting_value = ""  # missing FX -> blank, never fabricated
        rows.append([
            _s(h.symbol), _s(h.name), h.market.value, _s(h.board), _s(h.account_id),
            h.quote_ccy.value, _s(h.shares), _s(h.original_avg), _s(h.adjusted_avg),
            _s(h.original_cost_total), _s(h.adjusted_cost_total), _s(h.market_price),
            _s(h.price_as_of), _s(h.price_stale), _s(h.market_value), _s(h.unrealized_pnl),
            _s(h.capital_gain), _s(h.dividend_portion), _s(h.payback_ratio), _s(h.weight),
            reporting_value,
        ])
    as_of = data.as_of.date().isoformat()
    fx_rates = _fx_footer(resolver, reporting)
    footer = [f"as_of={as_of}, fx_rates={{{fx_rates}}}, generated={now.isoformat()}"]
    return csv_artifact(f"holdings_snapshot_{as_of}.csv",
                        header=_COLUMNS, rows=rows, footer_lines=footer)


def _fx_footer(resolver: RateResolver, reporting: Currency) -> str:
    """Best-effort current rates for the non-reporting currencies (USD, MYR)."""
    parts: list[str] = []
    for ccy in (Currency.USD, Currency.MYR):
        if ccy == reporting:
            continue
        try:
            parts.append(f"{ccy.value}:{resolver.rate(ccy, reporting)}")
        except KeyError:
            parts.append(f"{ccy.value}:n/a")
    return ", ".join(parts)
