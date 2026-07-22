"""Instruments and transactions persistence helpers (data ingestion store)."""

import json
import logging
import sqlite3
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, Field

from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Account, Instrument, MarketRule
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.money import from_db, to_db
from portfolio_dash.shared.sectors import CANONICAL_KEYS, canonical_sector

logger = logging.getLogger(__name__)

# Transaction price precision cap (audit L11): cap to 4 dp on the way in — same
# convention as pricing/store (ROUND_HALF_UP, CAP-not-pad; clean values are unchanged).
# 4 dp covers every market tick (US/TW 2 dp, MY 3 dp).
_PRICE_DP = 4


def _cap_price(v: Decimal) -> Decimal:
    """Round *v* to at most 4 decimals; values already within the cap are returned as-is."""
    exp = v.as_tuple().exponent
    if isinstance(exp, int) and exp < -_PRICE_DP:
        return v.quantize(Decimal(1).scaleb(-_PRICE_DP), rounding=ROUND_HALF_UP)
    return v


# ---------------------------------------------------------------------------
# Ledger correction audit trail (audit M9)
# ---------------------------------------------------------------------------
# Every row correction (edit / delete) on the four ledgers captures the BEFORE state
# here, so an explicit correction is auditable even though the ledger is not literally
# append-only. No UI viewer this wave — db-stats visibility is enough.


def _write_audit(
    conn: sqlite3.Connection,
    table_name: str,
    row_id: str,
    action: str,
    before: dict[str, object] | None,
) -> None:
    """Record the pre-mutation snapshot of one ledger row (idempotent-agnostic append)."""
    if before is None:
        return
    conn.execute(
        "INSERT INTO ledger_audit (table_name, row_id, action, before_json, at) "
        "VALUES (?,?,?,?,?)",
        (
            table_name,
            row_id,
            action,
            json.dumps(before, ensure_ascii=False, default=str),
            datetime.now(UTC).isoformat(),
        ),
    )


def _capture(
    conn: sqlite3.Connection, sql: str, params: tuple[object, ...]
) -> dict[str, object] | None:
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row is not None else None


