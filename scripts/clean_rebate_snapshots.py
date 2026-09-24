"""Make incoherent TW fee snapshots coherent — ``rebate_rate`` → ``0`` where ``discount < 1``.

DEF-010 (owner ruling 2026-09-24). A TW trade booked while its rule set had BOTH benefits
switched on — ``discount < 1`` (charged less at settlement) AND ``rebate_rate > 0`` (a refund
next month) — carries a ``fee_rule_snapshot`` that claims the same broker benefit twice
(``markets-and-fees.md``: the two are one benefit expressed two ways). The rebate forecaster
used to read that as "forecast a 77 % refund on a fee already cut to 23 %". The code is fixed
(``data_ingestion/fees.py::rebate_applies``); this cleans the DATA the app never rewrites.

    python scripts/clean_rebate_snapshots.py --db path/to.db --scope all          # dry run
    python scripts/clean_rebate_snapshots.py --db path/to.db --scope all --apply  # write

What "clean" means, precisely — and what it does NOT touch:

* **The fee of record never changes.** ``fees`` / ``tax`` / every other column are the money
  that actually left the account (original cost is never overwritten — ``domain-ledger.md``).
  The discount at settlement is what HAPPENED (the fee proves it), so ``discount`` stays; the
  claim that did not happen — a refund on top — is the part set to ``"0"``.
* The snapshot keeps its history: ``rebate_rate_was`` holds the old value and ``cleaned``
  names this ruling and the time, so the row itself says why it differs from its siblings.
* A snapshot without a ``discount`` key, or with ``discount >= 1``, or ``rebate_rate`` already
  ``0``, is never selected — which is what makes a re-run a no-op (idempotent).

``--scope`` is REQUIRED, because the ruling's count and the data's count differ:

* ``all`` — every incoherent row (10 on demo 2026-09-24: ids 26 28 29 32 33 34 42 44 45 53).
* ``uncredited`` — only rows whose (account, trade month) has NO booked 折讓款 credit (8 on
  demo: the two 2026-01 rows, 33 and 44, sit in a month already credited by cash #34 — which
  is why they were not in the verifier's forecast count).
``--id`` (repeatable) narrows either scope further to named rows.

Refuses (exit 2, nothing written) without ``--db``/``--scope``, on a path that does not exist
(``sqlite3.connect`` would silently CREATE an empty database), on a database with no
``transactions`` or ``ledger_audit`` table, and on an ``--id`` that is not an incoherent row.
With ``--apply``, in ONE transaction: each row's full pre-image is copied into
``ledger_audit`` (``action='update'``), the snapshot is rewritten, and one ``action_log`` row
records the operation (系統操作記錄). Any failure rolls all of it back. Prints before/after.

Exit code: 0 = done (or nothing to do), 2 = refused (nothing written).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("Asia/Taipei")
_REFUSED = 2
_ONE = Decimal("1")
_ZERO = Decimal("0")
_RULING = "DEF-010（owner 裁定 2026-09-24）：費用已於成交時打折，不會再有次月折讓款"
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
_TAG_SUFFIX = " 折讓款"


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[1]) for r in conn.execute(f'PRAGMA table_info("{table}")')}


def _dec(raw: object) -> Decimal | None:
    if raw is None:
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


def incoherent(snapshot: dict[str, object]) -> bool:
    """A snapshot that claims BOTH benefits: charged at a discount AND refunded later."""
    discount = _dec(snapshot.get("discount"))
    rebate = _dec(snapshot.get("rebate_rate"))
    return discount is not None and rebate is not None and discount < _ONE and rebate > _ZERO


def cleaned(snapshot: dict[str, object], *, at: str) -> dict[str, object]:
    """The coherent snapshot: ``rebate_rate`` → ``"0"``, the old value and the reason kept."""
    out = dict(snapshot)
    out["rebate_rate_was"] = str(snapshot.get("rebate_rate"))
    out["rebate_rate"] = "0"
    out["cleaned"] = f"{_RULING}（{at}）"
    return out


def _prev_month(d: date) -> str:
    y, m = (d.year - 1, 12) if d.month == 1 else (d.year, d.month - 1)
    return f"{y:04d}-{m:02d}"


def credited_months(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    """(account_id, YYYY-MM) trade months a booked 折讓款 credit already covers.

    The same keys ``api/rebates.py::_confirmed_months`` reads, re-stated in SQL so this
    script runs on a database the new app build has not migrated yet: the explicit
    ``rebate_period`` link when the column exists, else the legacy dual key (the month
    before the credit's date, and the 「YYYY-MM 折讓款」 note tag).
    """
    if "cash_movements" not in _tables(conn):
        return set()
    has_link = "rebate_period" in _columns(conn, "cash_movements")
    cols = "account_id, date, kind, note" + (", rebate_period" if has_link else "")
    out: set[tuple[str, str]] = set()
    for r in conn.execute(f"SELECT {cols} FROM cash_movements"):
        acct = str(r["account_id"])
        if has_link and r["rebate_period"]:
            out.add((acct, str(r["rebate_period"])))
            continue
        if str(r["kind"]).upper() != "REBATE":
            continue
        out.add((acct, _prev_month(date.fromisoformat(str(r["date"])))))
        note = str(r["note"] or "")
        if note.endswith(_TAG_SUFFIX):
            head = note[: -len(_TAG_SUFFIX)]
            if len(head) == 7 and head[4] == "-" and head[:4].isdigit() and head[5:].isdigit():
                out.add((acct, head))
    return out


def candidates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    rows = conn.execute("SELECT * FROM transactions ORDER BY trade_date, id").fetchall()
    out: list[sqlite3.Row] = []
    for r in rows:
        try:
            snap = json.loads(r["fee_rule_snapshot"] or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(snap, dict) and incoherent(snap):
            out.append(r)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    ap.add_argument("--db", required=True, help="SQLite database path (never created)")
    ap.add_argument("--scope", required=True, choices=("all", "uncredited"),
                    help="all = every incoherent row; uncredited = skip months already credited")
    ap.add_argument("--id", dest="ids", type=int, action="append", default=None,
                    help="narrow to this transaction id (repeatable)")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = ap.parse_args(argv)
    started = time.monotonic()

    db = Path(args.db)
    if not db.is_file():
        print(f"拒絕：找不到資料庫 {db}（不會建立新檔）", file=sys.stderr)
        return _REFUSED
    conn = sqlite3.connect(str(db), isolation_level=None)  # explicit transactions below
    conn.row_factory = sqlite3.Row
    try:
        tables = _tables(conn)
        for needed in ("transactions", "ledger_audit"):
            if needed not in tables:
                print(f"拒絕：此資料庫沒有 {needed} 表，未寫入。", file=sys.stderr)
                return _REFUSED
        found = candidates(conn)
        credited = credited_months(conn)

        def month_of(r: sqlite3.Row) -> tuple[str, str]:
            return str(r["account_id"]), str(r["trade_date"])[:7]

        chosen = [r for r in found
                  if args.scope == "all" or month_of(r) not in credited]
        if args.ids:
            wanted = list(dict.fromkeys(args.ids))
            by_id = {int(r["id"]): r for r in chosen}
            missing = [i for i in wanted if i not in by_id]
            if missing:
                print("拒絕：以下 id 不是本範圍內「快照同時記錄打折與折讓款」的交易："
                      + "、".join(f"#{i}" for i in missing) + "（已清理過的列不會再出現）",
                      file=sys.stderr)
                print("未寫入任何資料。", file=sys.stderr)
                return _REFUSED
            chosen = [by_id[i] for i in wanted]

        print(f"快照同時記錄打折（discount<1）與折讓款（rebate_rate>0）的交易：共 {len(found)} 筆；"
              f"本次範圍（--scope {args.scope}"
              + (f"，--id {len(args.ids)} 筆" if args.ids else "") + f"）：{len(chosen)} 筆")
        chosen_ids = {int(r["id"]) for r in chosen}
        for r in found:
            snap = json.loads(r["fee_rule_snapshot"] or "{}")
            mark = "→ 清理" if int(r["id"]) in chosen_ids else "  保留"
            credit = "（該月已入帳折讓款）" if month_of(r) in credited else ""
            print(f"  {mark} #{r['id']} {r['trade_date']} {r['account_id']} {r['symbol']} "
                  f"{r['side']} 手續費 {r['fees']}  discount {snap.get('discount')}  "
                  f"rebate_rate {snap.get('rebate_rate')} → 0{credit}")
        if not chosen:
            print("沒有需要清理的列。")
            return 0
        if not args.apply:
            print(f"（試跑）將清理 {len(chosen)} 筆；加 --apply 才會寫入。"
                  "手續費／稅金等金額欄位不會變動。")
            return 0

        conn.executescript(_ACTION_LOG_DDL)
        now = datetime.now(_TZ).isoformat()
        conn.execute("BEGIN IMMEDIATE")
        try:
            for r in chosen:
                conn.execute(
                    "INSERT INTO ledger_audit (table_name, row_id, action, before_json, at) "
                    "VALUES ('transactions', ?, 'update', ?, ?)",
                    (str(r["id"]),
                     json.dumps(dict(r), ensure_ascii=False, default=str), now),
                )
                snap = json.loads(r["fee_rule_snapshot"] or "{}")
                conn.execute(
                    "UPDATE transactions SET fee_rule_snapshot = ? WHERE id = ?",
                    (json.dumps(cleaned(snap, at=now), ensure_ascii=False), r["id"]),
                )
            id_text = "、".join(f"#{r['id']}" for r in chosen)
            conn.execute(
                "INSERT INTO action_log (ts, username, method, path, action, status, "
                "duration_ms) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    now, "script", "SCRIPT",
                    f"scripts/clean_rebate_snapshots.py --scope {args.scope}",
                    f"費率快照清理（資料清理）{id_text}：rebate_rate → 0；{_RULING}",
                    200, int((time.monotonic() - started) * 1000),
                ),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        left = len(candidates(conn))
        print(f"已清理 {len(chosen)} 筆；清理後仍不一致的快照：{left} 筆。"
              "ledger_audit 已留存每筆原始內容，action_log 已記錄。")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
