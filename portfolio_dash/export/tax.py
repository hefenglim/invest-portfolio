"""Annual tax-package export (spec 02): realized gains, dividends, FX realized, summary.

Year-cut by trade date (sell date / dividend date / conversion date). Per-currency
rows are never summed across currencies. Reporting conversion uses trade-date FX.
"""

import sqlite3
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal

from portfolio_dash.data_ingestion.store import (
    list_accounts,
    list_dividends,
    list_fx_conversions,
    list_instruments,
    list_opening,
    list_transactions,
)
from portfolio_dash.export.artifact import ExportArtifact, csv_blob, zip_artifact
from portfolio_dash.forex.fx_pnl import realized_fx_rows
from portfolio_dash.forex.pools import average_acquisition_rate
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.pricing.store import get_fx_on
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.enums import DividendType
from portfolio_dash.shared.models.ledger import (
    Dividend,
    FXConversion,
    OpeningInventory,
    Transaction,
)

_ONE = Decimal("1")
_ZERO = Decimal("0")
_REALIZED_COLS = ["sell_date", "account_id", "symbol", "quote_ccy", "shares_sold",
                  "proceeds_net", "original_cost_removed", "adjusted_cost_removed",
                  "realized", "rate_used", "reporting_realized"]
_DIV_COLS = ["date", "account_id", "symbol", "type", "gross", "withholding", "net", "ccy"]
_FX_COLS = ["date", "account_id", "home_ccy", "foreign_ccy", "foreign_sold",
            "home_received", "rate_used", "realized"]


def _rate_on(conn: sqlite3.Connection, d: date, base: Currency,
             quote: Currency) -> Decimal | None:
    """Trade-date base->quote rate via direct lookup, then inverse fallback. None if absent."""
    if base == quote:
        return _ONE
    direct = get_fx_on(conn, base, quote, on=d)
    if direct is not None:
        return direct.rate
    inverse = get_fx_on(conn, quote, base, on=d)
    if inverse is not None:
        return _ONE / inverse.rate
    return None


def build_tax_package_zip(
    conn: sqlite3.Connection, *, now: datetime, year: int, reporting: Currency
) -> ExportArtifact:
    """Build the annual tax package zip (realized gains + dividends + FX realized + summary).

    ``now`` is accepted for signature parity with sibling exports (audit logging is the
    router's concern); the package content is year-cut, not as-of ``now``.
    """
    txs = [Transaction(account_id=s.account_id, symbol=s.symbol, side=s.side,
                       quantity=s.quantity, price=s.price, fees=s.fees, tax=s.tax,
                       trade_date=s.trade_date) for s in list_transactions(conn)]
    divs = [Dividend(account_id=s.account_id, symbol=s.symbol, date=s.date,
                     type=DividendType(s.type), gross=s.gross, withholding=s.withholding,
                     net=s.net, reinvest_shares=s.reinvest_shares,
                     reinvest_price=s.reinvest_price) for s in list_dividends(conn)]
    opening = [OpeningInventory(account_id=s.account_id, symbol=s.symbol, shares=s.shares,
                                original_cost_total=s.original_cost_total,
                                build_date=s.build_date) for s in list_opening(conn)]
    convs = [FXConversion(account_id=s.account_id, date=s.date, from_ccy=s.from_ccy,
                          from_amount=s.from_amount, to_ccy=s.to_ccy,
                          to_amount=s.to_amount) for s in list_fx_conversions(conn)]
    instruments = {i.symbol: i for i in list_instruments(conn)}
    accounts = {a.account_id: a for a in list_accounts(conn)}
    book = build_book(txs, divs, opening, instruments)

    realized_rows: list[list[str]] = []
    realized_subtotal: dict[Currency, Decimal] = defaultdict(lambda: _ZERO)
    for r in book.realized.rows:
        if r.sell_date.year != year:
            continue
        rate = _rate_on(conn, r.sell_date, r.quote_ccy, reporting)
        reporting_realized = "" if rate is None else str(r.realized * rate)
        realized_rows.append([
            r.sell_date.isoformat(), r.account_id, r.symbol, r.quote_ccy.value,
            str(r.shares_sold), str(r.proceeds_net), str(r.original_cost_removed),
            str(r.adjusted_cost_removed), str(r.realized),
            "" if rate is None else str(rate), reporting_realized,
        ])
        realized_subtotal[r.quote_ccy] += r.realized

    div_rows: list[list[str]] = []
    div_subtotal: dict[Currency, Decimal] = defaultdict(lambda: _ZERO)
    for d in divs:
        if d.date.year != year or d.type is DividendType.STOCK:
            continue
        ccy = instruments[d.symbol].quote_ccy
        div_rows.append([
            d.date.isoformat(), d.account_id, d.symbol, d.type.value.lower(),
            str(d.gross), str(d.withholding), str(d.net), ccy.value,
        ])
        div_subtotal[ccy] += d.net

    fx_rows: list[list[str]] = []
    fx_subtotal: dict[Currency, Decimal] = defaultdict(lambda: _ZERO)
    for acct in accounts.values():
        if acct.settlement_ccy == acct.funding_ccy:
            continue
        home, foreign = acct.funding_ccy, acct.settlement_ccy
        acct_convs = [c for c in convs if c.account_id == acct.account_id]
        avg = average_acquisition_rate(acct_convs, home, foreign)
        for fr in realized_fx_rows(acct_convs, home, foreign, avg):
            if fr.date.year != year:
                continue
            fx_rows.append([fr.date.isoformat(), acct.account_id, fr.home_ccy.value,
                            fr.foreign_ccy.value, str(fr.foreign_sold),
                            str(fr.home_received), str(fr.rate_used), str(fr.realized)])
            fx_subtotal[fr.home_ccy] += fr.realized

    files: dict[str, bytes] = {
        f"realized_gains_{year}.csv": csv_blob(_REALIZED_COLS, realized_rows),
        f"dividends_{year}.csv": csv_blob(_DIV_COLS, div_rows),
        f"fx_realized_{year}.csv": csv_blob(_FX_COLS, fx_rows),
        "summary.md": _summary_md(year, realized_subtotal, div_subtotal, fx_subtotal),
    }
    return zip_artifact(f"tax_package_{year}.zip", files)


def _subtotal_lines(subtotal: dict[Currency, Decimal]) -> str:
    if not subtotal:
        return "- （無）\n"
    return "".join(
        f"- {ccy.value}: {amt}\n"
        for ccy, amt in sorted(subtotal.items(), key=lambda kv: kv[0].value)
    )


def _summary_md(year: int, realized: dict[Currency, Decimal],
                dividends: dict[Currency, Decimal],
                fx: dict[Currency, Decimal]) -> bytes:
    md = (
        f"# Tax Package {year}\n\n"
        "Per-currency subtotals (never summed across currencies).\n\n"
        f"## Realized gains\n{_subtotal_lines(realized)}\n"
        f"## Dividends (net)\n{_subtotal_lines(dividends)}\n"
        f"## Realized FX P&L\n{_subtotal_lines(fx)}\n"
    )
    return md.encode("utf-8")