def list_ledger_audit(
    conn: sqlite3.Connection, *, table_name: str | None = None
) -> list[dict[str, object]]:
    """Return ledger_audit rows (newest first); optionally filter by table_name."""
    where = " WHERE table_name=?" if table_name is not None else ""
    params: tuple[object, ...] = (table_name,) if table_name is not None else ()
    rows = conn.execute(
        f"SELECT id, table_name, row_id, action, before_json, at "
        f"FROM ledger_audit{where} ORDER BY id DESC",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def upsert_instrument(conn: sqlite3.Connection, inst: Instrument) -> None:
    """Insert or update an instrument row (idempotent). board_status is owned by
    register_instrument and intentionally not written here (preserved on conflict)."""
    conn.execute(
        """INSERT INTO instruments (symbol, market, quote_ccy, sector, name, board,
               target_low, target_high, is_etf, industry)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(symbol) DO UPDATE SET
               market=excluded.market, quote_ccy=excluded.quote_ccy,
               sector=excluded.sector, name=excluded.name, board=excluded.board,
               target_low=excluded.target_low, target_high=excluded.target_high,
               is_etf=excluded.is_etf, industry=excluded.industry""",
        (
            inst.symbol, inst.market.value, inst.quote_ccy.value,
            inst.sector, inst.name, inst.board,
            to_db(inst.target_low) if inst.target_low is not None else None,
            to_db(inst.target_high) if inst.target_high is not None else None,
            1 if inst.is_etf else 0,
            inst.industry,
        ),
    )
    conn.commit()


def _row_to_instrument(row: sqlite3.Row) -> Instrument:
    return Instrument(
        symbol=row["symbol"], market=Market(row["market"]),
        quote_ccy=Currency(row["quote_ccy"]), sector=row["sector"], name=row["name"],
        board=row["board"] or "",
        target_low=from_db(row["target_low"]) if row["target_low"] else None,
        target_high=from_db(row["target_high"]) if row["target_high"] else None,
        is_etf=bool(row["is_etf"]),
        archived=bool(row["archived"]),
        industry=row["industry"],
    )


def get_instrument(conn: sqlite3.Connection, symbol: str) -> Instrument | None:
    """Return a single instrument by exact symbol, or None if not found."""
    row = conn.execute(
        "SELECT symbol, market, quote_ccy, sector, name, board, target_low, target_high, "
        "is_etf, archived, industry FROM instruments WHERE symbol=?",
        (symbol,),
    ).fetchone()
    return _row_to_instrument(row) if row is not None else None


def list_instruments(conn: sqlite3.Connection) -> list[Instrument]:
    """Return all instruments in the database (archived included; callers that must
    exclude archived symbols — quote/signal/news fetch scopes — filter on ``.archived``,
    never here: the dashboard / portfolio / exports keep seeing EVERY registered symbol
    so no money figure is affected by archiving)."""
    rows = conn.execute(
        "SELECT symbol, market, quote_ccy, sector, name, board, target_low, target_high, "
        "is_etf, archived, industry FROM instruments"
    ).fetchall()
    return [_row_to_instrument(r) for r in rows]


def migrate_instrument_sectors(conn: sqlite3.Connection) -> int:
    """One-time idempotent rewrite of stored instrument sectors to the canonical GICS
    vocabulary (R6, owner sign-off 2026-07-19). Runs on EVERY boot at the schema seam
    (``schema.create_tables``); a no-op when every stored value is already canonical.

    For each instruments row with a NON-EMPTY stored sector, ``new = canonical_sector(sector)``;
    the row is UPDATEd only when ``new != stored`` AND ``new`` is a real canonical key — so a
    known synonym is folded (``Semiconductors`` → ``Information Technology``, ``Shipping`` →
    ``Industrials``, ``Healthcare`` → ``Health Care``) while an unrecognized value such as
    ``Electronics`` is left untouched (never silently rebucketed). Blank/NULL sectors are
    intentionally left as-is: they already group as ``Unclassified`` at read time, and keeping
    them NULL preserves the "not yet classified" signal the next wave's AI sector fill relies
    on (rewriting them to the literal ``"Unclassified"`` would destroy that signal). Returns the
    number of rows rewritten; logs a single summary line when > 0.

    Idempotent: a second run finds every value already canonical → 0 rewrites, no writes.
    """
    rows = conn.execute("SELECT symbol, sector FROM instruments").fetchall()
    migrated = 0
    for row in rows:
        stored = row["sector"]
        if stored is None or not stored.strip():
            continue  # blank/NULL stays NULL (see docstring) — not rebucketed to Unclassified
        new = canonical_sector(stored)
        if new != stored and new in CANONICAL_KEYS:
            conn.execute(
                "UPDATE instruments SET sector=? WHERE symbol=?", (new, row["symbol"])
            )
            migrated += 1
    if migrated:
        conn.commit()
        logger.info(
            "migrated %d instrument sector(s) to the canonical GICS vocabulary", migrated
        )
    return migrated


# ---------------------------------------------------------------------------
# Watchlist deletion / archive (FU-D13; API path superseded by FU-D18)
# ---------------------------------------------------------------------------
# The instruments registry IS the watchlist. Ledger tables reference ``symbol`` with no
# foreign keys, and the dashboard silently DROPS any ledger row whose symbol lacks an
# instruments row from ALL computation — so a HARD delete of a symbol with history would
# corrupt realized P&L / XIRR.
#
# FU-D18 (2026-07-17) makes watchlist deletion ACCUMULATIVE: the API DELETE now SOFT-deletes
# EVERY non-held symbol (never-traded included) by setting ``archived=1`` — no data is ever
# removed, and re-adding a symbol restores it and gap-backfills the missing price window. The
# hard-delete ``delete_instrument`` below is RETAINED for internal / test use but is NO LONGER
# routed (see its docstring). Only two API tiers remain:
#   * currently held -> DELETE refused (422 ``held``); a held symbol is never archived,
#   * everything else -> SOFT delete (archived=1), fully reversible.
# Invariant "held => not archived" is enforced at the single booking seam below
# (``insert_transaction`` / ``upsert_opening`` un-archive on any new booking).


def has_ledger_history(conn: sqlite3.Connection, symbol: str) -> bool:
    """Whether *symbol* appears in ANY permanent ledger (transactions / dividends /
    opening_inventory) — the guard that forbids a hard delete (ledger integrity / 重算)."""
    for sql in (
        "SELECT 1 FROM transactions WHERE symbol=? LIMIT 1",
        "SELECT 1 FROM dividends WHERE symbol=? LIMIT 1",
        "SELECT 1 FROM opening_inventory WHERE symbol=? LIMIT 1",
    ):
        if conn.execute(sql, (symbol,)).fetchone() is not None:
            return True
    return False


def set_instrument_archived(
    conn: sqlite3.Connection, symbol: str, archived: bool
) -> bool:
    """Set (or clear) the archived flag on one instrument. Returns False if unknown."""
    cur = conn.execute(
        "UPDATE instruments SET archived=? WHERE symbol=?",
        (1 if archived else 0, symbol),
    )
    conn.commit()
    return cur.rowcount > 0


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    """Whether a table exists — cleanup guards spare a fresh/partial DB (some derived
    tables are created lazily by their own module's bootstrap)."""
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _like_escape(value: str) -> str:
    r"""Escape LIKE metacharacters (``\`` ``%`` ``_``) for use with ``ESCAPE '\'``."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def delete_instrument(
    conn: sqlite3.Connection, symbol: str, *, preserve_market_data: bool = False
) -> bool:
    """Hard-delete a NEVER-TRADED watch-only symbol and its derived / cache rows, in ONE
    transaction; audit the instruments row first (mirrors ``delete_opening``).

    NOTE (FU-D18, 2026-07-17): the plain DELETE path is SOFT-delete-only
    (``set_instrument_archived(True)``). This hard delete is routed ONLY by the FU-D32
    「永久移除」 purge endpoint (``POST /api/instruments/{symbol}/purge``), which guarantees
    the symbol is neither held nor carries ledger history before invoking it; it also remains
    available for internal / test use (rebuild tooling, fixtures).

    Cleaned in addition to the instruments row: ``prices``, ``dividend_events`` (both keyed
    ``instrument``), ``signal_states`` / ``alert_events`` (keyed ``symbol``), any
    ``pending_dividend_skips`` fingerprint for the symbol, and the symbol's entry in the
    single-row ``target_weights_config`` JSON map. Raw SQL by table name keeps the cleanup
    atomic without importing upward (data_ingestion imports no higher layer).

    ``preserve_market_data`` (FU-D32 benchmark guard): when True, the market-data rows
    (``prices`` / ``dividend_events``, keyed ``instrument``) are KEPT so a benchmark daily
    series stored under this same ``prices.instrument`` key (``pricing/benchmarks.py`` — e.g.
    ``"0050"``) survives the purge. The registry row AND every personal artifact
    (``signal_states`` / ``alert_events`` / ``pending_dividend_skips`` / the target-weights
    entry) are still removed. The purge route sets this for a symbol that is also a benchmark
    storage key."""
    _write_audit(
        conn, "instruments", symbol, "delete",
        _capture(conn, "SELECT * FROM instruments WHERE symbol=?", (symbol,)),
    )
    # Market-data tables are keyed ``instrument`` and shared with benchmark series; personal
    # artifacts are keyed ``symbol`` and always cleaned. preserve_market_data keeps only the
    # former (so a benchmark's daily-close series stored under the same key is not orphaned).
    market_data = (("prices", "instrument"), ("dividend_events", "instrument"))
    personal = (("signal_states", "symbol"), ("alert_events", "symbol"))
    tables = personal if preserve_market_data else (*market_data, *personal)
    for table, col in tables:
        if _table_exists(conn, table):
            conn.execute(f"DELETE FROM {table} WHERE {col}=?", (symbol,))  # noqa: S608 (fixed table/col literals)
    if _table_exists(conn, "pending_dividend_skips"):
        # fingerprint form: ``div:{account}:{symbol}:{ex_date}[:stock]`` (symbol = 3rd part).
        conn.execute(
            r"DELETE FROM pending_dividend_skips WHERE fingerprint LIKE ? ESCAPE '\'",
            (f"div:%:{_like_escape(symbol)}:%",),
        )
    if _table_exists(conn, "target_weights_config"):
        row = conn.execute(
            "SELECT weights_json FROM target_weights_config WHERE id=1"
        ).fetchone()
        if row is not None and row[0]:
            weights = json.loads(row[0])
            if symbol in weights:
                del weights[symbol]
                conn.execute(
                    "UPDATE target_weights_config SET weights_json=? WHERE id=1",
                    (json.dumps(weights),),
                )
    cur = conn.execute("DELETE FROM instruments WHERE symbol=?", (symbol,))
    conn.commit()
    return cur.rowcount > 0


def _unarchive_on_booking(conn: sqlite3.Connection, symbol: str) -> None:
    """Un-archive *symbol* on ANY new booking — the single write seam upholding the
    "held => not archived" invariant across the manual, CSV, and opening-edit paths (they
    all funnel through ``insert_transaction`` / ``upsert_opening``). No commit here: it
    joins the caller's transaction (batch-import atomicity is preserved)."""
    conn.execute(
        "UPDATE instruments SET archived=0 WHERE symbol=? AND archived=1", (symbol,)
    )


def list_accounts(conn: sqlite3.Connection) -> list[Account]:
    """Return all broker accounts (seeded by ``config_seed.seed_accounts``).

    Each ``Account`` carries its ``account_market_rules`` bindings in ``market_rules``
    (keyed by market value) so the pure compute layer can read per-market rules without a
    ``conn``. Absent bindings -> empty dict; the scalar fields remain the fallback.
    """
    rows = conn.execute(
        "SELECT account_id, name, broker, settlement_ccy, funding_ccy, dividend_model "
        "FROM accounts ORDER BY account_id"
    ).fetchall()
    by_account: dict[str, dict[str, MarketRule]] = {}
    for b in conn.execute(
        "SELECT account_id, market, fee_rule_set, dividend_model FROM account_market_rules"
    ).fetchall():
        by_account.setdefault(b["account_id"], {})[b["market"]] = MarketRule(
            fee_rule_set=b["fee_rule_set"], dividend_model=b["dividend_model"]
        )
    return [
        Account(
            account_id=r["account_id"], name=r["name"], broker=r["broker"],
            settlement_ccy=Currency(r["settlement_ccy"]),
            funding_ccy=Currency(r["funding_ccy"]),
            dividend_model=r["dividend_model"],
            market_rules=by_account.get(r["account_id"], {}),
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


class StoredTransaction(BaseModel):
    """Pydantic model for a persisted transaction row."""

    id: int
    account_id: str
    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal
    fees: Decimal
    tax: Decimal
    trade_date: date
    fee_rule_snapshot: dict[str, str] = Field(default_factory=dict)
    note: str | None = None
    daytrade: bool = False


def insert_transaction(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    symbol: str,
    side: Side,
    quantity: Decimal,
    price: Decimal,
    fees: Decimal,
    tax: Decimal,
    trade_date: date,
    fee_rule_snapshot: dict[str, str] | None = None,
    note: str | None = None,
    daytrade: bool = False,
    commit: bool = True,
) -> int:
    """Insert a transaction row and return its new primary-key id.

    Pass ``commit=False`` to defer the commit to the caller (the batch-import path
    runs many inserts in one transaction for all-or-nothing atomicity, #1). Single-row
    and manual callers keep the default ``commit=True`` and are unchanged.
    """
    cur = conn.execute(
        """INSERT INTO transactions (account_id, symbol, side, quantity, price, fees, tax,
               trade_date, fee_rule_snapshot, note, daytrade)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            account_id,
            symbol,
            side.value,
            to_db(quantity),
            to_db(_cap_price(price)),
            to_db(fees),
            to_db(tax),
            trade_date.isoformat(),
            json.dumps(fee_rule_snapshot or {}),
            note,
            1 if daytrade else 0,
        ),
    )
    _unarchive_on_booking(conn, symbol)  # held => not archived (FU-D13)
    if commit:
        conn.commit()
    return int(cur.lastrowid or 0)


# ---------------------------------------------------------------------------
# Opening inventory
# ---------------------------------------------------------------------------


class StoredOpening(BaseModel):
    """Pydantic model for a persisted opening_inventory row.

    ``original_cost_total`` is the authoritative money of record; the original average is
    NEVER stored (A6, 2026-07-21) — it is computed on read via :attr:`original_avg`
    (domain-ledger.md: a rounded average must never be the authority).
    """

    account_id: str
    symbol: str
    shares: Decimal
    original_cost_total: Decimal
    build_date: date

    @property
    def original_avg(self) -> Decimal:
        """Original average cost, computed on read (total / shares). Display-only; never the
        authority. Zero shares -> Decimal(0) defensively (a valid opening has shares > 0)."""
        return self.original_cost_total / self.shares if self.shares else Decimal(0)


def upsert_opening(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    symbol: str,
    shares: Decimal,
    original_cost_total: Decimal,
    build_date: date,
    commit: bool = True,
) -> None:
    """Insert or update an opening_inventory row (idempotent on PK account_id+symbol).

    Pass ``commit=False`` to defer the commit to the caller (batch-import atomicity, #1).
    Single-row and manual callers keep the default ``commit=True`` and are unchanged.

    When a row for (account_id, symbol) already exists, the pre-mutation state is
    captured to ``ledger_audit`` as an ``update`` (audit M9); a first insert audits
    nothing (there is no prior row to preserve).
    """
    _write_audit(
        conn, "opening_inventory", f"{account_id}/{symbol}", "update",
        _capture(
            conn,
            "SELECT * FROM opening_inventory WHERE account_id=? AND symbol=?",
            (account_id, symbol),
        ),
    )
    conn.execute(
        """INSERT INTO opening_inventory
               (account_id, symbol, shares, original_cost_total, build_date)
           VALUES (?,?,?,?,?)
           ON CONFLICT(account_id, symbol) DO UPDATE SET
               shares=excluded.shares,
               original_cost_total=excluded.original_cost_total,
               build_date=excluded.build_date""",
        (
            account_id,
            symbol,
            to_db(shares),
            to_db(original_cost_total),
            build_date.isoformat(),
        ),
    )
    _unarchive_on_booking(conn, symbol)  # held => not archived (FU-D13)
    if commit:
        conn.commit()


def list_opening(
    conn: sqlite3.Connection,
    *,
    account_id: str | None = None,
) -> list[StoredOpening]:
    """Return opening_inventory rows ordered by account_id, symbol.

    Optionally filter by *account_id*.
    """
    where: str
    params: list[str]
    if account_id is not None:
        where = " WHERE account_id=?"
        params = [account_id]
    else:
        where = ""
        params = []
    rows = conn.execute(
        f"SELECT account_id, symbol, shares, original_cost_total, "
        f"build_date FROM opening_inventory{where} ORDER BY account_id, symbol",
        params,
    ).fetchall()
    return [
        StoredOpening(
            account_id=r["account_id"],
            symbol=r["symbol"],
            shares=from_db(r["shares"]),
            original_cost_total=from_db(r["original_cost_total"]),
            build_date=date.fromisoformat(r["build_date"]),
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Dividends
# ---------------------------------------------------------------------------


class StoredDividend(BaseModel):
    """Pydantic model for a persisted dividends row."""

    id: int
    account_id: str
    symbol: str
    date: date
    type: str
    gross: Decimal
    withholding: Decimal
    net: Decimal
    reinvest_shares: Decimal | None = None
    reinvest_price: Decimal | None = None


def insert_dividend(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    symbol: str,
    div_date: date,
    div_type: str,
    gross: Decimal,
    withholding: Decimal,
    net: Decimal,
    reinvest_shares: Decimal | None = None,
    reinvest_price: Decimal | None = None,
    commit: bool = True,
) -> int:
    """Insert a dividends row and return its new primary-key id.

    Pass ``commit=False`` to defer the commit to the caller (batch-import atomicity, #1).
    Single-row and manual callers keep the default ``commit=True`` and are unchanged.
    """
    cur = conn.execute(
        """INSERT INTO dividends
               (account_id, symbol, date, type, gross, withholding, net,
                reinvest_shares, reinvest_price)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            account_id,
            symbol,
            div_date.isoformat(),
            div_type,
            to_db(gross),
            to_db(withholding),
            to_db(net),
            to_db(reinvest_shares) if reinvest_shares is not None else None,
            to_db(reinvest_price) if reinvest_price is not None else None,
        ),
    )
    if commit:
        conn.commit()
    return int(cur.lastrowid or 0)


def list_dividends(
    conn: sqlite3.Connection,
    *,
    account_id: str | None = None,
    symbol: str | None = None,
) -> list[StoredDividend]:
    """Return dividends rows ordered by date ASC, id ASC.

    Optionally filter by *account_id* and/or *symbol* (AND logic when both given).
    """
    clauses: list[str] = []
    params: list[str] = []
    if account_id is not None:
        clauses.append("account_id=?")
        params.append(account_id)
    if symbol is not None:
        clauses.append("symbol=?")
        params.append(symbol)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT id, account_id, symbol, date, type, gross, withholding, net, "
        f"reinvest_shares, reinvest_price FROM dividends{where} ORDER BY date ASC, id ASC",
        params,
    ).fetchall()
    return [
        StoredDividend(
            id=r["id"],
            account_id=r["account_id"],
            symbol=r["symbol"],
            date=date.fromisoformat(r["date"]),
            type=r["type"],
            gross=from_db(r["gross"]),
            withholding=from_db(r["withholding"]),
            net=from_db(r["net"]),
            reinvest_shares=(
                from_db(r["reinvest_shares"]) if r["reinvest_shares"] is not None else None
            ),
            reinvest_price=(
                from_db(r["reinvest_price"]) if r["reinvest_price"] is not None else None
            ),
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# FX conversions
# ---------------------------------------------------------------------------


class StoredFxConversion(BaseModel):
    """Pydantic model for a persisted fx_conversions row."""

    id: int
    account_id: str
    date: date
    from_ccy: Currency
    from_amount: Decimal
    to_ccy: Currency
    to_amount: Decimal

    @property
    def implied_rate(self) -> Decimal:
        """Home-currency units per one foreign-currency unit (from_amount / to_amount)."""
        return self.from_amount / self.to_amount


def insert_fx_conversion(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    date: date,
    from_ccy: Currency,
    from_amount: Decimal,
    to_ccy: Currency,
    to_amount: Decimal,
    commit: bool = True,
) -> int:
    """Insert an fx_conversions row and return its new primary-key id.

    Pass ``commit=False`` to defer the commit to the caller (batch-import atomicity, #1).
    Single-row and manual callers keep the default ``commit=True`` and are unchanged.
    """
    cur = conn.execute(
        """INSERT INTO fx_conversions (account_id, date, from_ccy, from_amount, to_ccy,
               to_amount) VALUES (?,?,?,?,?,?)""",
        (
            account_id,
            date.isoformat(),
            from_ccy.value,
            to_db(from_amount),
            to_ccy.value,
            to_db(to_amount),
        ),
    )
    if commit:
        conn.commit()
    return int(cur.lastrowid or 0)


def list_fx_conversions(
    conn: sqlite3.Connection,
    *,
    account_id: str | None = None,
) -> list[StoredFxConversion]:
    """Return fx_conversions rows ordered by date ASC, id ASC.

    Optionally filter by *account_id*.
    """
    where = ""
    params: list[str] = []
    if account_id is not None:
        where = " WHERE account_id=?"
        params = [account_id]
    rows = conn.execute(
        f"SELECT id, account_id, date, from_ccy, from_amount, to_ccy, to_amount "
        f"FROM fx_conversions{where} ORDER BY date ASC, id ASC",
        params,
    ).fetchall()
    return [
        StoredFxConversion(
            id=r["id"],
            account_id=r["account_id"],
            date=date.fromisoformat(r["date"]),
            from_ccy=Currency(r["from_ccy"]),
            from_amount=from_db(r["from_amount"]),
            to_ccy=Currency(r["to_ccy"]),
            to_amount=from_db(r["to_amount"]),
        )
        for r in rows
    ]


def list_transactions(
    conn: sqlite3.Connection,
    *,
    account_id: str | None = None,
    symbol: str | None = None,
) -> list[StoredTransaction]:
    """Return transactions ordered by trade_date ASC, id ASC.

    Optionally filter by *account_id* and/or *symbol* (AND logic when both given).
    """
    clauses: list[str] = []
    params: list[str] = []
    if account_id is not None:
        clauses.append("account_id=?")
        params.append(account_id)
    if symbol is not None:
        clauses.append("symbol=?")
        params.append(symbol)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT id, account_id, symbol, side, quantity, price, fees, tax, trade_date, "
        f"fee_rule_snapshot, note, daytrade FROM transactions{where} "
        f"ORDER BY trade_date ASC, id ASC",
        params,
    ).fetchall()
    return [
        StoredTransaction(
            id=r["id"],
            account_id=r["account_id"],
            symbol=r["symbol"],
            side=Side(r["side"]),
            quantity=from_db(r["quantity"]),
            price=from_db(r["price"]),
            fees=from_db(r["fees"]),
            tax=from_db(r["tax"]),
            trade_date=date.fromisoformat(r["trade_date"]),
            fee_rule_snapshot=json.loads(r["fee_rule_snapshot"] or "{}"),
            note=r["note"],
            daytrade=bool(r["daytrade"]),
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Ledger row corrections: explicit edit / delete (2026-07-02)
# ---------------------------------------------------------------------------
# The ledgers stay "append-only in spirit": corrections are EXPLICIT user actions
# through these helpers (never silent mutation), and every report rebuilds from
# the stored rows afterward, so the 重算 semantics are preserved. The API layer
# replays the mutated ledger through build_book BEFORE writing (oversell guard).


def get_transaction(conn: sqlite3.Connection, txn_id: int) -> StoredTransaction | None:
    """Return one transaction by id, or None."""
    for t in list_transactions(conn):
        if t.id == txn_id:
            return t
    return None


def update_transaction(
    conn: sqlite3.Connection,
    txn_id: int,
    *,
    account_id: str,
    symbol: str,
    side: Side,
    quantity: Decimal,
    price: Decimal,
    fees: Decimal,
    tax: Decimal,
    trade_date: date,
    daytrade: bool,
    note: str | None = None,
    fee_rule_snapshot: dict[str, str] | None = None,
) -> bool:
    """Full-row transaction correction; returns False when the id does not exist.

    ``daytrade`` is persisted (audit MED-1) so a recompute reproduces the sell-side TW tax
    rate; the caller supplies the effective value explicitly (no default — the single
    caller resolves preserve-vs-change against the stored row).

    ``fee_rule_snapshot`` is regenerated by the caller ONLY when the fee/tax are
    recomputed from the new account's rule set (audit M6); pass ``None`` to leave the
    stored snapshot untouched (records the rule set in force when first written). The
    pre-mutation row is captured to ``ledger_audit`` first (M9).
    """
    _write_audit(conn, "transactions", str(txn_id), "update",
                 _capture(conn, "SELECT * FROM transactions WHERE id=?", (txn_id,)))
    dt = 1 if daytrade else 0
    if fee_rule_snapshot is None:
        cur = conn.execute(
            """UPDATE transactions SET account_id=?, symbol=?, side=?, quantity=?, price=?,
                   fees=?, tax=?, trade_date=?, note=?, daytrade=? WHERE id=?""",
            (
                account_id, symbol, side.value, to_db(quantity), to_db(_cap_price(price)),
                to_db(fees), to_db(tax), trade_date.isoformat(), note, dt, txn_id,
            ),
        )
    else:
        cur = conn.execute(
            """UPDATE transactions SET account_id=?, symbol=?, side=?, quantity=?, price=?,
                   fees=?, tax=?, trade_date=?, note=?, daytrade=?, fee_rule_snapshot=?
                   WHERE id=?""",
            (
                account_id, symbol, side.value, to_db(quantity), to_db(_cap_price(price)),
                to_db(fees), to_db(tax), trade_date.isoformat(), note, dt,
                json.dumps(fee_rule_snapshot), txn_id,
            ),
        )
    conn.commit()
    return cur.rowcount > 0


def delete_transaction(conn: sqlite3.Connection, txn_id: int) -> bool:
    _write_audit(conn, "transactions", str(txn_id), "delete",
                 _capture(conn, "SELECT * FROM transactions WHERE id=?", (txn_id,)))
    cur = conn.execute("DELETE FROM transactions WHERE id=?", (txn_id,))
    conn.commit()
    return cur.rowcount > 0


def get_dividend(conn: sqlite3.Connection, div_id: int) -> StoredDividend | None:
    """Return one dividend by id, or None."""
    for d in list_dividends(conn):
        if d.id == div_id:
            return d
    return None


def update_dividend(
    conn: sqlite3.Connection,
    div_id: int,
    *,
    account_id: str,
    symbol: str,
    div_date: date,
    div_type: str,
    gross: Decimal,
    withholding: Decimal,
    net: Decimal,
    reinvest_shares: Decimal | None = None,
    reinvest_price: Decimal | None = None,
) -> bool:
    """Full-row dividend correction; returns False when the id does not exist."""
    _write_audit(conn, "dividends", str(div_id), "update",
                 _capture(conn, "SELECT * FROM dividends WHERE id=?", (div_id,)))
    cur = conn.execute(
        """UPDATE dividends SET account_id=?, symbol=?, date=?, type=?, gross=?,
               withholding=?, net=?, reinvest_shares=?, reinvest_price=? WHERE id=?""",
        (
            account_id, symbol, div_date.isoformat(), div_type, to_db(gross),
            to_db(withholding), to_db(net),
            to_db(reinvest_shares) if reinvest_shares is not None else None,
            to_db(reinvest_price) if reinvest_price is not None else None,
            div_id,
        ),
    )
    conn.commit()
    return cur.rowcount > 0


def delete_dividend(conn: sqlite3.Connection, div_id: int) -> bool:
    _write_audit(conn, "dividends", str(div_id), "delete",
                 _capture(conn, "SELECT * FROM dividends WHERE id=?", (div_id,)))
    cur = conn.execute("DELETE FROM dividends WHERE id=?", (div_id,))
    conn.commit()
    return cur.rowcount > 0


def get_fx_conversion(conn: sqlite3.Connection, fx_id: int) -> StoredFxConversion | None:
    """Return one fx_conversions row by id, or None."""
    for c in list_fx_conversions(conn):
        if c.id == fx_id:
            return c
    return None


def update_fx_conversion(
    conn: sqlite3.Connection,
    fx_id: int,
    *,
    account_id: str,
    date: date,
    from_ccy: Currency,
    from_amount: Decimal,
    to_ccy: Currency,
    to_amount: Decimal,
) -> bool:
    """Full-row FX-conversion correction; returns False when the id does not exist."""
    _write_audit(conn, "fx_conversions", str(fx_id), "update",
                 _capture(conn, "SELECT * FROM fx_conversions WHERE id=?", (fx_id,)))
    cur = conn.execute(
        """UPDATE fx_conversions SET account_id=?, date=?, from_ccy=?, from_amount=?,
               to_ccy=?, to_amount=? WHERE id=?""",
        (
            account_id, date.isoformat(), from_ccy.value, to_db(from_amount),
            to_ccy.value, to_db(to_amount), fx_id,
        ),
    )
    conn.commit()
    return cur.rowcount > 0


def delete_fx_conversion(conn: sqlite3.Connection, fx_id: int) -> bool:
    _write_audit(conn, "fx_conversions", str(fx_id), "delete",
                 _capture(conn, "SELECT * FROM fx_conversions WHERE id=?", (fx_id,)))
    cur = conn.execute("DELETE FROM fx_conversions WHERE id=?", (fx_id,))
    conn.commit()
    return cur.rowcount > 0


def get_opening(
    conn: sqlite3.Connection, account_id: str, symbol: str
) -> StoredOpening | None:
    """Return one opening_inventory row by its (account_id, symbol) key, or None."""
    for o in list_opening(conn, account_id=account_id):
        if o.symbol == symbol:
            return o
    return None


def delete_opening(conn: sqlite3.Connection, account_id: str, symbol: str) -> bool:
    _write_audit(
        conn, "opening_inventory", f"{account_id}/{symbol}", "delete",
        _capture(
            conn,
            "SELECT * FROM opening_inventory WHERE account_id=? AND symbol=?",
            (account_id, symbol),
        ),
    )
    cur = conn.execute(
        "DELETE FROM opening_inventory WHERE account_id=? AND symbol=?",
        (account_id, symbol),
    )
    conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Cash movements (入金 / 出金) — the fifth ledger (2026-07-03, R6 item 7)
# ---------------------------------------------------------------------------
# Deposits/withdrawals per account+currency. Together with fx_conversions and
# trade/dividend settlements they yield the per-account cash pools
# (portfolio/cash.py). Same corrections discipline as the other ledgers.


class StoredCashMovement(BaseModel):
    """Pydantic model for a persisted cash_movements row."""

    id: int
    account_id: str
    date: date
    kind: str  # DEPOSIT | WITHDRAW
    ccy: Currency
    amount: Decimal
    note: str | None = None


def insert_cash_movement(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    move_date: date,
    kind: str,
    ccy: Currency,
    amount: Decimal,
    note: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO cash_movements (account_id, date, kind, ccy, amount, note) "
        "VALUES (?,?,?,?,?,?)",
        (account_id, move_date.isoformat(), kind, ccy.value, to_db(amount), note),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def list_cash_movements(
    conn: sqlite3.Connection, *, account_id: str | None = None
) -> list[StoredCashMovement]:
    where = ""
    params: list[str] = []
    if account_id is not None:
        where = " WHERE account_id=?"
        params = [account_id]
    rows = conn.execute(
        f"SELECT id, account_id, date, kind, ccy, amount, note "
        f"FROM cash_movements{where} ORDER BY date ASC, id ASC",
        params,
    ).fetchall()
    return [
        StoredCashMovement(
            id=r["id"], account_id=r["account_id"],
            date=date.fromisoformat(r["date"]), kind=r["kind"],
            ccy=Currency(r["ccy"]), amount=from_db(r["amount"]), note=r["note"],
        )
        for r in rows
    ]


def get_cash_movement(
    conn: sqlite3.Connection, move_id: int
) -> StoredCashMovement | None:
    for m in list_cash_movements(conn):
        if m.id == move_id:
            return m
    return None


def update_cash_movement(
    conn: sqlite3.Connection,
    move_id: int,
    *,
    account_id: str,
    move_date: date,
    kind: str,
    ccy: Currency,
    amount: Decimal,
    note: str | None = None,
) -> bool:
    cur = conn.execute(
        "UPDATE cash_movements SET account_id=?, date=?, kind=?, ccy=?, amount=?, "
        "note=? WHERE id=?",
        (account_id, move_date.isoformat(), kind, ccy.value, to_db(amount), note,
         move_id),
    )
    conn.commit()
    return cur.rowcount > 0


def delete_cash_movement(conn: sqlite3.Connection, move_id: int) -> bool:
    cur = conn.execute("DELETE FROM cash_movements WHERE id=?", (move_id,))
    conn.commit()
    return cur.rowcount > 0
