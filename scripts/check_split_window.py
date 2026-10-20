"""Check stored price bases against the exchange-local split window; repair with ``--apply``.

The price-basis split edge (owner ruling 2026-10-20). Until this change the write seam's window
``(as_of, fetched_at]`` closed on the APP clock's date (Asia/Taipei). The US quote job fires at
16:30 New York, which is already the next calendar day in Taipei, so the scheduled fetch of a
split's last pre-split session was stored at ratio x as traded. ``history_daily`` normally
re-fetched it about 20 hours later, so most such rows healed. A row that no later fetch rewrote
(a failed or disabled job, a symbol no longer refreshed, a delisted one) still carries the old
basis. The window now closes on the EXCHANGE's date (``pricing.store.split_window_through``),
and this script finds the stored rows that the new window disagrees with.

    python scripts/check_split_window.py --db path/to.db                 # read-only report
    python scripts/check_split_window.py --db path/to.db --apply         # back up, then restate
    python scripts/check_split_window.py --db path/to.db --symbol AAPL   # one symbol (repeatable)

**What it lists.** The symbols are those with a SPLIT in ``corporate_actions``, read through the
app's own reader (``list_corporate_actions``), narrowed by ``--symbol``. For their ``prices`` rows
it lists every row the reconcile would rewrite: ``pricing.reconcile.pending_restatements`` with
the real binding ``scheduler.jobs.split_factor_fn(conn)``. Each line gives the symbol, as_of,
market, fetched_at, the stored and the new ``split_basis`` and close, and the close of the
previous AS-TRADED session: the nearest earlier row the new window agrees with (it is not in the
list), or a note that there is none. A row can also differ for another reason (a close that
drifted from its raw value); it is listed too, because the reconcile repairs it as well.

**US rows and TW / MY rows are listed apart.** TW (Asia/Taipei) and MY (Asia/Kuala_Lumpur) are
UTC+8 like the app clock, so a +08:00 stamp never moves their bound. A TW / MY row is listed only
when its ``fetched_at`` carries another offset — for example a row written with a UTC clock
before the single-clock decision Q6 (2026-07-07): 18:00 UTC on 06-15 is 02:00 on 06-16 in
Taipei, one day later than the old bound. The app clock is ``Settings.tz_display`` (Asia/Taipei
by default), so "+08:00" is a deployment assumption, not an invariant.

**SUSPECT rows.** The new window assumes a fetch made before the exchange's midnight on the
ex-date received the close AS TRADED. A provider that had already restated by then delivered a
post-split close, which the old window stored correctly and the new one reads as unfolded (or,
for a TW / MY row with a non-+08:00 stamp, the mirror case). Such a row's new close lands about
one split ratio away from the previous as-traded session's: past the geometric midpoint
``sqrt(ratio)`` it is marked SUSPECT. ``--apply`` skips every symbol with a SUSPECT row (the
reconcile restates a symbol's rows together) unless ``--include-suspect`` is given, and prints
the repair instead: re-fetch the symbol's history from its earliest SUSPECT row — the
instruments page's 「回補歷史報價」 (``POST /api/actions/backfill-history``, whose window starts
years earlier). A fetch made now lies after the split, so the split is folded back out; run this
check again afterwards.

**Read-only without ``--apply``.** The database is opened ``mode=ro``
(``shared.db.connect_path(..., read_only=True)``), so the report cannot write, even by accident.

**``--apply``**, in order:

1. Probes the write lock (``BEGIN IMMEDIATE`` then ``ROLLBACK``) with ``--busy-timeout``
   seconds of patience (default 10). A lock held longer is refused before anything is written.
2. Backs the database up (``ops.backup.pre_write_snapshot``, prefix ``pre_split_window_``, under
   ``<db dir>/snapshots`` unless ``--backup-dir``) and VERIFIES the copy: it is decompressed,
   ``PRAGMA integrity_check`` must answer ``ok``, and every row about to be restated must be in
   it with the stored values listed above. An unverified backup is refused before any write.
3. Restates through the app's ONE restatement seam, ``api.instrument_service.
   restate_price_basis`` — the one the corporate-action doors use. It first deletes the
   symbols' derived ``signal_history`` / ``signal_states`` rows (W6 / AI-D27: the next scan
   rebuilds them without phantom transition events), then reads and rewrites the rows in ONE
   transaction that holds the write lock, so a quote written concurrently lands before the read
   (and is restated from its own raw) or waits for the commit. Each row is printed before ->
   after, and the check runs again. The settlement store's ``prices`` triggers mark every
   restated day dirty (actor 「外部程式／腳本」), and the next settlement run settles those
   days again.

Exit codes:
  0  done, or nothing to restate (the read-only report always exits 0 unless refused)
  1  rows still differ after --apply (including symbols skipped as SUSPECT)
  2  refused: no database at the path (never created), no prices / corporate_actions table,
     or prices not migrated to the two-column basis
  3  the database's write lock was held past --busy-timeout; prices not restated
  4  the backup could not be verified; nothing written
"""

