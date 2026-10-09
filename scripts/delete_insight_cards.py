"""Delete named insight cards — and every row that references them — with an audit trail.

DEF-046 (owner ruling 2026-09-24): an insight card produced by the R1 defect DEF-037 (an
account id handed to the per-symbol card as its "symbol", so the model invented a 「Moomoo
交易商警示」) is still listed under AI 洞察 › 持倉健診. The code was fixed in R2; this removes the
DATA, which the app itself never does (cards are append-only).

    .venv/Scripts/python scripts/delete_insight_cards.py --db path/to.db --id 192          # dry run
    .venv/Scripts/python scripts/delete_insight_cards.py --db path/to.db --id 192 --apply  # write

What it does, in order:

1. **Refuses** without ``--db`` (argparse), on a path that does not exist (``sqlite3.connect``
   would silently CREATE an empty database there), and on any ``--id`` that is not a card.
   An id that is gone but has a ``ledger_audit`` delete record is reported as already deleted
   and skipped — a re-run is a no-op, not an error.
2. **Refuses a card whose ``symbol`` is a registered instrument** unless
   ``--allow-registered-symbol`` is given. The cards this exists for carry a symbol that is
   NOT a symbol (an account id); a real holding's card reaching this script is the signature
   of a wrong id — the exact mix-up that put a legitimate 2884 card (#207) in the R2 handoff.
3. Prints every card and every DEPENDENT row: every table with an ``insight_id`` column is
   found by reading the schema (today: ``insight_evaluations`` — the card's scoring rows), so
   a table added later is covered without editing this file.
4. Dry run by default. With ``--apply``, in ONE transaction: each dependent row, then the
   card, is copied into ``ledger_audit`` (``action='delete'``, ``before_json`` = the whole
   row, so the content stays recoverable from the database itself) and deleted; one
   ``action_log`` row records the operation (系統操作記錄). Any failure rolls all of it back.
5. Prints before/after row counts for every table it touches.

Exit code: 0 = done (or nothing to do), 2 = refused (nothing written).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("Asia/Taipei")
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
_REFUSED = 2


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def dependent_tables(conn: sqlite3.Connection) -> list[str]:
    """Every table carrying an ``insight_id`` column — read from the schema, never listed."""
    out: list[str] = []
    for name in sorted(_tables(conn)):
        cols = {str(r[1]) for r in conn.execute(f'PRAGMA table_info("{name}")')}
        if "insight_id" in cols:
            out.append(name)
    return out


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def _already_deleted(conn: sqlite3.Connection, card_id: int) -> bool:
    if "ledger_audit" not in _tables(conn):
        return False
    row = conn.execute(
        "SELECT 1 FROM ledger_audit WHERE table_name = 'insights' AND row_id = ? "
        "AND action = 'delete' LIMIT 1",
        (str(card_id),),
    ).fetchone()
    return row is not None


def _registered(conn: sqlite3.Connection, symbol: str | None) -> bool:
    if not symbol or "instruments" not in _tables(conn):
        return False
    return conn.execute(
        "SELECT 1 FROM instruments WHERE symbol = ? LIMIT 1", (symbol,)
    ).fetchone() is not None


def _is_account(conn: sqlite3.Connection, symbol: str | None) -> bool:
    if not symbol or "accounts" not in _tables(conn):
        return False
    return conn.execute(
        "SELECT 1 FROM accounts WHERE account_id = ? LIMIT 1", (symbol,)
    ).fetchone() is not None


def _task_name(conn: sqlite3.Connection, insight_type_id: int) -> str:
    if "insight_types" not in _tables(conn):
        return "?"
    row = conn.execute(
        "SELECT name FROM insight_types WHERE id = ?", (insight_type_id,)
    ).fetchone()
    return str(row[0]) if row is not None else "?"


def _audit(conn: sqlite3.Connection, table: str, row: sqlite3.Row, at: str) -> None:
    conn.execute(
        "INSERT INTO ledger_audit (table_name, row_id, action, before_json, at) "
        "VALUES (?, ?, 'delete', ?, ?)",
        (table, str(row["id"]), json.dumps(dict(row), ensure_ascii=False, default=str), at),
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    ap.add_argument("--db", required=True, help="SQLite database path (never created)")
    ap.add_argument("--id", dest="ids", type=int, action="append", required=True,
                    help="insight card id to delete (repeatable)")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--allow-registered-symbol", action="store_true",
                    help="allow deleting a card whose symbol IS a registered instrument")
    ap.add_argument("--reason", default="owner 裁定刪除錯誤洞察卡",
                    help="recorded in the action log row")
    args = ap.parse_args(argv)
    started = time.monotonic()

    db = Path(args.db)
    if not db.is_file():
        print(f"拒絕：找不到資料庫 {db}（不會建立新檔）", file=sys.stderr)
        return _REFUSED
    conn = sqlite3.connect(str(db), isolation_level=None)  # explicit transactions below
    conn.row_factory = sqlite3.Row
    try:
        if "insights" not in _tables(conn):
            print("拒絕：此資料庫沒有 insights 表", file=sys.stderr)
            return _REFUSED
        deps = dependent_tables(conn)
        ids = list(dict.fromkeys(args.ids))
        cards: list[sqlite3.Row] = []
        refused: list[str] = []
        for cid in ids:
            row = conn.execute("SELECT * FROM insights WHERE id = ?", (cid,)).fetchone()
            if row is None:
                if _already_deleted(conn, cid):
                    print(f"#{cid}：已刪除（ledger_audit 有刪除紀錄），略過")
                else:
                    refused.append(f"#{cid} 不存在")
                continue
            if _registered(conn, row["symbol"]) and not args.allow_registered_symbol:
                refused.append(
                    f"#{cid} 的 symbol「{row['symbol']}」是已註冊標的 — 這通常代表 id 填錯；"
                    "確定要刪請加 --allow-registered-symbol"
                )
                continue
            cards.append(row)
        if refused:
            for msg in refused:
                print(f"拒絕：{msg}", file=sys.stderr)
            print("未寫入任何資料。", file=sys.stderr)
            return _REFUSED
        if not cards:
            print("沒有需要刪除的卡片。")
            return 0

        plan: list[tuple[str, sqlite3.Row]] = []
        for card in cards:
            sym = card["symbol"]
            kind = (
                "帳戶代號（不是標的）" if _is_account(conn, sym)
                else "已註冊標的" if _registered(conn, sym) else "未註冊"
            )
            print(f"卡片 #{card['id']}  任務 {card['insight_type_id']}"
                  f"「{_task_name(conn, int(card['insight_type_id']))}」")
            print(f"  symbol={sym!r}（{kind}）  建立 {card['created_at']}")
            print(f"  標題：{card['title']}")
            trig = card["trigger_json"] if "trigger_json" in card.keys() else None
            print(f"  trigger_json={trig!r}")
            for table in deps:
                rows = conn.execute(
                    f'SELECT * FROM "{table}" WHERE insight_id = ? ORDER BY id', (card["id"],)
                ).fetchall()
                print(f"  依附列 {table}：{len(rows)} 筆"
                      + (f"（id {', '.join(str(r['id']) for r in rows)}）" if rows else ""))
                plan.extend((table, r) for r in rows)
            plan.append(("insights", card))

        touched = [*deps, "insights"]
        before = {t: _count(conn, t) for t in touched}
        print("刪除前筆數：" + "、".join(f"{t} {n}" for t, n in before.items()))
        if not args.apply:
            print(f"（試跑）將刪除 {len(plan)} 列；加 --apply 才會寫入。")
            return 0
        if "ledger_audit" not in _tables(conn):
            print("拒絕：此資料庫沒有 ledger_audit 表，無法留下稽核紀錄，未寫入。", file=sys.stderr)
            return _REFUSED

        conn.executescript(_ACTION_LOG_DDL)
        now = datetime.now(_TZ).isoformat()
        id_text = "、".join(f"#{c['id']}" for c in cards)
        conn.execute("BEGIN IMMEDIATE")
        try:
            for table, row in plan:
                _audit(conn, table, row, now)
                conn.execute(f'DELETE FROM "{table}" WHERE id = ?', (row["id"],))
            conn.execute(
                "INSERT INTO action_log (ts, username, method, path, action, status, "
                "duration_ms) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    now, "script", "SCRIPT",
                    "scripts/delete_insight_cards.py " + " ".join(
                        f"--id {c['id']}" for c in cards),
                    f"洞察卡刪除（資料清理）{id_text}：{args.reason}",
                    200, int((time.monotonic() - started) * 1000),
                ),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        after = {t: _count(conn, t) for t in touched}
        print("刪除後筆數：" + "、".join(f"{t} {n}" for t, n in after.items()))
        print(f"已刪除 {len(plan)} 列（{id_text} 及其依附列），ledger_audit 已留存原始內容，"
              "action_log 已記錄。")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
