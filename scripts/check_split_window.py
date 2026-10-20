"""Check stored price bases against the exchange-local split window; repair with ``--apply``.

The price-basis split edge (owner ruling 2026-10-20). Until this change the write seam's window
``(as_of, fetched_at]`` closed on the APP clock's date (Asia/Taipei). The US quote job fires at
16:30 New York, which is already the next calendar day in Taipei, so the scheduled fetch of a
split's last pre-split session was stored at ratio x as traded. ``history_daily`` normally
re-fetched it about 20 hours later, so most such rows healed. A row that no later fetch rewrote
(a failed or disabled job, a symbol no longer refreshed, a delisted one) still carries the old
basis. The window now closes on the EXCHANGE's date (``pricing.store.split_window_through``),
and this script finds the stored rows that the new window disagrees with.

    python scripts/check_split_window.py --db path/to.db            # read-only report
    python scripts/check_split_window.py --db path/to.db --apply    # back up, then restate

**What it lists.** The symbols are those with a SPLIT in ``corporate_actions``, read through the
app's own reader (``list_corporate_actions``). For their ``prices`` rows it lists every row the
reconcile would rewrite: ``pricing.reconcile.pending_restatements`` with the real binding
``scheduler.jobs.split_factor_fn(conn)``. That is the computation ``reconcile_prices`` runs, so
the listing is exactly what ``--apply`` changes. Each line gives the symbol, as_of, market,
fetched_at, the stored and the new ``split_basis``, the stored and the new close, and the
previous session's close for comparison. A row can also differ for another reason (a close that
drifted from its raw value). It is listed too, because the reconcile repairs it as well.

**Read the list before applying.** The new window assumes a fetch before the exchange's
midnight on the ex-date received the close as traded. A provider that had already restated by
then (the evening before the ex-date, in New York) delivered a post-split close, which the old
window had stored correctly and the new one reads as unfolded. Such a row's new close lands
about one split ratio away from the previous session's. Do not ``--apply`` over it; re-fetch the
symbol's history instead (a fetch on or after the ex-date folds the split back out).

**Read-only without ``--apply``.** The database is opened ``mode=ro``
(``shared.db.connect_path(..., read_only=True)``), so the report cannot write, even by accident.

**``--apply``** first backs the database up (``ops.backup.pre_write_snapshot``, prefix
``pre_split_window_``, under ``<db dir>/snapshots`` unless ``--backup-dir`` is given). It then
runs ``pricing.reconcile.reconcile_prices`` for the affected symbols only, with the same binding
``api.instrument_service.reconcile_price_basis`` uses, prints each row before and after, and
checks again (0 rows left). The settlement store's ``prices`` triggers mark every restated day
dirty, with the actor 「外部程式／腳本」, and the next settlement run settles those days again.

Refuses (exit 2, nothing written) on a path that does not exist (``sqlite3`` would create an
empty database), and on a database whose ``prices`` table is missing or not yet migrated to the
two-column basis, or which has no ``corporate_actions`` table.

Exit code: 0 = done (or nothing to do), 2 = refused.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from portfolio_dash.data_ingestion.store import list_corporate_actions  # noqa: E402
from portfolio_dash.ops import backup as backup_ops  # noqa: E402
from portfolio_dash.pricing.reconcile import (  # noqa: E402
    Restatement,
    pending_restatements,
    reconcile_prices,
)
from portfolio_dash.scheduler.jobs import split_factor_fn  # noqa: E402
from portfolio_dash.shared.corporate_actions import ActionIndex, CorporateActionKind  # noqa: E402
from portfolio_dash.shared.db import connect_path  # noqa: E402

_REFUSED = 2
_SNAPSHOT_PREFIX = "pre_split_window_"
_TZ = ZoneInfo("Asia/Taipei")
#: The ``prices`` columns the reconcile reads; a database without them is not migrated.
_PRICE_COLUMNS = frozenset({
    "instrument", "market", "as_of_date", "fetched_at", "close", "close_raw", "split_basis",
    "open", "open_raw", "high", "high_raw", "low", "low_raw",
})


def refusal(conn: sqlite3.Connection) -> str | None:
    """Why this database cannot be checked, or ``None`` when it can."""
    tables = {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "corporate_actions" not in tables:
        return "this database has no corporate_actions table"
    if "prices" not in tables:
        return "this database has no prices table"
    columns = {str(r[1]) for r in conn.execute("PRAGMA table_info(prices)")}
    missing = sorted(_PRICE_COLUMNS - columns)
    if missing:
        return f"prices is not migrated to the two-column basis (missing {', '.join(missing)})"
    return None


def split_symbols(conn: sqlite3.Connection) -> tuple[list[str], int]:
    """The symbols with a SPLIT in the ledger, and how many action rows were unreadable.

    An unreadable row contributes no factor, exactly as in the app (``split_factor_fn``).
    """
    index = ActionIndex.from_stored(list_corporate_actions(conn))
    symbols = {a.from_symbol for a in index.all if a.kind is CorporateActionKind.SPLIT}
    return sorted(symbols), len(index.unreadable)


def check(conn: sqlite3.Connection) -> tuple[list[str], int, list[Restatement]]:
    """``(split symbols, unreadable action rows, rows the reconcile would rewrite)``."""
    symbols, unreadable = split_symbols(conn)
    return symbols, unreadable, pending_restatements(
        conn, symbols, factor_of=split_factor_fn(conn))


def previous_close(
    conn: sqlite3.Connection, r: Restatement, pending: list[Restatement],
) -> tuple[str, str] | None:
    """The symbol's previous stored session ``(as_of, close)`` as it will read after a
    restatement (its pending value when it is pending too), or ``None`` when there is none.

    The comparison the operator needs: a restated close is AS TRADED, so it should sit near
    the previous session's. One that lands about one split ratio away from it means the
    provider had ALREADY restated that row when it was fetched (the new window reads it as
    not yet folded). Restating it would make it wrong, and re-fetching the symbol's history
    is the repair instead.
    """
    row = conn.execute(
        "SELECT as_of_date, close FROM prices WHERE instrument=? AND as_of_date<? "
        "ORDER BY as_of_date DESC LIMIT 1", (r.symbol, r.as_of_date),
    ).fetchone()
    if row is None:
        return None
    day = str(row["as_of_date"])
    wanted = {p.as_of_date: p.wanted[0] for p in pending if p.symbol == r.symbol}
    return day, str(wanted.get(day) or row["close"])


def _line(r: Restatement, prev: tuple[str, str] | None) -> str:
    ohlc = " (open/high/low also restated)" if r.current[1:] != r.wanted[1:] else ""
    context = f"  [previous session {prev[0]}: {prev[1]}]" if prev else ""
    return (f"  {r.symbol} {r.as_of_date} {r.market}  fetched_at {r.fetched_at}  "
            f"basis {r.current_basis} -> {r.wanted_basis}  "
            f"close {r.current[0]} -> {r.wanted[0]}{ohlc}{context}")


def _report(conn: sqlite3.Connection, symbols: list[str], unreadable: int,
            pending: list[Restatement]) -> None:
    print(f"Symbols with a SPLIT: {len(symbols)}"
          + (f" ({', '.join(symbols)})" if symbols else ""))
    if unreadable:
        print(f"Warning: {unreadable} corporate-action row(s) are unreadable and contribute no "
              "factor (as in the app).")
    print(f"Rows whose stored basis differs from the exchange-date window: {len(pending)}")
    for r in pending:
        print(_line(r, previous_close(conn, r, pending)))
    if pending:
        print("Each new close is as traded and should sit near its previous session's close. "
              "One about a split ratio away means the provider had already restated that row "
              "when it was fetched: do not --apply; re-fetch the symbol's history instead.")


def apply(conn: sqlite3.Connection, symbols: list[str]) -> tuple[int, int]:
    """Restate *symbols* with the real binding; print each row before and after.

    Returns ``(rows changed, rows still pending afterwards)``. The rows are re-derived on this
    write connection, so a quote the app wrote since the report is not overwritten blindly.
    """
    factor_of = split_factor_fn(conn)
    before = pending_restatements(conn, symbols, factor_of=factor_of)
    changed = reconcile_prices(conn, symbols, factor_of=factor_of)
    for r in before:
        now = conn.execute(
            "SELECT close, split_basis FROM prices WHERE instrument=? AND as_of_date=?",
            (r.symbol, r.as_of_date),
        ).fetchone()
        print(f"  {r.symbol} {r.as_of_date}: close {r.current[0]} -> {now['close']}, "
              f"basis {r.current_basis} -> {now['split_basis']}")
    left = pending_restatements(conn, symbols, factor_of=split_factor_fn(conn))
    return changed, len(left)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    ap.add_argument("--db", required=True, help="SQLite database path (never created)")
    ap.add_argument("--apply", action="store_true", help="write (default: read-only report)")
    ap.add_argument("--backup-dir", default=None,
                    help="where the pre-write snapshot goes (default: <db dir>/snapshots)")
    args = ap.parse_args(argv)

    db = Path(args.db)
    if not db.is_file():
        print(f"Refused: no database at {db} (a new file is never created).", file=sys.stderr)
        return _REFUSED
    ro = connect_path(db, read_only=True)
    try:
        why = refusal(ro)
        if why is not None:
            print(f"Refused: {why}; nothing written.", file=sys.stderr)
            return _REFUSED
        symbols, unreadable, pending = check(ro)
        _report(ro, symbols, unreadable, pending)
    finally:
        ro.close()
    if not pending:
        print("Nothing to restate.")
        return 0
    if not args.apply:
        print("(read-only) Re-run with --apply to back up the database and restate these rows.")
        return 0

    snapshot = backup_ops.pre_write_snapshot(
        prefix=_SNAPSHOT_PREFIX, db_path=db,
        backup_dir=Path(args.backup_dir) if args.backup_dir else None,
        now=datetime.now(_TZ),
    )
    print(f"Backed up the database: {snapshot}")
    conn = connect_path(db)
    try:
        changed, left = apply(conn, sorted({r.symbol for r in pending}))
    finally:
        conn.close()
    print(f"Restated {changed} row(s); rows still differing: {left}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