from __future__ import annotations

import argparse
import gzip
import os
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from portfolio_dash.api import instrument_service  # noqa: E402
from portfolio_dash.data_ingestion.store import list_corporate_actions  # noqa: E402
from portfolio_dash.ops import backup as backup_ops  # noqa: E402
from portfolio_dash.pricing.reconcile import Restatement, pending_restatements  # noqa: E402
from portfolio_dash.scheduler.jobs import split_factor_fn  # noqa: E402
from portfolio_dash.shared.corporate_actions import ActionIndex, CorporateActionKind  # noqa: E402
from portfolio_dash.shared.db import connect_path  # noqa: E402
from portfolio_dash.shared.enums import Market  # noqa: E402
from portfolio_dash.shared.money import from_db  # noqa: E402

EXIT_OK = 0
EXIT_ROWS_LEFT = 1
EXIT_REFUSED = 2
EXIT_LOCKED = 3
EXIT_BACKUP_UNVERIFIED = 4

_SNAPSHOT_PREFIX = "pre_split_window_"
_TZ = ZoneInfo("Asia/Taipei")
#: The ``prices`` columns the reconcile reads; a database without them is not migrated.
_PRICE_COLUMNS = frozenset({
    "instrument", "market", "as_of_date", "fetched_at", "close", "close_raw", "split_basis",
    "open", "open_raw", "high", "high_raw", "low", "low_raw",
})
_REFETCH = ("the instruments page's 「回補歷史報價」 (POST /api/actions/backfill-history); a "
            "fetch made now lies after the split, so it is folded back out. Run this check "
            "again afterwards.")


@dataclass(frozen=True)
class Finding:
    """One row the reconcile would rewrite, with what the report says about it."""

    row: Restatement
    #: ``(as_of, stored close)`` of the nearest earlier session the new window agrees with.
    previous: tuple[str, str] | None
    #: ``None`` when nothing can be compared (no earlier session, or the basis does not move).
    suspect: bool | None


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


def previous_as_traded(
    conn: sqlite3.Connection, row: Restatement, pending_days: set[str],
) -> tuple[str, str] | None:
    """The nearest earlier session of the row's symbol that is NOT itself pending.

    Not pending means the new window agrees with its stored basis, so its close is as traded
    under the same rule the restated row will be. Comparing with a pending neighbour's new
    value instead hid the jump on every row after the first of a batch the provider had
    already restated (review of the split-window fix).
    """
    for r in conn.execute(
        "SELECT as_of_date, close FROM prices WHERE instrument=? AND as_of_date<? "
        "ORDER BY as_of_date DESC", (row.symbol, row.as_of_date),
    ):
        if str(r["as_of_date"]) not in pending_days:
            return str(r["as_of_date"]), str(r["close"])
    return None


def is_suspect(row: Restatement, previous: tuple[str, str] | None) -> bool | None:
    """Whether the new close sits about one split ratio away from the previous as-traded one.

    ``f`` = stored basis / new basis is the ratio the restatement removes (or adds). The new
    close is SUSPECT when ``previous / new`` lies past the geometric midpoint ``sqrt(f)``
    on the far side from 1 — closer to the ratio than to no move at all.
    """
    if previous is None:
        return None
    old_basis, new_basis = from_db(row.current_basis), from_db(row.wanted_basis)
    if old_basis == new_basis or row.wanted[0] is None:
        return None
    ratio = old_basis / new_basis
    move = from_db(previous[1]) / from_db(row.wanted[0])
    midpoint = ratio.sqrt()
    return move > midpoint if ratio > 1 else move < midpoint


