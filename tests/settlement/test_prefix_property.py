"""The prefix property, checked through the triggers (spec 2026-10-18 §4, §18 "Prefix property";
plan W1-B tests).

For a fixed-seed sequence of ledger, reference, price and FX writes on a corpus whose store is
settled through the corpus day, after EACH write: every trend point (``total_value``,
``net_invested``, ``incomplete``, ``net_worth``) dated before the ``dirty_from`` the triggers
recorded is unchanged, and ``dirty_from`` is at or before the earliest point that changed. A
write that changes nothing may record nothing.

The oracle is the FROZEN v0.1.31 engine and trend (``tests/reference/cost_basis_v0131.py``,
``trend_v0131.py``) over the inputs ``build_dashboard`` assembles (``dashboard_inputs.py``) —
never the new engine (package A builds it in parallel; the main session reruns this file on
A's engine at the wave-1 merge). A write after which either series is unavailable (the
series-level ``available`` is evaluated live on every build, §4) is counted, not compared.

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


def _changed(before: Points, after: Points) -> list[date]:
    return sorted(d for d in set(before) | set(after) if before.get(d) != after.get(d))


class _Ops:
    """The operation catalogue over one corpus connection (raw SQL: the triggers fire for any
    writer, which ``test_triggers.py`` checks writer by writer)."""

    def __init__(self, conn: sqlite3.Connection, rng: random.Random, first: date,
                 today: date) -> None:
        self.conn, self.rng, self.first, self.today = conn, rng, first, today

    # --- helpers ---
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

    def inst_register(self) -> str:
        if not self.conn.execute("SELECT 1 FROM transactions WHERE symbol = 'ZZPF'").fetchone():
            self.inst_unregistered_trade()
            self.conn.execute("UPDATE settle_dirty SET dirty_from = NULL")
            self.conn.commit()
        self.x("INSERT OR IGNORE INTO instruments (symbol, market, quote_ccy, sector, name) "
               "VALUES ('ZZPF', 'TW', 'TWD', 'Energy', 'prefix')")
        return "register ZZPF"

    def inst_display(self) -> str:
        self.x("UPDATE instruments SET name = name || '*', sector = 'Energy', archived = 1")
        return "instrument display fields"

    def acct_rules(self) -> str:
        self.x("UPDATE account_market_rules SET fee_rule_set = fee_rule_set || 'x' "
               "WHERE account_id = 'tw_broker'")
        return "account market rule"


_OPS: tuple[str, ...] = tuple(sorted(
    name for name, fn in vars(_Ops).items() if callable(fn) and not name.startswith("_")
    and name not in {"day", "one", "x", "held"}))


@pytest.mark.parametrize("name", ["golden", "demo", "doubled_demo"])
def test_dirty_from_bounds_every_reference_change(corpus_db: CorpusFactory, name: str) -> None:
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
        before = _points(conn, db)
        assert before is not None, f"{name}: the reference series must start available"
        compared = changed_ops = 0
        for op in schedule:
            conn.execute("UPDATE settle_dirty SET dirty_from = NULL WHERE id = 1")
            conn.commit()
            label: str = getattr(ops, op)()
            dirty_text = conn.execute("SELECT dirty_from FROM settle_dirty").fetchone()[0]
            dirty = None if dirty_text is None else date.fromisoformat(dirty_text)
            after = _points(conn, db)
            if before is not None and after is not None:
                compared += 1
                moved = _changed(before, after)
                if moved:
                    changed_ops += 1
                    assert dirty is not None, f"{name} {op} ({label}): {moved[0]} changed, " \
                                              "nothing recorded"
                    assert dirty <= moved[0], (
                        f"{name} {op} ({label}): dirty_from {dirty} but {moved[0]} changed")
            before = after  # an unavailable series re-baselines: never compare across it
        assert compared >= len(schedule) // 2, f"{name}: only {compared} writes were comparable"
        assert changed_ops >= len(schedule) // 3, f"{name}: the writes changed too little"
    finally:
        conn.close()


def test_the_catalogue_covers_every_trigger_table() -> None:
    prefixes: dict[str, Callable[[str], bool]] = {
        "transactions": lambda o: o.startswith("tx_"),
        "dividends": lambda o: o.startswith("div_"),
        "opening_inventory": lambda o: o.startswith("open_"),
        "cash_movements": lambda o: o.startswith("cash_"),
        "fx_conversions": lambda o: o.startswith("fxc_"),
        "corporate_actions": lambda o: o.startswith("ca_"),
        "prices": lambda o: o.startswith("px_"),
        "fx_rates": lambda o: o.startswith("fx_"),
        "instruments": lambda o: o.startswith("inst_"),
        "accounts": lambda o: o.startswith("acct_"),
    }
    assert set(st.TRIGGER_TABLES) - {"account_market_rules"} == set(prefixes)
    for table, match in prefixes.items():
        assert any(match(o) for o in _OPS), table
