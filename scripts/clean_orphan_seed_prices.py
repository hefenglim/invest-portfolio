"""Delete ORPHAN SPINOFF seed prices — seed-signature rows no corporate action owns.

DEF-063 (owner ruling ④, 2026-09-25). A SPINOFF's child price typed on the corporate-action
form is the one ``prices`` row a human writes (``pricing/seed.py``). Since R4 a seed is only
written into an EMPTY slot and a delete takes back only the row its action RECORDED writing
(``corporate_actions.child_seed_json``) — so the orphans the R2 / R3 code left behind (a delete
that missed its seed, a date edit that stranded one) are never removed by anything, and no
screen can remove them. This removes the DATA; the code that made them is already fixed.

    python scripts/clean_orphan_seed_prices.py --db path/to.db                  # dry run
    python scripts/clean_orphan_seed_prices.py --db path/to.db --apply --reason "…"
    python scripts/clean_orphan_seed_prices.py --db path/to.db --apply --reason "…" --backfill

**An orphan is a row for which ALL of these hold** (anything else is never touched):

* ``prices.source`` = ``pricing.seed.SEED_SOURCE`` (``"manual"``) and ``fetched_at`` = its own
  ``as_of_date`` at 00:00 — the full seed signature (``pricing.seed.has_seed_signature``);
* no corporate action OWNS its ``(instrument, as_of_date)`` — decided by
  ``StoredCorporateAction.owned_seed_slot`` (``data_ingestion/store.py``), the SAME method a
  SPINOFF's delete, batch undo and edit ask, so this script and those doors agree by
  construction: a row whose ``child_seed_json``
  records writing that slot owns it, and a legacy SPINOFF with no record (NULL) owns its own
  ``(to_symbol, date)`` by R3's signature rule.

Refuses (exit 2, nothing written) without ``--db`` (argparse), on a path that does not exist
(``sqlite3.connect`` would silently CREATE an empty database), on a database with no
``prices`` / ``corporate_actions`` table or not yet migrated to ``child_seed_json`` (ownership
could not be decided), and on ``--apply`` without a ``--reason`` or without ``ledger_audit``.

With ``--apply``: the database is backed up FIRST (``ops.backup.pre_write_snapshot`` — a
timestamped ``pre_clean_orphan_seed_*.db.gz`` under ``<db dir>/snapshots``, path printed); then,
in ONE transaction that re-derives the plan under the write lock, each orphan's full row goes
to ``ledger_audit`` (``table_name='prices'``, ``action='delete'``, ``source`` = the script) and
the row is deleted; one ``action_log`` row records the operation (id printed). Any failure
rolls all of it back. A re-run is a no-op: the dry run then lists 0 rows.

``--backfill`` (with ``--apply``) then runs the existing history refresh
(``pricing.refresh.refresh_history`` with the scheduler's split-factor binding — the same
operation as the nightly 歷史回補) for the affected symbols from their earliest cleaned day, to
recover a provider quote an R3 seed had overwritten. It prints how many rows it wrote. It needs
the network; without it (or on any failure) it reports and exits 0 — the cleanup is already
committed and is not affected. Like the nightly job, a quote it fetches replaces a seed on the
same day (that is the seed lifecycle, ``pricing/seed.py``), so it only runs on the symbols this
cleanup touched.

Exit code: 0 = done (or nothing to do), 2 = refused (nothing written).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from portfolio_dash.data_ingestion.store import list_corporate_actions  # noqa: E402
from portfolio_dash.ops import backup as backup_ops  # noqa: E402
from portfolio_dash.pricing.seed import SEED_SOURCE, has_seed_signature  # noqa: E402

_TZ = ZoneInfo("Asia/Taipei")
_REFUSED = 2
_SNAPSHOT_PREFIX = "pre_clean_orphan_seed_"
_AUDIT_SOURCE = "腳本 clean_orphan_seed_prices（DEF-063）"
_ACTION_LOG_DDL = """
CREATE TABLE IF NOT EXISTS action_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    username TEXT,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    action TEXT NOT NULL,
    status INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL
);
"""
_ORPHAN_REASON = (
    "來源 manual 且 fetched_at＝當日 00:00（起始價簽章完整）；沒有任何公司行動的 "
    "child_seed_json 記錄寫入此 (標的, 日期)；也沒有舊格式（child_seed_json 為 NULL）的分拆"
    "以 to_symbol＝此標的、日期＝此日擁有它"
)


@dataclass(frozen=True)
class Verdict:
    """One ``manual`` price row and what this script decides about it."""

    row: dict[str, object]
    orphan: bool
    reason: str

    @property
    def symbol(self) -> str:
        return str(self.row["instrument"])

    @property
    def day(self) -> str:
        return str(self.row["as_of_date"])


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[1]) for r in conn.execute(f'PRAGMA table_info("{table}")')}


def owners(conn: sqlite3.Connection) -> dict[tuple[str, str], list[str]]:
    """``(symbol, ISO day)`` → who owns that seed slot, per
    ``StoredCorporateAction.owned_seed_slot`` over every corporate action (read through the
    app's one reader, ``list_corporate_actions``)."""
    out: dict[tuple[str, str], list[str]] = {}
    for a in list_corporate_actions(conn):
        slot = a.owned_seed_slot()
        if slot is None:
            continue
        how = ("child_seed_json 記錄" if a.child_seed is not None
               else "舊格式分拆（child_seed_json 為 NULL，R3 簽章規則）")
        out.setdefault((slot[0], slot[1].isoformat()), []).append(f"公司行動 #{a.id}（{how}）")
    return out


def plan(conn: sqlite3.Connection) -> list[Verdict]:
    """Every ``manual`` price row, each judged orphan or kept (with the reason)."""
    owned = owners(conn)
    verdicts: list[Verdict] = []
    rows = conn.execute(
        "SELECT * FROM prices WHERE source = ? ORDER BY instrument, as_of_date", (SEED_SOURCE,)
    ).fetchall()
    for r in rows:
        row = dict(r)
        symbol, day = str(row["instrument"]), str(row["as_of_date"])
        if not has_seed_signature(str(row["source"]), str(row["fetched_at"]),
                                  date.fromisoformat(day[:10])):
            verdicts.append(Verdict(row, False,
                                    "保留：簽章不完整（fetched_at 不是當日 00:00）"))
        elif (symbol, day) in owned:
            verdicts.append(Verdict(row, False,
                                    "保留：仍被擁有 — " + "、".join(owned[(symbol, day)])))
        else:
            verdicts.append(Verdict(row, True, _ORPHAN_REASON))
    return verdicts


def _print_plan(verdicts: list[Verdict]) -> None:
    orphans = [v for v in verdicts if v.orphan]
    print(f"prices 中來源為 {SEED_SOURCE!r} 的列：{len(verdicts)} 筆；"
          f"孤兒起始價：{len(orphans)} 筆")
    for v in verdicts:
        mark = "→ 刪除" if v.orphan else "  保留"
        print(f"  {mark} {v.symbol} {v.day}  close {v.row['close']}  "
              f"fetched_at {v.row['fetched_at']}")
        print(f"         理由：{v.reason}")


def _audit(conn: sqlite3.Connection, v: Verdict, at: str, *, has_source: bool) -> None:
    key = f"{v.symbol}/{v.day}"
    before = json.dumps(v.row, ensure_ascii=False, default=str)
    if has_source:
        conn.execute(
            "INSERT INTO ledger_audit (table_name, row_id, action, before_json, at, source) "
            "VALUES ('prices', ?, 'delete', ?, ?, ?)", (key, before, at, _AUDIT_SOURCE))
    else:
        conn.execute(
            "INSERT INTO ledger_audit (table_name, row_id, action, before_json, at) "
            "VALUES ('prices', ?, 'delete', ?, ?)", (key, before, at))


def backfill(conn: sqlite3.Connection, cleaned: list[Verdict], *, now: datetime) -> int:
    """Re-run the history refresh for the cleaned symbols; returns the rows it wrote.

    Imported lazily: the scheduler module is only needed for this optional step, and a
    failure to import or fetch must not take the (already committed) cleanup with it."""
    from portfolio_dash.pricing.defaults import default_registry
    from portfolio_dash.pricing.refresh import refresh_history
    from portfolio_dash.pricing.refs import InstrumentRef
    from portfolio_dash.scheduler.jobs import DEFAULT_BOARD, split_factor_fn
    from portfolio_dash.shared.enums import Market

    starts: dict[str, date] = {}
    for v in cleaned:
        d = date.fromisoformat(v.day[:10])
        starts[v.symbol] = min(d, starts.get(v.symbol, d))
    registry = default_registry(conn)
    factor_of = split_factor_fn(conn)
    for symbol, start in sorted(starts.items()):
        inst = conn.execute("SELECT market, board FROM instruments WHERE symbol = ?",
                            (symbol,)).fetchone()
        if inst is None:
            print(f"  回補略過 {symbol}：未註冊標的")
            continue
        market = Market(str(inst["market"]))
        ref = InstrumentRef(symbol=symbol, market=market,
                            board=str(inst["board"] or DEFAULT_BOARD[market]))
        summary = refresh_history(conn, registry, [ref], start, now=now, factor_of=factor_of)
        state = ("成功" if symbol in summary.ok
                 else "失敗：" + "；".join(summary.failed or ["無資料"]))
        print(f"  回補 {symbol}（自 {start.isoformat()}）：{state}")
    conn.commit()
    marks = ",".join("?" * len(starts))
    row = conn.execute(
        f"SELECT COUNT(*) FROM prices WHERE instrument IN ({marks}) AND fetched_at = ?",  # noqa: S608
        (*sorted(starts), now.isoformat()),
    ).fetchone()
    return int(row[0])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    ap.add_argument("--db", required=True, help="SQLite database path (never created)")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--reason", default=None, help="required with --apply; recorded in action_log")
    ap.add_argument("--backfill", action="store_true",
                    help="after --apply, re-run the history backfill for the cleaned symbols")
    ap.add_argument("--backup-dir", default=None,
                    help="where the pre-write snapshot goes (default: <db dir>/snapshots)")
    args = ap.parse_args(argv)
    started = time.monotonic()

    db = Path(args.db)
    if not db.is_file():
        print(f"拒絕：找不到資料庫 {db}（不會建立新檔）", file=sys.stderr)
        return _REFUSED
    if args.apply and not (args.reason and args.reason.strip()):
        print("拒絕：--apply 需要 --reason \"…\"（寫入 action_log），未寫入。", file=sys.stderr)
        return _REFUSED
    conn = sqlite3.connect(str(db), isolation_level=None)  # explicit transactions below
    conn.row_factory = sqlite3.Row
    try:
        tables = _tables(conn)
        for needed in ("prices", "corporate_actions"):
            if needed not in tables:
                print(f"拒絕：此資料庫沒有 {needed} 表，未寫入。", file=sys.stderr)
                return _REFUSED
        if "child_seed_json" not in _columns(conn, "corporate_actions"):
            print("拒絕：corporate_actions 尚無 child_seed_json 欄位（資料庫未升級到 R4），"
                  "無法判定擁有者，未寫入。", file=sys.stderr)
            return _REFUSED
        verdicts = plan(conn)
        _print_plan(verdicts)
        orphans = [v for v in verdicts if v.orphan]
        if not orphans:
            print("沒有需要清理的孤兒起始價。")
            return 0
        if not args.apply:
            print(f"（試跑）將刪除 {len(orphans)} 列；加 --apply --reason \"…\" 才會寫入"
                  "（寫入前會先備份資料庫）。" + ("--backfill 只在 --apply 時執行。"
                                                 if args.backfill else ""))
            return 0
        if "ledger_audit" not in tables:
            print("拒絕：此資料庫沒有 ledger_audit 表，無法留下稽核紀錄，未寫入。", file=sys.stderr)
            return _REFUSED

        now = datetime.now(_TZ)
        backup_dir = Path(args.backup_dir) if args.backup_dir else None
        snapshot = backup_ops.pre_write_snapshot(prefix=_SNAPSHOT_PREFIX, db_path=db,
                                                 backup_dir=backup_dir, now=now)
        print(f"已備份資料庫：{snapshot}")

        conn.executescript(_ACTION_LOG_DDL)
        has_source = "source" in _columns(conn, "ledger_audit")
        at = now.isoformat()
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Re-derived under the write lock: the app may have written since the listing.
            cleaned = [v for v in plan(conn) if v.orphan]
            for v in cleaned:
                _audit(conn, v, at, has_source=has_source)
                conn.execute(
                    "DELETE FROM prices WHERE instrument = ? AND as_of_date = ? AND source = ? "
                    "AND fetched_at = ?",
                    (v.symbol, v.day, SEED_SOURCE, v.row["fetched_at"]),
                )
            slots = "、".join(f"{v.symbol} {v.day}" for v in cleaned)
            cur = conn.execute(
                "INSERT INTO action_log (ts, username, method, path, action, status, "
                "duration_ms) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    at, "script", "SCRIPT", "scripts/clean_orphan_seed_prices.py --apply",
                    f"孤兒起始價清理（資料清理）{len(cleaned)} 列：{slots}；{args.reason.strip()}",
                    200, int((time.monotonic() - started) * 1000),
                ),
            )
            log_id = int(cur.lastrowid or 0)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        left = sum(1 for v in plan(conn) if v.orphan)
        print(f"已刪除 {len(cleaned)} 列孤兒起始價；清理後剩餘孤兒：{left} 列。"
              f"ledger_audit 已留存每列原始內容；action_log #{log_id}。")

        if args.backfill:
            print("執行歷史回補（取回被起始價覆蓋的正式報價）：")
            try:
                written = backfill(conn, cleaned, now=now)
            except Exception as exc:  # noqa: BLE001 — the cleanup is committed; report and go
                print(f"回補失敗（{type(exc).__name__}: {exc}）；清理已完成、不受影響，"
                      "可待網路恢復後由「歷史回補」補齊。")
            else:
                print(f"回補寫入 {written} 列報價。")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
