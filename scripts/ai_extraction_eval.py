#!/usr/bin/env python
"""W4 (AI-D20): run the AI-extraction accuracy corpus against a LIVE model and report.

The corpus (``tests/golden/ai_extraction/cases.json``) is synthetic and hand-authored; this
runner feeds each case's input through the REAL door (``ai_agents_input`` — prompt, parse,
per-kind CSV) and compares the commit-level CSV fields against the ground truth. It exists
so a prompt edit is measured, not blind (spec §1: 「改提示詞是盲改」).

MANUAL ONLY — never in CI: it is non-deterministic, costs tokens, and pytest-socket bans
the network. Two modes:

* self-contained (default): an in-memory DB is seeded with the three canonical accounts and
  ONE model built from flags/env — nothing touches any real ledger:
      python scripts/ai_extraction_eval.py --model-name google/gemini-2.5-flash \
          --provider openrouter --api-key-env OPENROUTER_KEY
* ``--db PATH``: use an existing database's LLM configuration and account catalog (usage
  rows are written there — they are legitimate, attributable spend).

Report: per-case verdicts, per-field hit rates overall and per kind, and — listed
SEPARATELY, per AI-D20 — the cash-``kind`` / ``daytrade`` / ``short_sale`` mislabel rates
(the only fields that move money with no error raised), plus missing/spurious rows and the
unparsed confession recall. Thresholds (``--min-field-hit`` etc.) default to OFF: pin them
to the first baseline run's numbers, not to a guess — an unmeasured threshold is the same
blindness with a number attached.
"""
# mypy: ignore-errors

import argparse
import csv
import io
import json
import os
import sqlite3
import sys
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.agents import ai_agents_input
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.validate import CashPool
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.shared.llm_config import (
    LLMRole,
    ModelConfig,
    add_topup,
    ensure_llm_seeded,
    set_role,
    upsert_model,
)

_KIND_TO_IMPORT = {"txn": "transactions", "div": "dividends", "cash": "cash"}
#: Fields compared by VALUE (Decimal), not by string — "210.50" and "210.5" are one number.
_NUMERIC = {"shares", "price", "gross", "withholding", "net",
            "reinvest_shares", "reinvest_price", "amount", "acq_home_amount"}


def _rich_pool(account_id: str, ccy: object, **kw: object) -> CashPool:
    return CashPool(balance=Decimal("999999999"), low=Decimal("999999999"))


def _field_eq(name: str, expected: str, actual: str) -> bool:
    if name in _NUMERIC:
        try:
            return Decimal(expected) == Decimal(actual)
        except InvalidOperation:
            return False
    return expected.strip() == actual.strip()


def _build_conn(args: argparse.Namespace) -> sqlite3.Connection:
    if args.db:
        conn = sqlite3.connect(args.db)
        conn.row_factory = sqlite3.Row
        return conn
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    bootstrap_db(conn)
    # `bootstrap_db` owns the LEDGER tables; `prices` / `fx_rates` belong to pricing's own
    # schema (data-and-pricing.md keeps that split deliberately). Without this line every
    # case that touches FX died as `OperationalError: no such table: fx_rates` and was
    # reported as a RUNNER ERROR — which took ALL THREE MY cases and the merged-account US
    # case out of the run. Measured 2026-08-28 on the first live baseline: 4 of the 6
    # "failures" were this, so MY extraction accuracy was not low, it was UNMEASURED.
    create_pricing_tables(conn)
    ensure_llm_seeded(conn)
    seed_accounts(conn)
    key = os.environ.get(args.api_key_env, "").strip()
    if not key:
        raise SystemExit(f"env var {args.api_key_env} is empty — the model needs its key")
    upsert_model(conn, ModelConfig(
        id="eval", model_alias="eval", provider=args.provider,
        model_name=args.model_name, api_key=key, vision=True, enabled=True,
    ))
    set_role(conn, LLMRole.DEFAULT, "eval")
    set_role(conn, LLMRole.VISION, "eval")
    add_topup(conn, Decimal("25"), note="ai_extraction_eval")
    return conn


