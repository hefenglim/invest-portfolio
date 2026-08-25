"""READ-ONLY audit of the TW ETF flag (AI-D40, 2026-08-24) — reports, never writes.

Why this exists.  ``quick_register`` defaulted ``is_etf=False`` and the manual door's
auto-registration never passed the argument, so every TW symbol first seen by trading it
was recorded as *not* an ETF.  The registry is authoritative at both fee seams, so that
default then set the sell-tax rate: 現股 0.3% instead of ETF 0.1%, three times the tax, and
``fee_rule_snapshot`` recorded the assumed rate as though it were established.

The fix (``etf_flag_unknown``) only covers registrations made from now on.  The owner ruling
is explicit that **existing rows are never relabelled by the program** — a script that
"corrects" a ledger by guessing is the same defect wearing a repair badge.  So this reports
what may need a human answer, ranks it by whether the flag has already priced a real sell,
and stops there.

    .venv/Scripts/python scripts/audit_etf_flags.py                  # uses settings DB_PATH
    .venv/Scripts/python scripts/audit_etf_flags.py --db path/to.db

Exit code is 0 whatever it finds: this is a report, not a gate.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# A TW listing whose code starts with "00" is an ETF/ETN under the TWSE numbering scheme
# (0050, 0056, 006208, 00878 …). This is a HINT for a human to check, never an inference the
# program acts on — AI-D40's whole point is that the system does not guess this flag.
_TW_ETF_CODE_HINT = "00"
_NAME_HINTS = ("ETF", "指數", "基金", "元大台灣", "富邦台", "國泰永續")


def _hint(symbol: str, name: str) -> str:
    if symbol.startswith(_TW_ETF_CODE_HINT):
        return "代號以 00 開頭（TWSE ETF/ETN 編碼）"
    for token in _NAME_HINTS:
        if token in (name or ""):
            return f"名稱含「{token}」"
    return ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", help="SQLite ledger path (default: settings DB_PATH)")
    args = ap.parse_args(argv)

    if args.db:
        db = Path(args.db)
    else:
        from portfolio_dash.shared.config import get_settings
        db = Path(get_settings().db_path)
    if not db.exists():
        print(f"no ledger at {db}", file=sys.stderr)
        return 0

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(instruments)")}
    unknown_col = "etf_flag_unknown" if "etf_flag_unknown" in cols else "0"
    rows = conn.execute(
        f"SELECT symbol, name, is_etf, {unknown_col} AS unk FROM instruments "  # noqa: S608
        "WHERE market = 'TW' AND archived = 0 ORDER BY symbol"
    ).fetchall()

    print(f"TW instruments in {db.name}: {len(rows)}\n")
    suspect: list[tuple[str, str, str, int]] = []
    for r in rows:
        if r["is_etf"] or r["unk"]:
            continue    # answered as an ETF, or already marked unanswered — nothing to guess at
        sells = conn.execute(
            "SELECT COUNT(*) c FROM transactions WHERE symbol = ? AND side = 'SELL'",
            (r["symbol"],),
        ).fetchone()["c"]
        h = _hint(r["symbol"], r["name"] or "")
        if h or sells:
            suspect.append((r["symbol"], r["name"] or "", h, int(sells)))
    conn.close()

    if not suspect:
        print("沒有需要人工確認的 TW 標的。")
        return 0

    print("以下 TW 標的目前記為「非 ETF」。若其中有 ETF，過去的賣出已按 現股 0.3% 課稅"
          "（應為 0.1%）。\n請至標的管理逐一確認；本腳本不會修改任何資料。\n")
    print(f"{'代號':<10}{'已賣出筆數':<12}{'提示':<28}名稱")
    for symbol, name, h, sells in sorted(suspect, key=lambda x: (-x[3], x[0])):
        mark = f"{sells} ⚠" if sells else "0"
        print(f"{symbol:<10}{mark:<12}{h or '-':<28}{name}")
    print(f"\n共 {len(suspect)} 筆；其中 "
          f"{sum(1 for s in suspect if s[3])} 筆已有賣出紀錄（稅已按現股稅率落帳）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
