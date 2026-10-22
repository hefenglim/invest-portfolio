"""The trend / net-worth / benchmark inputs ``build_dashboard`` assembles, read the same way.

v0.1.31's ``portfolio/dashboard.py`` steps 1, 1b, 1c (the bundle and its valuation cut), 9 (the
trend's price and FX histories — ONLY the ledger's quote currencies), 9b (the cash series'
rows and FX histories — the cash pools' currencies) and 10 (the benchmark closes), so the
frozen functions and the live ones can be handed IDENTICAL inputs. Each consumer keeps exactly
the FX pair set it reads in v0.1.31 (spec §8 item 5): handing the trend a pair it does not read
would change ``available`` on the ``unquoted_ccy_cash`` corpus. The shipping dashboard reads a
wider trend set since PERF-F2's fix (``timeseries.trend_fx_currencies``: the quote currencies
and every converted cash movement's currency); these inputs keep v0.1.31's ON PURPOSE, because
the engine-equality tests compare two engines on one set of inputs, not two pair sets.

The one book this needs (for the cash series' booked dividends) is the FROZEN engine's, so the
inputs depend on no live replay. Read-only over the connection.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from portfolio_dash.data_ingestion.store import (
    StoredCashMovement,
    StoredDividend,
    StoredFxConversion,
    StoredTransaction,
    list_cash_movements,
    list_dividends,
    list_fx_conversions,
    list_transactions,
    load_ledger_bundle,
)
from portfolio_dash.portfolio.benchmark_counterfactual import ReportingFlow
from portfolio_dash.portfolio.cash import cash_balances
from portfolio_dash.portfolio.price_basis import series_in
from portfolio_dash.portfolio.returns import booked_dividends
from portfolio_dash.portfolio.twr import convert_closes
from portfolio_dash.pricing.benchmarks import benchmark_for_market
from portfolio_dash.pricing.store import get_fx_history, get_price_history
from portfolio_dash.shared.corporate_actions import ActionIndex
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.ledger import LedgerBundle, valued_rows_as_of
from tests.reference.cost_basis_v0131 import ref_build_book
from tests.reference.trend_v0131 import FxHistory, PriceHistory

#: "From the beginning" bounds, as ``dashboard.py``'s ``_EPOCH`` / ``_BENCH_EPOCH`` at 473fe5a.
#: Local copies on purpose: the bounded loads of spec §8 item 5 may retire those constants.
EPOCH = date(1900, 1, 1)
BENCH_EPOCH = date(1990, 1, 1)


@dataclass(frozen=True)
class DashboardInputs:
    as_of: date
    reporting: Currency
    #: Unregistered rows dropped, then cut at ``as_of`` (``valued_as_of``) — the dashboard's.
    bundle: LedgerBundle
    #: Cash movements cut at ``as_of`` (``valued_rows_as_of``).
    cash_movements: list[StoredCashMovement]
    price_history: PriceHistory
    #: The trend's pairs: the ledger's quote currencies against the reporting currency.
    trend_fx_history: FxHistory
    #: The cash series' rows (uncut, as ``dashboard.py`` 9b reads them) and its pairs.
    cash_txs: list[StoredTransaction]
    cash_divs: list[StoredDividend]
    cash_convs: list[StoredFxConversion]
    instruments: dict[str, Instrument]
    cash_fx_history: FxHistory
    actions: ActionIndex
    has_events: bool


def _fx_pairs(conn: sqlite3.Connection, ccys: set[Currency], reporting: Currency,
              as_of: date) -> FxHistory:
    out: FxHistory = {}
    for ccy in sorted(ccys, key=lambda c: c.value):
        if ccy == reporting:
            continue
        for base, quote in ((ccy, reporting), (reporting, ccy)):
            rows = get_fx_history(conn, base, quote, EPOCH, as_of)
            if rows:
                out[(base, quote)] = [(r.as_of, r.rate) for r in rows]
    return out


def load_inputs(conn: sqlite3.Connection, now: datetime, reporting: Currency
                ) -> DashboardInputs:
    as_of = now.date()
    bundle = load_ledger_bundle(conn)
    if bundle.unregistered_symbols:
        bundle = bundle.without_unregistered()
    bundle = bundle.valued_as_of(as_of)
    instruments = bundle.instruments
    cash_movements = valued_rows_as_of(list_cash_movements(conn), as_of)
    txs, divs, opening = bundle.transactions, bundle.dividends, bundle.opening
    has_events = bool(txs or divs or opening)

    ledger_symbols = sorted({t.symbol for t in txs} | {d.symbol for d in divs}
                            | {o.symbol for o in opening}
                            | {a.from_symbol for a in bundle.actions}
                            | {a.to_symbol for a in bundle.actions})
    price_history: PriceHistory = {
        sym: [(p.as_of, p.value) for p in get_price_history(conn, sym, EPOCH, as_of)]
        for sym in ledger_symbols
    }
    trend_fx = _fx_pairs(conn, {instruments[s].quote_ccy for s in ledger_symbols},
                         reporting, as_of)

    book = ref_build_book(bundle, allow_oversell=True)
    cash_txs = list_transactions(conn)
    cash_divs = booked_dividends(list_dividends(conn), book.refused_dividends)
    cash_convs = list_fx_conversions(conn)
    cash_ccys = {ccy for _acct, ccy in cash_balances(
        cash_movements, cash_convs, cash_txs, cash_divs, instruments)}
    cash_fx = _fx_pairs(conn, cash_ccys, reporting, as_of)

    return DashboardInputs(
        as_of=as_of, reporting=reporting, bundle=bundle, cash_movements=cash_movements,
        price_history=price_history, trend_fx_history=trend_fx, cash_txs=cash_txs,
        cash_divs=cash_divs, cash_convs=cash_convs, instruments=instruments,
        cash_fx_history=cash_fx, actions=ActionIndex.build(bundle.actions),
        has_events=has_events,
    )


def benchmark_closes(conn: sqlite3.Connection, inputs: DashboardInputs,
                     flows: Sequence[ReportingFlow]
                     ) -> dict[Market, list[tuple[date, Decimal]]]:
    """Each flow market's benchmark closes in the reporting currency (``dashboard.py`` 10)."""
    out: dict[Market, list[tuple[date, Decimal]]] = {}
    for market in sorted({f.market for f in flows if f.market is not None},
                         key=lambda m: m.value):
        bench = benchmark_for_market(market)
        if bench is None:
            continue
        raw = [(p.as_of, p.value) for p in series_in(
            inputs.actions, bench.storage_key,
            get_price_history(conn, bench.storage_key, BENCH_EPOCH, inputs.as_of),
            valued_on=inputs.as_of)]
        converted = convert_closes(raw, inputs.trend_fx_history, bench.quote_ccy,
                                   inputs.reporting)
        if converted:
            out[market] = converted
    return out


__all__ = ["BENCH_EPOCH", "EPOCH", "DashboardInputs", "benchmark_closes", "load_inputs"]
