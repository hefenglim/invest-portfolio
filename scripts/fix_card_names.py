"""Correct stored insight cards that pair a registered code with the wrong name (item 8).

Owner ruling 2026-09-30: 「舊有資料就進行修正」 — with the registry's names and aliases as the
authority (``llm_insight/name_check.py``). A wrong name is replaced by the code's preferred
name (``shared/instrument_names.preferred_name``: a Chinese alias when the registered name is
not Chinese — 聯詠 (3008) → 大立光 (3008)). The CODE is kept: codes come from the data the model
was given, names from its memory, and on the demo every wrong pairing sat on a card whose own
subject was that code.

Where the wrong name's extent is known — the parenthesised name of 「代號 (名稱)」, or another
registered instrument's name — it is replaced as found. Where it is not (「持股以聯詠 (3008)」:
where does the name start?), only a name listed with ``--wrong-name`` is replaced; anything
else is reported as NEEDS REVIEW and left alone.

Dry run by default; ``--apply`` writes. Back the database up first — the script does not.

    python scripts/fix_card_names.py --db <path> [--wrong-name 聯詠 ...] [--apply]
        [--report out.json]
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portfolio_dash.llm_insight.name_check import (  # noqa: E402
    Mismatch,
    accepts,
    mismatches,
    registry_from_db,
)
from portfolio_dash.shared.instrument_names import (  # noqa: E402
    NamedInstrument,
    by_symbol,
    preferred_name,
)

_FIELDS = ("title", "summary", "body_md")


def _span(text: str, mm: Mismatch, wrong_names: list[str]) -> tuple[int, int] | None:
    """The wrong name's extent in *text*, or None when it cannot be told."""
    if mm.span is not None:
        return mm.span
    for name in sorted(wrong_names, key=len, reverse=True):
        for opener in ("(", "（"):
            for gap in ("", " "):
                needle = f"{name}{gap}{opener}{mm.code}"
                at = text.find(needle)
                if at >= 0:
                    return (at, at + len(name))
    return None


def _bare_reviewed(text: str, registry: list[NamedInstrument], wrong_names: list[str]
                   ) -> tuple[NamedInstrument, int, int] | None:
    """A reviewed wrong name written right after its bare code (「2603 萬海：…」), or None.

    The checker reads a bare code only when ANOTHER registered instrument's name follows it —
    ordinary words follow a bare code far more often than names — so an unregistered wrong
    name there is invisible to it. A name the owner reviewed as wrong is not: it is replaced
    wherever it directly follows a registered code it is not a name of (聯詠 stays right
    after 3034, were 3034 registered)."""
    for inst in registry:
        for name in sorted(wrong_names, key=len, reverse=True):
            if accepts(name, inst):
                continue
            m = re.search(rf"(?<![A-Za-z0-9.]){re.escape(inst.symbol)}[ \t]+{re.escape(name)}",
                          text)
            if m:
                return inst, m.end() - len(name), m.end()
    return None


def fix_field(text: str, registry: list[NamedInstrument], wrong_names: list[str]
              ) -> tuple[str, list[dict[str, str]], list[dict[str, str]]]:
    """(fixed text, changes made, pairings left for review)."""
    index = by_symbol(registry)
    changes: list[dict[str, str]] = []
    for _ in range(50):                      # one fix per pass: spans move after each edit
        pending = mismatches(text, registry)
        fixable = [(mm, _span(text, mm, wrong_names)) for mm in pending]
        target = next(((mm, sp) for mm, sp in fixable if sp is not None), None)
        if target is None:
            bare = _bare_reviewed(text, registry, wrong_names)
            if bare is None:
                return text, changes, [{"code": mm.code, "written": mm.written}
                                       for mm in pending]
            inst, start, end = bare
            code = inst.symbol
        else:
            mm, (start, end) = target
            inst = index.get(mm.code) or index[mm.code.split(".")[0]]
            code = mm.code
        new_name = preferred_name(inst)
        changes.append({"code": code, "from": text[start:end], "to": new_name,
                        "context": text[max(0, start - 12):end + 10]})
        text = text[:start] + new_name + text[end:]
    raise RuntimeError("more than 50 corrections in one field — stopping")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True)
    ap.add_argument("--wrong-name", action="append", default=[],
                    help="a reviewed wrong name whose extent the text cannot show")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--report")
    args = ap.parse_args(argv)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    registry = registry_from_db(conn)
    report: dict[str, object] = {"applied": args.apply, "cards": []}
    fixed_cards = review = 0
    for row in conn.execute("SELECT id, title, summary, body_md FROM insights ORDER BY id"):
        entry: dict[str, object] = {"id": row["id"], "changes": [], "review": []}
        new_values: dict[str, str] = {}
        for field in _FIELDS:
            text = row[field] or ""
            fixed, changes, left = fix_field(text, registry, args.wrong_name)
            for c in changes:
                c["field"] = field
            entry["changes"] += changes          # type: ignore[operator]
            entry["review"] += [dict(x, field=field) for x in left]  # type: ignore[operator]
            if fixed != text:
                new_values[field] = fixed
        if entry["changes"] or entry["review"]:
            report["cards"].append(entry)        # type: ignore[union-attr]
            fixed_cards += bool(entry["changes"])
            review += len(entry["review"])       # type: ignore[arg-type]
        if new_values and args.apply:
            sets = ", ".join(f"{f} = ?" for f in new_values)
            conn.execute(f"UPDATE insights SET {sets} WHERE id = ?",
                         (*new_values.values(), row["id"]))
    if args.apply:
        conn.commit()
    changes_total = sum(len(c["changes"]) for c in report["cards"])  # type: ignore[union-attr,misc]
    report["summary"] = {"cards_changed": fixed_cards, "corrections": changes_total,
                         "needs_review": review}
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.report:
        Path(args.report).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
