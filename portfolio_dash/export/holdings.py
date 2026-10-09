"""Holdings snapshot export (spec 02). Reuses build_dashboard; computes no numbers.

Options (spec §9): the stock rows are exactly what they were — ``data.holdings`` is stock-only
(OPT-D5). On a ledger with an option (``data.options`` present, and ONLY then) the file gains
two things after the stock rows: a 選擇權 block — a blank row, a 「選擇權」 title row, its own
header and one row per open contract (the §7.1 ``open[]`` fields, same filter as the stock
rows, plus the reporting-currency value) — and a footer line naming the KPI 總市值 and the
「其中選擇權」 part of it (the stock rows' values do not include it). A stock ledger's file is
byte-identical (spec §10, ``tests/export/test_options_w6_stock_exports_unchanged.py``).
"""

import sqlite3
from datetime import datetime
from decimal import Decimal

from portfolio_dash.data_ingestion.store import list_instruments
from portfolio_dash.export.artifact import ExportArtifact, csv_artifact
from portfolio_dash.portfolio.dashboard import RateResolver, build_dashboard
from portfolio_dash.portfolio.dashboard_models import HoldingRow, OptionOpenRow, OptionsBlock
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.fx import convert
from portfolio_dash.shared.models.assets import Instrument
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

#: The 選擇權 block's title row (spec §9) — a one-cell row, so a reader splitting the file on
#: blank rows finds the second table by its title.
OPTIONS_BLOCK_TITLE = "選擇權"

#: The block's columns: the §7.1 ``open[]`` keys in their wire order, then the reporting-
#: currency value (as the stock rows carry it). ``share_equivalents`` is SIGNED (a sold
#: contract is negative, like ``shares`` above); ``contracts`` is the unsigned 口數.
OPTION_COLUMNS = [
    "account_id", "symbol", "contract", "underlying", "expiry", "days_to_expiry", "strike",
    "right", "direction", "contracts", "share_equivalents", "average_premium",
    "premium_total", "last_price", "last_price_date", "market_value", "unrealized",
    "obligation", "underlying_price", "underlying_price_date", "past_expiry",
    "unbookable_close", "oversold", "reporting_ccy_value",
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


def option_rows_matching(
    block: OptionsBlock, contracts: dict[str, Instrument], account: str | None,
    market: Market | None,
) -> list[OptionOpenRow]:
    """The open contracts the (account, market) chips keep — the stock predicate, with each
    contract's market read off the registry (*contracts*: symbol → its ``Instrument``)."""
    return [r for r in block.open
            if (account is None or r.account_id == account)
            and (market is None or (r.symbol in contracts
                                    and contracts[r.symbol].market == market))]


def option_contracts(conn: sqlite3.Connection) -> dict[str, Instrument]:
    """Every registered option contract, by symbol (its market and currency)."""
    return {i.symbol: i for i in list_instruments(conn) if i.is_option}


def reporting_value(value: Decimal | None, ccy: Currency, resolver: RateResolver,
                    reporting: Currency) -> Decimal | None:
    """*value* converted to the reporting currency — ``None`` on a missing value or rate,
    never fabricated."""
    if value is None:
        return None
    try:
        return convert(value, resolver.rate(ccy, reporting))
    except KeyError:
        return None


def _reporting_cell(value: Decimal | None, ccy: Currency, resolver: RateResolver,
                    reporting: Currency) -> str:
    # Same canonical form as every other Decimal cell (see `_s`): this one is derived here
    # rather than read off the row, so it needs the call directly; missing FX -> blank.
    converted = reporting_value(value, ccy, resolver, reporting)
    return "" if converted is None else decimal_str(converted)


def _option_rows(
    conn: sqlite3.Connection, block: OptionsBlock, resolver: RateResolver,
    reporting: Currency, account: str | None, market: Market | None,
) -> list[list[str]]:
    """The 選擇權 block: a blank row, the title, the header, one row per open contract."""
    contracts = option_contracts(conn)
    out: list[list[str]] = [[], [OPTIONS_BLOCK_TITLE], list(OPTION_COLUMNS)]
    for r in option_rows_matching(block, contracts, account, market):
        inst = contracts.get(r.symbol)
        ccy = inst.quote_ccy if inst is not None else Currency.USD
        out.append([
            r.account_id, r.symbol, r.contract, r.underlying, _s(r.expiry),
            _s(r.days_to_expiry), _s(r.strike), r.right, r.direction, _s(r.contracts),
            _s(r.share_equivalents), _s(r.average_premium), _s(r.premium_total),
            _s(r.last_price), _s(r.last_price_date), _s(r.market_value), _s(r.unrealized),
            _s(r.obligation), _s(r.underlying_price), _s(r.underlying_price_date),
            _s(r.past_expiry), _s(r.unbookable_close), _s(r.oversold),
            _reporting_cell(r.market_value, ccy, resolver, reporting),
        ])
    return out


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
        rows.append([
            _s(h.symbol), _s(h.name), h.market.value, _s(h.board), _s(h.account_id),
            h.quote_ccy.value, _s(h.shares), _s(h.original_avg), _s(h.adjusted_avg),
            _s(h.original_cost_total), _s(h.adjusted_cost_total), _s(h.market_price),
            _s(h.price_as_of), _s(h.price_stale), _s(h.market_value), _s(h.unrealized_pnl),
            _s(h.capital_gain), _s(h.dividend_portion), _s(h.payback_ratio), _s(h.weight),
            _reporting_cell(h.market_value, h.quote_ccy, resolver, reporting),
            _s(h.payback_from_symbol), _s(h.payback_carried_dividends),
            _s(h.payback_own_dividends),
        ])
    if data.options is not None:
        rows.extend(_option_rows(conn, data.options, resolver, reporting, account, market))
    as_of = data.as_of.date().isoformat()
    fx_rates = _fx_footer(resolver, reporting)
    footer = [f"as_of={as_of}, fx_rates={{{fx_rates}}}, generated={now.isoformat()}"]
    if data.options is not None:
        # The KPI 總市值 carries the open contracts' value (OPT-D5); the stock rows above do
        # not. Say how much of the whole-portfolio figure is options, whatever the filter.
        total = _s(data.kpis.total_market_value) or "n/a"
        opt_mv = _s(data.options.market_value.reporting) or "n/a"
        footer.append(f"total_market_value={total}, 其中選擇權={opt_mv}, "
                      f"reporting_ccy={reporting.value}")
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