def check(
    conn: sqlite3.Connection, only: list[str] | None,
) -> tuple[list[str], int, list[Finding]]:
    """``(symbols checked, unreadable action rows, findings)``."""
    symbols, unreadable = split_symbols(conn)
    if only:
        symbols = [s for s in symbols if s in set(only)]
    pending = pending_restatements(conn, symbols, factor_of=split_factor_fn(conn))
    days: dict[str, set[str]] = {}
    for r in pending:
        days.setdefault(r.symbol, set()).add(r.as_of_date)
    findings: list[Finding] = []
    for r in pending:
        previous = previous_as_traded(conn, r, days[r.symbol])
        findings.append(Finding(r, previous, is_suspect(r, previous)))
    return symbols, unreadable, findings


def _line(f: Finding) -> str:
    r = f.row
    mark = "SUSPECT " if f.suspect else ""
    ohlc = " (open/high/low also restated)" if r.current[1:] != r.wanted[1:] else ""
    context = (f"  [previous as-traded session {f.previous[0]}: {f.previous[1]}]" if f.previous
               else "  [no earlier as-traded session to compare]")
    return (f"  {mark}{r.symbol} {r.as_of_date} {r.market}  fetched_at {r.fetched_at}  "
            f"basis {r.current_basis} -> {r.wanted_basis}  "
            f"close {r.current[0]} -> {r.wanted[0]}{ohlc}{context}")


def suspect_starts(findings: list[Finding]) -> dict[str, str]:
    """Each symbol with a SUSPECT row → its earliest SUSPECT ``as_of``."""
    out: dict[str, str] = {}
    for f in findings:
        if f.suspect:
            out[f.row.symbol] = min(f.row.as_of_date, out.get(f.row.symbol, f.row.as_of_date))
    return out


def _report(symbols: list[str], unreadable: int, findings: list[Finding]) -> None:
    print(f"Symbols with a SPLIT: {len(symbols)}"
          + (f" ({', '.join(symbols)})" if symbols else ""))
    if unreadable:
        print(f"Warning: {unreadable} corporate-action row(s) are unreadable and contribute no "
              "factor (as in the app).")
    print(f"Rows whose stored basis differs from the exchange-date window: {len(findings)}")
    us = [f for f in findings if f.row.market == Market.US.value]
    others = [f for f in findings if f.row.market != Market.US.value]
    print(f"US rows: {len(us)}")
    for f in us:
        print(_line(f))
    print(f"TW / MY rows: {len(others)} (their fetched_at is not +08:00, e.g. written with a UTC "
          "clock before the single-clock decision Q6 of 2026-07-07)")
    for f in others:
        print(_line(f))
    for symbol, start in sorted(suspect_starts(findings).items()):
        print(f"SUSPECT {symbol}: the provider had already restated these rows when they were "
              f"fetched, so restating them would make them wrong. Do not --apply them; "
              f"re-fetch {symbol}'s history from {start} instead: {_REFETCH}")


def _locked(exc: sqlite3.OperationalError) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def verify_backup(snapshot: Path, rows: list[Restatement]) -> str | None:
    """Why the gzip backup at *snapshot* cannot be trusted, or ``None`` when it can.

    The copy is decompressed beside it, ``PRAGMA integrity_check`` must answer ``ok``
    (``ops.backup.check_integrity``, the daily backup job's own check), and every row about to
    be restated must be present with the stored close and basis the report listed — the
    values a restore would bring back.
    """
    fd, name = tempfile.mkstemp(suffix=".db", dir=snapshot.parent)
    os.close(fd)
    copy = Path(name)
    try:
        try:
            with gzip.open(snapshot, "rb") as gz, open(copy, "wb") as out:
                shutil.copyfileobj(gz, out)
        except OSError as exc:
            return f"it does not decompress ({exc})"
        ok, detail = backup_ops.check_integrity(copy)
        if not ok:
            return f"integrity_check answered {detail!r}"
        conn = sqlite3.connect(copy)
        try:
            for r in rows:
                got = conn.execute(
                    "SELECT close, split_basis FROM prices WHERE instrument=? AND as_of_date=?",
                    (r.symbol, r.as_of_date)).fetchone()
                if got is None or (str(got[0]), str(got[1])) != (r.current[0], r.current_basis):
                    return f"{r.symbol} {r.as_of_date} is missing or differs in the copy"
            total = int(conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0])
        finally:
            conn.close()
        print(f"Backup verified: integrity ok, {total} prices row(s), the {len(rows)} row(s) "
              "to restate present as listed.")
        return None
    finally:
        copy.unlink(missing_ok=True)


