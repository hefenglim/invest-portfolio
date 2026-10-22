"""The prefix property, checked through the triggers (spec 2026-10-18 §4, §18 "Prefix property";
plan W1-B tests).

For a fixed-seed sequence of ledger, reference, price and FX writes on a corpus whose store is
settled through the corpus day, after EACH write: every trend point (``total_value``,
``net_invested``, ``incomplete``, ``net_worth``) dated before the ``dirty_from`` the triggers
recorded is unchanged, and ``dirty_from`` is at or before the earliest point that changed. A
write that changes nothing may record nothing.

The property is checked on TWO engines: the FROZEN v0.1.31 engine and trend
(``tests/reference/cost_basis_v0131.py``, ``trend_v0131.py``) over the inputs ``build_dashboard``
assembles (``dashboard_inputs.py``), and — since the wave-1 merge — the SHIPPING engine, the
trend of an uncached ``dashboard._build_dashboard`` (``iter_books`` + ``value_series``), so the
property the store relies on is pinned on the code that serves it. A write after which either
series is unavailable (the series-level ``available`` is evaluated live on every build, §4) is
counted, not compared.

Every operation kind runs once per corpus, in a seeded order, so every trigger table sees an
insert, an update and a delete. ``PD_PREFIX_SWEEP=<n>`` adds *n* further seeded random
operations per corpus (a heavier local sweep; the default suite runs none).
"""

from __future__ import annotations

import os
import random
import sqlite3
from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest

from portfolio_dash.portfolio import dashboard
from portfolio_dash.portfolio import settlement_store as st
from tests.reference import trend_v0131 as ref
from tests.reference.canon import canon
from tests.reference.corpora import CorpusDB, open_db
from tests.reference.dashboard_inputs import load_inputs
from tests.settlement.conftest import CorpusFactory

Points = dict[date, Any]
SWEEP = int(os.environ.get("PD_PREFIX_SWEEP", "0"))


def _points(conn: sqlite3.Connection, db: CorpusDB) -> Points | None:
    """The reference trend + net worth by day, or ``None`` when it is unavailable."""
    i = load_inputs(conn, db.now, db.reporting)
    if not i.has_events:
        return None
    try:
        trend = ref.ref_daily_value_series(
            i.bundle, i.price_history, i.trend_fx_history, i.reporting, end=i.as_of,
            cash_movements=i.cash_movements)
        if not trend.available:
            return None
        cash = ref.ref_daily_cash_series(
            i.cash_movements, i.cash_convs, i.cash_txs, i.cash_divs, i.instruments,
            i.cash_fx_history, i.reporting, end=i.as_of)
    except ValueError:  # an unbookable ledger: the dashboard degrades the same way, live
        return None
    series = ref.ref_compose_net_worth(trend, cash)
    return {p.date: canon((p.total_value, p.net_invested, p.incomplete, p.net_worth))
            for p in series.points}


def _points_live(conn: sqlite3.Connection, db: CorpusDB) -> Points | None:
    """The shipping engine's trend + net worth by day (an uncached build), or ``None``."""
    trend = dashboard._build_dashboard(conn, now=db.now, reporting=db.reporting).trend
    if not trend.available:
        return None
    return {p.date: canon((p.total_value, p.net_invested, p.incomplete, p.net_worth))
            for p in trend.points}


_ENGINES: dict[str, Callable[[sqlite3.Connection, CorpusDB], Points | None]] = {
    "v0131": _points,
    "live": _points_live,
}


def _changed(before: Points, after: Points) -> list[date]:
    return sorted(d for d in set(before) | set(after) if before.get(d) != after.get(d))


