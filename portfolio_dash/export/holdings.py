"""Holdings snapshot export (spec 02). Reuses build_dashboard; computes no numbers."""

import sqlite3
from datetime import datetime
from decimal import Decimal

from portfolio_dash.export.artifact import ExportArtifact, csv_artifact
from portfolio_dash.portfolio.dashboard import RateResolver, build_dashboard
from portfolio_dash.portfolio.dashboard_models import HoldingRow
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.fx import convert
from portfolio_dash.shared.wire import decimal_str

_COLUMNS = [
    "symbol", "name", "market", "board", "account_id", "quote_ccy", "shares",
    "original_avg", "adjusted_avg", "original_cost_total", "adjusted_cost_total",
    "market_price", "price_as_of", "price_stale", "market_value", "unrealized_pnl",
    "capital_gain", "dividend_portion", "payback_ratio", "weight", "reporting_ccy_value",
    # M1-03 / D21: whose dividends `payback_ratio` is built on (a SPINOFF child's ratio is its
    # parent's). APPENDED so every existing column keeps its index; empty on a position no
    # SPINOFF fed, exactly like every other None cell.
    "payback_from_symbol", "payback_carried_dividends", "payback_own_dividends",
]


def _s(value: object) -> str:
    """Raw cell: Decimal/str/bool/date -> str; None -> empty.

    A Decimal goes through ``decimal_str`` — the ONE canonical wire form (wire.py):
    fixed-point, full source precision, NEVER scientific notation. Bare ``str()`` is not
    that form: it switches to exponent notation on the value's exponent alone, so an exact
    zero inherited from a repeating average printed ``0E-24`` here while ``/api/dashboard``
    served ``0.000000000000000000000000`` for the same field (M8-01). Same number, different
    string — and this CSV is a reconciliation artifact whose cells are diffed against the
    API/screen as text.
    """
    if isinstance(value, Decimal):
        return decimal_str(value)
    return "" if value is None else str(value)


def _matches(h: HoldingRow, account: str | None, market: Market | None) -> bool:
    """The holdings-filter predicate — mirrors the dashboard chips (account / market),
    each independent; None means "all" on that axis (so no filter == every row)."""
    return (account is None or h.account_id == account) and (
        market is None or h.market == market
    )


def build_holdings_csv(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    reporting: Currency,
    account: str | None = None,
    market: Market | None = None,
) -> ExportArtifact:
    # Optional (account, market) filter so the CSV follows the dashboard's active chips.
    # Filtering the row SET only — every money value still comes straight from the
    # per-holding numbers build_dashboard already computed (no re-computation here).
    data = build_dashboard(conn, now=now, reporting=reporting)
    resolver = RateResolver(conn, now=now)
    holdings = [h for h in data.holdings if _matches(h, account, market)]
    rows: list[list[str]] = []
    for h in holdings:
        reporting_value = ""
        if h.market_value is not None:
            try:
                # Same canonical form as every other Decimal cell (see `_s`): this one is
                # derived here rather than read off the row, so it needs the call directly.
                reporting_value = decimal_str(convert(h.market_value,
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
            _s(h.payback_from_symbol), _s(h.payback_carried_dividends),
            _s(h.payback_own_dividends),
        ])
    as_of = data.as_of.date().isoformat()
    fx_rates = _fx_footer(resolver, reporting)
    footer = [f"as_of={as_of}, fx_rates={{{fx_rates}}}, generated={now.isoformat()}"]
    # When a filter is active, record it in the footer AND the filename so the download is
    # self-describing (an unfiltered export keeps the byte-identical name/footer it had).
    suffix = ""
    if account is not None or market is not None:
        filt = f"account={account or 'all'}, market={market.value if market else 'all'}"
        footer.append(f"filter: {filt}")
        suffix = f"_{account or 'all'}_{market.value if market else 'all'}"
    return csv_artifact(f"holdings_snapshot_{as_of}{suffix}.csv",
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