def main() -> int:
    # The report carries zh + symbols (⚠・—); the Windows console defaults to cp1252 and
    # would crash the FINAL print after a full paid run — reconfigure first, not after.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cases", default="tests/golden/ai_extraction/cases.json")
    ap.add_argument("--db", default=None,
                    help="use an existing DB's LLM config + accounts instead of in-memory")
    ap.add_argument("--model-name", default=None,
                    help="provider-side model id (self-contained mode), e.g. "
                         "google/gemini-2.5-flash")
    ap.add_argument("--provider", default="openrouter")
    ap.add_argument("--api-key-env", default="OPENROUTER_KEY")
    ap.add_argument("--model-alias", default=None,
                    help="force this registry alias at the chain head (both modes)")
    ap.add_argument("--json", default=None, help="also dump the raw report to this path")
    ap.add_argument("--limit", type=int, default=None,
                    help="run only the first N cases (smoke mode)")
    ap.add_argument("--min-field-hit", type=float, default=None)
    ap.add_argument("--max-cash-kind-miss", type=int, default=None)
    ap.add_argument("--max-daytrade-miss", type=int, default=None)
    ap.add_argument("--max-short-miss", type=int, default=None)
    args = ap.parse_args()

    doc = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    cases = doc["cases"][: args.limit] if args.limit else doc["cases"]
    default_today = date.fromisoformat(doc["today_default"])
    if not args.db and not args.model_name:
        raise SystemExit("--model-name is required in self-contained mode (or pass --db)")

    conn = _build_conn(args)
    stats = {"hit": 0, "miss": 0, "missing_rows": 0, "spurious_rows": 0,
             "unparsed_hit": 0, "unparsed_miss": 0}
    per_kind = {k: {"hit": 0, "miss": 0} for k in ("txn", "div", "cash")}
    separate = {"cash kind": {"hit": 0, "miss": 0},
                "daytrade": {"hit": 0, "miss": 0},
                "short_sale": {"hit": 0, "miss": 0}}
    passed = 0
    failures: list[str] = []

    for i, case in enumerate(cases):
        today = date.fromisoformat(case.get("today", default_today.isoformat()))
        try:
            result = ai_agents_input(
                conn, case["input"], pool=_rich_pool, today=today,
                model_alias=args.model_alias)
        except Exception as exc:  # noqa: BLE001 — a corpus run must finish and report
            failures.append(f"{case['id']}: RUNNER ERROR {exc!r}")
            continue
        if result.error is not None:
            failures.append(f"{case['id']}: LLM degrade ({result.error.kind}) "
                            f"{result.error.message}")
            continue

        actual: dict[str, list[dict[str, str]]] = {}
        for kind, text in result.csv_texts.items():
            actual[kind] = [dict(r) for r in csv.DictReader(io.StringIO(text))]

        case_misses: list[str] = []
        expected = case["expect"]["rows"]
        by_kind: dict[str, list[dict[str, object]]] = {}
        for row in expected:
            by_kind.setdefault(row["kind"], []).append(row["fields"])
        for ukind, rows in by_kind.items():
            got = actual.get(_KIND_TO_IMPORT[ukind], [])
            if len(got) > len(rows):
                extra = len(got) - len(rows)
                stats["spurious_rows"] += extra
                case_misses.append(f"{ukind}: {extra} SPURIOUS row(s)")
            for pos, fields in enumerate(rows):
                if pos >= len(got):
                    stats["missing_rows"] += 1
                    stats["miss"] += len(fields)
                    per_kind[ukind]["miss"] += len(fields)
                    case_misses.append(f"{ukind}[{pos}]: row MISSING entirely")
                    continue
                for name, want in fields.items():
                    bucket = (separate["cash kind"] if ukind == "cash" and name == "kind"
                              else separate.get(name))
                    ok = _field_eq(name, want, got[pos].get(name, ""))
                    stats["hit" if ok else "miss"] += 1
                    per_kind[ukind]["hit" if ok else "miss"] += 1
                    if bucket is not None:
                        bucket["hit" if ok else "miss"] += 1
                    if not ok:
                        case_misses.append(
                            f"{ukind}[{pos}].{name}: want {want!r} got "
                            f"{got[pos].get(name, '')!r}")
        for kind, rows in actual.items():
            # rows of a kind the case did not expect at all are spurious too
            ukind = {v: k for k, v in _KIND_TO_IMPORT.items()}[kind]
            if ukind not in by_kind and rows:
                stats["spurious_rows"] += len(rows)
                case_misses.append(f"{ukind}: {len(rows)} SPURIOUS row(s)")

        confessed = " ".join(u.text + " " + u.reason for u in result.unparsed)
        for sub in case["expect"]["unparsed_contains"]:
            if sub in confessed:
                stats["unparsed_hit"] += 1
            else:
                stats["unparsed_miss"] += 1
                case_misses.append(f"unparsed: never confessed {sub!r}")

        if case_misses:
            failures.append(f"{case['id']}: FAIL — " + "; ".join(case_misses))
        else:
            passed += 1
        print(f"[{i + 1}/{len(cases)}] {case['id']}: "
              f"{'PASS' if not case_misses else 'FAIL'}")

    total = stats["hit"] + stats["miss"]
    hit_rate = (stats["hit"] / total) if total else 0.0
    print("\n================ AI extraction accuracy ================")
    print(f"cases: {passed}/{len(cases)} clean")
    print(f"field hit rate: {stats['hit']}/{total} = {hit_rate:.1%}")
    for k, b in per_kind.items():
        t = b["hit"] + b["miss"]
        print(f"  {k:>4}: {b['hit']}/{t} = {(b['hit'] / t if t else 0):.1%}")
    print("— the silent-money fields, separately (AI-D20) —")
    for name, b in separate.items():
        t = b["hit"] + b["miss"]
        print(f"  {name}: {b['hit']}/{t} hits, {b['miss']} misses")
    print(f"missing rows: {stats['missing_rows']} · spurious rows: {stats['spurious_rows']}")
    up_total = stats["unparsed_hit"] + stats["unparsed_miss"]
    print(f"unparsed confession recall: {stats['unparsed_hit']}/{up_total}")
    if failures:
        print("\n— failures —")
        for line in failures:
            print(" ", line)
    if args.json:
        Path(args.json).write_text(json.dumps({
            "cases": len(cases), "passed": passed, "stats": stats,
            "per_kind": per_kind, "separate": separate, "failures": failures,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    breached = []
    if args.min_field_hit is not None and hit_rate < args.min_field_hit:
        breached.append(f"field hit {hit_rate:.1%} < {args.min_field_hit:.1%}")
    if args.max_cash_kind_miss is not None and (
            separate["cash kind"]["miss"] > args.max_cash_kind_miss):
        breached.append("cash kind misses over threshold")
    if args.max_daytrade_miss is not None and (
            separate["daytrade"]["miss"] > args.max_daytrade_miss):
        breached.append("daytrade misses over threshold")
    if args.max_short_miss is not None and (
            separate["short_sale"]["miss"] > args.max_short_miss):
        breached.append("short_sale misses over threshold")
    if not any(v is not None for v in (args.min_field_hit, args.max_cash_kind_miss,
                                       args.max_daytrade_miss, args.max_short_miss)):
        print("\n⚠ no thresholds passed — report-only. Pin --min-field-hit & co. to THIS "
              "baseline once it is on record, not before (AI-D20).")
    for b in breached:
        print("GATE FAIL:", b)
    return 1 if breached else 0


if __name__ == "__main__":
    raise SystemExit(main())