class _Ops:
    """The operation catalogue over one corpus connection (raw SQL: the triggers fire for any
    writer, which ``test_triggers.py`` checks writer by writer)."""

    def __init__(self, conn: sqlite3.Connection, rng: random.Random, first: date,
                 today: date) -> None:
        self.conn, self.rng, self.first, self.today = conn, rng, first, today
        self.prepared: dict[str, str] = {}
        self.serial = 0

    def fresh(self, prefix: str) -> str:
        self.serial += 1
        return f"{prefix}{self.serial}"

    # --- helpers ---
    def registered_with_a_buy(self, prefix: str) -> str:
        """A fresh registered stock with one buy and one price (a ``prep_*`` step: it moves
        the series, so the loop re-baselines after it)."""
        symbol = self.fresh(prefix)
        d = max(self.day(), self.first + timedelta(days=10))
        self.x("INSERT INTO instruments (symbol, market, quote_ccy, sector, name) "
               "VALUES (?, 'TW', 'TWD', 'Energy', 'prefix')", (symbol,))
        self.x("INSERT INTO transactions (account_id, symbol, side, quantity, price, fees, tax, "
               "trade_date) VALUES ('tw_broker', ?, 'BUY', '10', '20', '0', '0', ?)",
               (symbol, d.isoformat()))
        self.x("INSERT INTO prices (instrument, market, as_of_date, close, source, fetched_at) "
               "VALUES (?, 'TW', ?, '25', 'test', '2026-01-01T00:00:00')",
               (symbol, d.isoformat()))
        return symbol

    def registered_target(self, prefix: str) -> str:
        """A registered stock with a price on every day and no ledger row (an action target)."""
        symbol = self.fresh(prefix)
        self.x("INSERT INTO instruments (symbol, market, quote_ccy, sector, name) "
               "VALUES (?, 'TW', 'TWD', 'Energy', 'prefix')", (symbol,))
        self.x("INSERT INTO prices (instrument, market, as_of_date, close, source, fetched_at) "
               "VALUES (?, 'TW', ?, '7', 'test', '2026-01-01T00:00:00')",
               (symbol, self.first.isoformat()))
        return symbol
    def day(self) -> date:
        return self.first + timedelta(days=self.rng.randrange((self.today - self.first).days))

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        rows = self.conn.execute(sql, params).fetchall()
        return self.rng.choice(rows) if rows else None

    def x(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.conn.execute(sql, params)
        self.conn.commit()

    def held(self) -> sqlite3.Row:
        row = self.one("SELECT account_id, symbol, MIN(trade_date) AS d FROM transactions "
                       "WHERE symbol IN (SELECT symbol FROM instruments WHERE asset_type = "
                       "'stock') AND side = 'BUY' GROUP BY account_id, symbol")
        assert row is not None
        return row

    # --- transactions ---
    def tx_insert_backdated(self) -> str:
        h = self.held()
        d = max(self.day(), date.fromisoformat(h["d"]))
        self.x("INSERT INTO transactions (account_id, symbol, side, quantity, price, fees, tax, "
               "trade_date) VALUES (?, ?, 'BUY', '3', '101', '1', '0', ?)",
               (h["account_id"], h["symbol"], d.isoformat()))
        return f"buy {h['symbol']} {d}"

    def tx_insert_today(self) -> str:
        h = self.held()
        self.x("INSERT INTO transactions (account_id, symbol, side, quantity, price, fees, tax, "
               "trade_date) VALUES (?, ?, 'BUY', '2', '99', '0', '0', ?)",
               (h["account_id"], h["symbol"], self.today.isoformat()))
        return f"buy {h['symbol']} today"

    def tx_update_quantity(self) -> str:
        row = self.one("SELECT id, quantity FROM transactions")
        assert row is not None
        self.x("UPDATE transactions SET quantity = ? WHERE id = ?",
               (str(Decimal(row["quantity"]) + 1), row["id"]))
        return f"tx {row['id']} quantity"

    def tx_move_date(self) -> str:
        row = self.one("SELECT id FROM transactions")
        assert row is not None
        d = self.day()
        self.x("UPDATE transactions SET trade_date = ? WHERE id = ?", (d.isoformat(), row["id"]))
        return f"tx {row['id']} -> {d}"

    def tx_delete(self) -> str:
        row = self.one("SELECT id FROM transactions WHERE side = 'SELL'") or self.one(
            "SELECT id FROM transactions")
        assert row is not None
        self.x("DELETE FROM transactions WHERE id = ?", (row["id"],))
        return f"tx {row['id']} deleted"

    def tx_note(self) -> str:
        row = self.one("SELECT id FROM transactions")
        assert row is not None
        self.x("UPDATE transactions SET note = 'prefix', fee_rule_snapshot = '{}' WHERE id = ?",
               (row["id"],))
        return "tx note"

    # --- dividends ---
    def div_insert(self) -> str:
        h = self.held()
        d = max(self.day(), date.fromisoformat(h["d"]))
        self.x("INSERT INTO dividends (account_id, symbol, date, type, gross, withholding, net) "
               "VALUES (?, ?, ?, 'CASH', '40', '0', '40')", (h["account_id"], h["symbol"],
                                                              d.isoformat()))
        return f"cash dividend {h['symbol']} {d}"

    def div_stock_with_ex_date(self) -> str:
        h = self.held()
        d = max(self.day(), date.fromisoformat(h["d"]) + timedelta(days=1))
        pay = min(d + timedelta(days=30), self.today)
        self.x("INSERT INTO dividends (account_id, symbol, date, type, gross, withholding, net, "
               "reinvest_shares, ex_date) VALUES (?, ?, ?, 'STOCK', '0', '0', '0', '5', ?)",
               (h["account_id"], h["symbol"], pay.isoformat(), d.isoformat()))
        return f"stock dividend {h['symbol']} ex {d}"

    def div_update(self) -> str:
        row = self.one("SELECT id, net FROM dividends")
        if row is None:
            return self.div_insert()
        self.x("UPDATE dividends SET net = ?, gross = ? WHERE id = ?",
               (str(Decimal(row["net"] or "0") + 7), str(Decimal(row["net"] or "0") + 7),
                row["id"]))
        return f"dividend {row['id']} amount"

    def div_delete(self) -> str:
        row = self.one("SELECT id FROM dividends")
        if row is None:
            return self.div_insert()
        self.x("DELETE FROM dividends WHERE id = ?", (row["id"],))
        return f"dividend {row['id']} deleted"

    def div_move_date(self) -> str:
        row = self.one("SELECT id FROM dividends WHERE type <> 'STOCK'")
        if row is None:
            return self.div_insert()
        d = self.day()
        self.x("UPDATE dividends SET date = ? WHERE id = ?", (d.isoformat(), row["id"]))
        return f"dividend {row['id']} -> {d}"

    # --- openings ---
    def open_upsert(self) -> str:
        sym = self.one("SELECT symbol FROM instruments WHERE asset_type = 'stock' "
                       "AND quote_ccy = 'TWD'")
        assert sym is not None
        d = self.day()
        self.x("INSERT INTO opening_inventory (account_id, symbol, shares, original_cost_total, "
               "build_date) VALUES ('tw_broker', ?, '100', '5000', ?) ON CONFLICT (account_id, "
               "symbol) DO UPDATE SET shares = '120', build_date = excluded.build_date",
               (sym["symbol"], d.isoformat()))
        return f"opening {sym['symbol']} {d}"

    def open_update(self) -> str:
        row = self.one("SELECT account_id, symbol, shares FROM opening_inventory")
        if row is None:
            return self.open_upsert()
        self.x("UPDATE opening_inventory SET shares = ? WHERE account_id = ? AND symbol = ?",
               (str(Decimal(row["shares"]) + 10), row["account_id"], row["symbol"]))
        return f"opening {row['symbol']} shares"

    def open_delete(self) -> str:
        row = self.one("SELECT account_id, symbol FROM opening_inventory")
        if row is None:
            return self.open_upsert()
        self.x("DELETE FROM opening_inventory WHERE account_id = ? AND symbol = ?",
               (row["account_id"], row["symbol"]))
        return f"opening {row['symbol']} deleted"

    # --- cash movements ---
    def cash_insert(self) -> str:
        d = self.day()
        kind = self.rng.choice(["DEPOSIT", "REBATE", "BROKER_FEE", "WITHDRAW"])
        self.x("INSERT INTO cash_movements (account_id, date, kind, ccy, amount) "
               "VALUES ('tw_broker', ?, ?, 'TWD', '1500')", (d.isoformat(), kind))
        return f"cash {kind} {d}"

    def cash_update(self) -> str:
        row = self.one("SELECT id, amount FROM cash_movements")
        if row is None:
            return self.cash_insert()
        self.x("UPDATE cash_movements SET amount = ? WHERE id = ?",
               (str(Decimal(row["amount"]) + 11), row["id"]))
        return f"cash {row['id']} amount"

    def cash_delete(self) -> str:
        row = self.one("SELECT id FROM cash_movements")
        if row is None:
            return self.cash_insert()
        self.x("DELETE FROM cash_movements WHERE id = ?", (row["id"],))
        return f"cash {row['id']} deleted"

    def cash_move_date(self) -> str:
        row = self.one("SELECT id FROM cash_movements")
        if row is None:
            return self.cash_insert()
        d = self.day()
        self.x("UPDATE cash_movements SET date = ? WHERE id = ?", (d.isoformat(), row["id"]))
        return f"cash {row['id']} -> {d}"

    # --- FX conversions ---
    def fxc_insert(self) -> str:
        d = self.day()
        self.x("INSERT INTO fx_conversions (account_id, date, from_ccy, from_amount, to_ccy, "
               "to_amount) VALUES ('schwab', ?, 'TWD', '3200', 'USD', '100')", (d.isoformat(),))
        return f"fx conversion {d}"

    def fxc_update(self) -> str:
        row = self.one("SELECT id, to_amount FROM fx_conversions")
        if row is None:
            return self.fxc_insert()
        self.x("UPDATE fx_conversions SET to_amount = ? WHERE id = ?",
               (str(Decimal(row["to_amount"]) + 5), row["id"]))
        return f"fx conversion {row['id']} amount"

    def fxc_delete(self) -> str:
        row = self.one("SELECT id FROM fx_conversions")
        if row is None:
            return self.fxc_insert()
        self.x("DELETE FROM fx_conversions WHERE id = ?", (row["id"],))
        return f"fx conversion {row['id']} deleted"

    def fxc_move_date(self) -> str:
        row = self.one("SELECT id FROM fx_conversions")
        if row is None:
            return self.fxc_insert()
        d = self.day()
        self.x("UPDATE fx_conversions SET date = ? WHERE id = ?", (d.isoformat(), row["id"]))
        return f"fx conversion {row['id']} -> {d}"

    # --- corporate actions ---
    def ca_split(self) -> str:
        h = self.held()
        d = max(self.day(), date.fromisoformat(h["d"]) + timedelta(days=1))
        self.x("INSERT INTO corporate_actions (account_id, date, kind, from_symbol, to_symbol, "
               "ratio_to, ratio_from) VALUES (?, ?, 'SPLIT', ?, ?, '2', '1')",
               (h["account_id"], d.isoformat(), h["symbol"], h["symbol"]))
        return f"split {h['symbol']} {d}"

    def ca_update(self) -> str:
        row = self.one("SELECT id FROM corporate_actions WHERE kind = 'SPLIT'")
        if row is None:
            return self.ca_split()
        self.x("UPDATE corporate_actions SET ratio_to = '3' WHERE id = ?", (row["id"],))
        return f"action {row['id']} ratio"

    def ca_delete(self) -> str:
        row = self.one("SELECT id FROM corporate_actions")
        if row is None:
            return self.ca_split()
        self.x("DELETE FROM corporate_actions WHERE id = ?", (row["id"],))
        return f"action {row['id']} deleted"

    def ca_move_date(self) -> str:
        row = self.one("SELECT id FROM corporate_actions")
        if row is None:
            return self.ca_split()
        d = self.day()
        self.x("UPDATE corporate_actions SET date = ? WHERE id = ?", (d.isoformat(), row["id"]))
        return f"action {row['id']} -> {d}"

    def prep_ca_exchange(self) -> None:
        self.prepared["ca_exchange"] = self.registered_target("XCH")

    def ca_exchange(self) -> str:
        h = self.held()
        to = self.prepared.pop("ca_exchange", None) or self.registered_target("XCH")
        d = max(self.day(), date.fromisoformat(h["d"]) + timedelta(days=1))
        self.x("INSERT INTO corporate_actions (account_id, date, kind, from_symbol, to_symbol, "
               "ratio_to, ratio_from) VALUES (?, ?, 'EXCHANGE', ?, ?, '1', '1')",
               (h["account_id"], d.isoformat(), h["symbol"], to))
        return f"exchange {h['symbol']} -> {to} {d}"

    def prep_ca_spinoff(self) -> None:
        self.prepared["ca_spinoff"] = self.registered_target("SPN")

    def ca_spinoff(self) -> str:
        h = self.held()
        child = self.prepared.pop("ca_spinoff", None) or self.registered_target("SPN")
        d = max(self.day(), date.fromisoformat(h["d"]) + timedelta(days=1))
        self.x("INSERT INTO corporate_actions (account_id, date, kind, from_symbol, to_symbol, "
               "ratio_to, ratio_from, cost_carry) VALUES (?, ?, 'SPINOFF', ?, ?, '1', '10', "
               "'0.1')", (h["account_id"], d.isoformat(), h["symbol"], child))
        return f"spinoff {h['symbol']} -> {child} {d}"

    # --- prices ---
    def _ledger_price(self, *, keep_first: bool = False) -> sqlite3.Row | None:
        """A stored price of a ledger symbol; *keep_first* skips each series' earliest row, so
        a delete or a move never leaves a held symbol without any price."""
        first = (" AND as_of_date > (SELECT MIN(as_of_date) FROM prices q "
                 "WHERE q.instrument = prices.instrument)") if keep_first else ""
        return self.one("SELECT instrument, as_of_date, close FROM prices WHERE instrument IN "
                        f"(SELECT symbol FROM transactions) AND as_of_date <= ?{first}",
                        (self.today.isoformat(),))

    def px_restate(self) -> str:
        row = self._ledger_price()
        assert row is not None
        self.x("UPDATE prices SET close = ? WHERE instrument = ? AND as_of_date = ?",
               (str(Decimal(row["close"]) * Decimal("1.1")), row["instrument"],
                row["as_of_date"]))
        return f"price {row['instrument']} {row['as_of_date']} restated"

    def px_insert(self) -> str:
        h = self.held()
        d = self.day()
        self.x("INSERT INTO prices (instrument, market, as_of_date, close, source, fetched_at) "
               "SELECT ?, market, ?, '123.45', 'test', '2026-01-01T00:00:00' FROM instruments "
               "WHERE symbol = ? ON CONFLICT (instrument, as_of_date) DO UPDATE SET "
               "close = '123.45'", (h["symbol"], d.isoformat(), h["symbol"]))
        return f"price {h['symbol']} {d} inserted"

    def px_delete(self) -> str:
        row = self._ledger_price(keep_first=True)
        if row is None:
            return self.px_insert()
        self.x("DELETE FROM prices WHERE instrument = ? AND as_of_date = ?",
               (row["instrument"], row["as_of_date"]))
        return f"price {row['instrument']} {row['as_of_date']} deleted"

    def px_move(self) -> str:
        row = self._ledger_price(keep_first=True)
        if row is None:
            return self.px_insert()
        d = self.day()
        self.x("UPDATE OR REPLACE prices SET as_of_date = ? WHERE instrument = ? "
               "AND as_of_date = ?", (d.isoformat(), row["instrument"], row["as_of_date"]))
        return f"price {row['instrument']} {row['as_of_date']} -> {d}"

    def px_benchmark_or_stranger(self) -> str:
        d = self.day()
        self.x("INSERT OR REPLACE INTO prices (instrument, market, as_of_date, close, source, "
               "fetched_at) VALUES ('NOTHELD', 'TW', ?, '9', 'test', '2026-01-01T00:00:00')",
               (d.isoformat(),))
        return "price of a symbol in no ledger"

    # --- FX rates ---
    def fx_restate(self) -> str:
        row = self.one("SELECT base, quote, as_of_date, rate FROM fx_rates WHERE as_of_date <= ?",
                       (self.today.isoformat(),))
        assert row is not None
        self.x("UPDATE fx_rates SET rate = ? WHERE base = ? AND quote = ? AND as_of_date = ?",
               (str(Decimal(row["rate"]) * Decimal("1.02")), row["base"], row["quote"],
                row["as_of_date"]))
        return f"fx {row['base']}/{row['quote']} {row['as_of_date']} restated"

    def fx_insert(self) -> str:
        row = self.one("SELECT base, quote, rate FROM fx_rates")
        assert row is not None
        d = self.day()
        self.x("INSERT OR REPLACE INTO fx_rates (base, quote, as_of_date, rate, source, "
               "fetched_at) VALUES (?, ?, ?, ?, 'test', '2026-01-01T00:00:00')",
               (row["base"], row["quote"], d.isoformat(), str(Decimal(row["rate"]) + 1)))
        return f"fx {row['base']}/{row['quote']} {d} inserted"

    def fx_delete(self) -> str:
        row = self.one("SELECT base, quote, as_of_date FROM fx_rates r WHERE as_of_date > "
                       "(SELECT MIN(as_of_date) FROM fx_rates q WHERE q.base = r.base "
                       "AND q.quote = r.quote)")
        if row is None:
            return self.fx_insert()
        self.x("DELETE FROM fx_rates WHERE base = ? AND quote = ? AND as_of_date = ?",
               (row["base"], row["quote"], row["as_of_date"]))
        return f"fx {row['base']}/{row['quote']} {row['as_of_date']} deleted"

    # --- instruments and accounts ---
    def inst_unregistered_trade(self) -> str:
        d = self.day()
        self.x("INSERT INTO transactions (account_id, symbol, side, quantity, price, fees, tax, "
               "trade_date) VALUES ('tw_broker', 'ZZPF', 'BUY', '10', '20', '0', '0', ?)",
               (d.isoformat(),))
        if not self.conn.execute("SELECT 1 FROM prices WHERE instrument = 'ZZPF'").fetchone():
            self.x("INSERT INTO prices (instrument, market, as_of_date, close, source, "
                   "fetched_at) VALUES ('ZZPF', 'TW', ?, '25', 'test', '2026-01-01T00:00:00')",
                   (self.first.isoformat(),))
        return f"unregistered buy {d}"

    def prep_inst_register(self) -> None:
        """A fresh unregistered symbol with a trade and a price (excluded from the replay
        until it is registered)."""
        symbol = self.fresh("ZZR")
        self.x("INSERT INTO transactions (account_id, symbol, side, quantity, price, fees, tax, "
               "trade_date) VALUES ('tw_broker', ?, 'BUY', '10', '20', '0', '0', ?)",
               (symbol, self.day().isoformat()))
        self.x("INSERT INTO prices (instrument, market, as_of_date, close, source, fetched_at) "
               "VALUES (?, 'TW', ?, '25', 'test', '2026-01-01T00:00:00')",
               (symbol, self.first.isoformat()))
        self.prepared["inst_register"] = symbol

    def inst_register(self) -> str:
        symbol = self.prepared.pop("inst_register")
        self.x("INSERT INTO instruments (symbol, market, quote_ccy, sector, name) "
               "VALUES (?, 'TW', 'TWD', 'Energy', 'prefix')", (symbol,))
        return f"register {symbol}"

    def inst_display(self) -> str:
        self.x("UPDATE instruments SET name = name || '*', sector = 'Energy', archived = 1")
        return "instrument display fields"

    def prep_inst_delete(self) -> None:
        self.prepared["inst_delete"] = self.registered_with_a_buy("ZZD")

    def inst_delete(self) -> str:
        symbol = self.prepared.pop("inst_delete")
        self.x("DELETE FROM instruments WHERE symbol = ?", (symbol,))
        return f"instrument {symbol} deleted"

    def prep_inst_quote_ccy(self) -> None:
        self.prepared["inst_quote_ccy"] = self.registered_with_a_buy("ZZC")

    def inst_quote_ccy(self) -> str:
        symbol = self.prepared.pop("inst_quote_ccy")
        self.x("UPDATE instruments SET quote_ccy = 'USD', market = 'US' WHERE symbol = ?",
               (symbol,))
        return f"instrument {symbol} quote currency"

    # --- accounts and their market rules: the trend reads neither (only the FX pool and the
    # dividend projection do), so these move no point — they pin that the triggers record them
    def acct_currency(self) -> str:
        self.x("UPDATE accounts SET funding_ccy = CASE funding_ccy WHEN 'TWD' THEN 'USD' "
               "ELSE 'TWD' END WHERE account_id = 'schwab'")
        return "account funding currency"

    def amr_dividend_model(self) -> str:
        self.x("UPDATE account_market_rules SET dividend_model = CASE dividend_model "
               "WHEN 'cash' THEN 'drip_us' ELSE 'cash' END WHERE account_id = 'schwab'")
        return "account market rule dividend model"

    def amr_fee_rule(self) -> str:
        self.x("UPDATE account_market_rules SET fee_rule_set = fee_rule_set || 'x' "
               "WHERE account_id = 'tw_broker'")
        return "account market rule fee set"


_HELPERS = frozenset({"day", "one", "x", "held", "fresh", "registered_with_a_buy",
                      "registered_target"})
_OPS: tuple[str, ...] = tuple(sorted(
    name for name, fn in vars(_Ops).items() if callable(fn) and not name.startswith("_")
    and not name.startswith("prep_") and name not in _HELPERS))

#: An operation's table, by its name's first word.
_TABLE_OF: dict[str, str] = {
    "tx": "transactions", "div": "dividends", "open": "opening_inventory",
    "cash": "cash_movements", "fxc": "fx_conversions", "ca": "corporate_actions",
    "px": "prices", "fx": "fx_rates", "inst": "instruments", "acct": "accounts",
    "amr": "account_market_rules",
}
#: The tables whose writes move trend points (the trend reads neither accounts nor their
#: market rules).
_POINT_TABLES = frozenset(_TABLE_OF.values()) - {"accounts", "account_market_rules"}


def _table(op: str) -> str:
    return _TABLE_OF[op.split("_", 1)[0]]


#: Every corpus on both engines — and ``unquoted_ccy_cash`` on the shipping engine only: its
#: MYR rebate is in a currency no instrument is quoted in, so the frozen v0.1.31 trend (the
#: quote currencies' pairs only, PERF-F2) has no series to compare, while the fixed trend
#: converts the rebate.
_CASES = [(name, engine) for name in ("golden", "demo", "doubled_demo", "rich",
                                      "rich_refusals", "epoch1990")
          for engine in sorted(_ENGINES)] + [("unquoted_ccy_cash", "live")]


@pytest.mark.parametrize(("name", "engine"), _CASES)
def test_dirty_from_bounds_every_reference_change(
    corpus_db: CorpusFactory, name: str, engine: str
) -> None:
    points = _ENGINES[engine]
    db = corpus_db(name)
    conn = open_db(db.path)
    try:
        today = db.now.date()
        first = st.first_ledger_day(conn)
        assert first is not None
        st.install_or_verify(conn, calc_version_changed=False)
        conn.execute("UPDATE settle_meta SET generation = 1, settled_through = ?, "
                     "held_through = ?, first_day = ? WHERE id = 1",
                     (today.isoformat(), today.isoformat(), first.isoformat()))
        conn.commit()
        rng = random.Random(f"prefix-property-{name}")
        ops = _Ops(conn, rng, first, today)
        schedule = list(_OPS)
        rng.shuffle(schedule)
        schedule += [rng.choice(_OPS) for _ in range(SWEEP)]
        before = points(conn, db)
        assert before is not None, f"{name}: the reference series must start available"
        compared = changed_ops = 0
        fired: set[str] = set()
        moved_tables: set[str] = set()
        for op in schedule:
            prep: Callable[[], None] | None = getattr(ops, f"prep_{op}", None)
            if prep is not None:  # a setup that moves the series: re-baseline after it
                prep()
                before = points(conn, db)
            conn.execute("UPDATE settle_dirty SET dirty_from = NULL WHERE id = 1")
            conn.commit()
            label: str = getattr(ops, op)()
            dirty_text = conn.execute("SELECT dirty_from FROM settle_dirty").fetchone()[0]
            dirty = None if dirty_text is None else date.fromisoformat(dirty_text)
            if dirty is not None:
                fired.add(_table(op))
            after = points(conn, db)
            if before is not None and after is not None:
                compared += 1
                moved = _changed(before, after)
                if moved:
                    changed_ops += 1
                    moved_tables.add(_table(op))
                    assert dirty is not None, f"{name} {op} ({label}): {moved[0]} changed, " \
                                              "nothing recorded"
                    assert dirty <= moved[0], (
                        f"{name} {op} ({label}): dirty_from {dirty} but {moved[0]} changed")
            before = after  # an unavailable series re-baselines: never compare across it
        assert compared >= len(schedule) // 2, f"{name}: only {compared} writes were comparable"
        assert changed_ops >= len(schedule) // 3, f"{name}: the writes changed too little"
        assert fired == set(st.TRIGGER_TABLES), (
            f"{name}: no write recorded a change on {sorted(set(st.TRIGGER_TABLES) - fired)}")
        assert _POINT_TABLES <= moved_tables, (
            f"{name}: no write moved a point through {sorted(_POINT_TABLES - moved_tables)}")
    finally:
        conn.close()


def test_the_catalogue_covers_every_trigger_table() -> None:
    assert set(_TABLE_OF.values()) == set(st.TRIGGER_TABLES)
    for table in st.TRIGGER_TABLES:
        assert any(_table(o) == table for o in _OPS), table
    kinds = {
        "transactions": ("insert", "update", "move_date", "delete"),
        "dividends": ("insert", "update", "move_date", "delete"),
        "cash_movements": ("insert", "update", "move_date", "delete"),
        "fx_conversions": ("insert", "update", "move_date", "delete"),
        "corporate_actions": ("split", "exchange", "spinoff", "update", "move_date", "delete"),
        "instruments": ("register", "quote_ccy", "delete"),
    }
    for table, wanted in kinds.items():
        names = [o for o in _OPS if _table(o) == table]
        for kind in wanted:
            assert any(kind in o for o in names), f"{table}: no {kind} operation"
    for op in _OPS:
        _table(op)  # every operation names its table