def apply(conn: sqlite3.Connection, symbols: list[str]) -> list[Restatement]:
    """Restate *symbols* through the app's ONE seam; print each row before and after."""
    applied = instrument_service.restate_price_basis(conn, symbols)
    for r in applied:
        print(f"  {r.symbol} {r.as_of_date}: close {r.current[0]} -> {r.wanted[0]}, "
              f"basis {r.current_basis} -> {r.wanted_basis}")
    return applied


def main(argv: list[str] | None = None) -> int:
    doc = __doc__ or ""
    ap = argparse.ArgumentParser(
        description=doc.split("\n\n")[0],
        epilog="Exit codes:" + doc.split("Exit codes:", 1)[-1] if "Exit codes:" in doc else None,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--db", required=True, help="SQLite database path (never created)")
    ap.add_argument("--apply", action="store_true", help="write (default: read-only report)")
    ap.add_argument("--symbol", action="append", default=None,
                    help="check / restate only this symbol (repeatable)")
    ap.add_argument("--include-suspect", action="store_true",
                    help="with --apply, restate symbols with SUSPECT rows too")
    ap.add_argument("--backup-dir", default=None,
                    help="where the pre-write snapshot goes (default: <db dir>/snapshots)")
    ap.add_argument("--busy-timeout", type=float, default=10.0,
                    help="seconds to wait for the database's write lock (default 10)")
    args = ap.parse_args(argv)

    db = Path(args.db)
    if not db.is_file():
        print(f"Refused: no database at {db} (a new file is never created).", file=sys.stderr)
        return EXIT_REFUSED
    ro = connect_path(db, read_only=True)
    try:
        why = refusal(ro)
        if why is not None:
            print(f"Refused: {why}; nothing written.", file=sys.stderr)
            return EXIT_REFUSED
        symbols, unreadable, findings = check(ro, args.symbol)
        _report(symbols, unreadable, findings)
    finally:
        ro.close()
    if not findings:
        print("Nothing to restate.")
        return EXIT_OK
    if not args.apply:
        print("(read-only) Re-run with --apply to back up the database and restate these rows.")
        return EXIT_OK

    suspects = set() if args.include_suspect else set(suspect_starts(findings))
    for symbol in sorted(suspects):
        print(f"Skipped {symbol}: SUSPECT row(s); re-fetch its history instead "
              "(or pass --include-suspect).")
    targets = sorted({f.row.symbol for f in findings} - suspects)
    to_restate = [f.row for f in findings if f.row.symbol in targets]
    applied: list[Restatement] = []
    conn = connect_path(db)
    try:
        conn.execute(f"PRAGMA busy_timeout = {max(0, int(args.busy_timeout * 1000))}")
        if targets:
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.rollback()
            except sqlite3.OperationalError as exc:
                if not _locked(exc):
                    raise
                print(f"Refused: another connection held the database's write lock for more "
                      f"than {args.busy_timeout:g} s ({exc}); nothing written. Retry when the "
                      "app is idle, or stop the service first.", file=sys.stderr)
                return EXIT_LOCKED
            snapshot = backup_ops.pre_write_snapshot(
                prefix=_SNAPSHOT_PREFIX, db_path=db,
                backup_dir=Path(args.backup_dir) if args.backup_dir else None,
                now=datetime.now(_TZ),
            )
            print(f"Backed up the database: {snapshot}")
            problem = verify_backup(snapshot, to_restate)
            if problem is not None:
                print(f"Refused: the backup {snapshot} could not be verified ({problem}); "
                      "nothing written.", file=sys.stderr)
                return EXIT_BACKUP_UNVERIFIED
            try:
                applied = apply(conn, targets)
            except sqlite3.OperationalError as exc:
                if not _locked(exc):
                    raise
                print(f"Refused: the write lock was taken again before the restatement "
                      f"({exc}); prices not restated (derived signal rows of {', '.join(targets)} "
                      "may already be cleared — the next scan rebuilds them). Retry.",
                      file=sys.stderr)
                return EXIT_LOCKED
        checked = sorted({f.row.symbol for f in findings})
        left = pending_restatements(conn, checked, factor_of=split_factor_fn(conn))
    finally:
        conn.close()
    print(f"Restated {len(applied)} row(s); rows still differing: {len(left)}.")
    return EXIT_OK if not left else EXIT_ROWS_LEFT


if __name__ == "__main__":
    sys.exit(main())
