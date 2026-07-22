#!/usr/bin/env python3
"""Pre/post Moomoo-merge reconciliation harness — the real-data continuity proof.

The stress oracle clean-rooms *fresh* synthetic DBs; it can never prove that running the
one-time Moomoo account merge (``data_ingestion.moomoo_merge.migrate_moomoo_accounts``) on
the ACTUAL production ledger changes nothing but account labels. This harness does exactly
that: it captures every money-of-record figure a dashboard would show — plus the raw ledger
aggregates — BEFORE and AFTER the merge, and asserts they are identical under an "alias fold"
(the two legacy accounts ``moomoo_my_us`` / ``moomoo_my_my`` collapse into ``moomoo_my``).

It never fetches, never migrates the input, and never writes the file it reads:

* ``snapshot`` opens the DB **read-only** (SQLite URI ``mode=ro`` + a write-denying
  authorizer) and captures a canonical JSON snapshot (Decimal values as STRINGS, keys
  sorted → byte-stable output). The clock (``--as-of``) is injected so PRE and POST value at
  the SAME instant against the SAME stored prices.
* ``diff`` compares two snapshots under the alias fold and exits 0 iff equal.
* ``run`` is the convenience wrapper: it copies the DB to a temp file, snapshots (pre),
  migrates the COPY, snapshots (post), diffs, and prints PASS/FAIL — never touching the input.

What is captured
----------------
Engine figures (through the REAL modules — no math is re-implemented here):

* KPIs (``portfolio.dashboard.build_dashboard``): total market value, total return + rate,
  realized / unrealized totals, XIRR + its window, reporting FX realized / unrealized.
* Per-holding rows keyed (account_id → symbol): shares, original/adjusted cost totals,
  market value — the additive aggregates a weighted-average book is built from.
* ``fx.by_account``: home/foreign currency, avg rate, spot, foreign cash + stock value,
  realized/unrealized FX — plus the reporting-currency FX rollup.
* Portfolio TWR terminal index value (``portfolio.twr.twr_index`` over the same daily NAV
  series the trend card plots) and dividend totals (by-currency / TTM / by-year).

  Note on tax totals: the annual tax package's realized-gains / dividend / realized-FX
  subtotals are, by construction, the SAME figures already captured here
  (``realized_by_currency`` == book realized; ``dividends.total_by_currency``; per-account
  realized FX). Re-invoking ``export.tax`` would add a year-cut + a zip round-trip without a
  new money-of-record number, so it is deliberately NOT called (see "Decisions").

Raw SQL aggregates (independent of the engine):

* per-(account_id, ccy) ``cash_movements`` totals (exact Decimal sums);
* per-account row counts of the four flow tables + ``opening_inventory``;
* the ``accounts`` table dump (kept for evidence; see the fold rules below).

The alias fold (equality rule)
------------------------------
``moomoo_my_us`` and ``moomoo_my_my`` fold into ``moomoo_my``; EVERYTHING ELSE must be
EXACTLY string-equal:

* cash sums (per ccy) and row counts under the legacy ids are **summed** into ``moomoo_my``;
* per-holding rows re-key to ``moomoo_my``; if the same symbol ever appears under BOTH legacy
  accounts (post-merge ``build_book`` would combine them into one weighted-average row), the
  additive fields (shares, cost totals, market value) are **summed** — the exact identity a
  combined book produces (weighted-average cost is additive in shares & total cost);
* the single legacy ``fx.by_account`` entry (only ``moomoo_my_us`` has one — ``moomoo_my_my``
  settles == funds) **re-keys** to ``moomoo_my`` and must match field-for-field;
* the ``accounts`` dump legitimately changes (two rows → one, new currencies) — it is the
  migration's whole point. Equality is therefore checked as the folded **set of account ids**
  plus **exact rows for every bystander account** (schwab / tw_broker …). The merged row's own
  columns (name/ccy/rule bindings) are the MIGRATION's contract, proven by
  ``tests/data_ingestion/test_moomoo_merge.py`` (self-check V.c), not re-litigated here.

Money-of-record KPIs, realized-by-currency, dividend totals and TWR are portfolio-wide
reporting-currency aggregates — account-agnostic — so they must be EXACTLY equal with no fold.

Usage
-----
    python scripts/merge_reconcile.py snapshot <db.sqlite> <out.json> \
        --as-of 2026-07-22T00:00:00 [--reporting TWD]
    python scripts/merge_reconcile.py diff <pre.json> <post.json>
    python scripts/merge_reconcile.py run  <db.sqlite> \
        [--as-of ...] [--reporting TWD] [--pre-out pre.json] [--post-out post.json]

Exit codes: 0 = reconciled / equal, 1 = mismatch (each mismatch path is printed).

T14 deploy procedure (run on a COPY of the prod DB, immediately before deploy)
------------------------------------------------------------------------------
1. Produce a clean, consistent single-file copy of the prod ledger (checkpoints WAL):
       sqlite3 /path/prod.db ".backup /tmp/prod_pre.db"        # or: VACUUM INTO
2. Pick ONE fixed instant and reporting currency and snapshot the PRE copy:
       python scripts/merge_reconcile.py snapshot /tmp/prod_pre.db /tmp/pre.json \
           --as-of 2026-07-22T00:00:00 --reporting TWD
3. Copy the PRE copy and migrate the copy (never the original):
       cp /tmp/prod_pre.db /tmp/prod_post.db
       python -c "import sqlite3; from portfolio_dash.data_ingestion.moomoo_merge import \
           migrate_moomoo_accounts as m; c=sqlite3.connect('/tmp/prod_post.db'); \
           c.row_factory=sqlite3.Row; m(c); c.commit(); c.close()"
4. Snapshot the POST copy at the SAME --as-of / --reporting, then diff:
       python scripts/merge_reconcile.py snapshot /tmp/prod_post.db /tmp/post.json \
           --as-of 2026-07-22T00:00:00 --reporting TWD
       python scripts/merge_reconcile.py diff /tmp/pre.json /tmp/post.json
   (Steps 1-4 are exactly what ``run /tmp/prod_pre.db`` does in one shot; the manual form
   is preferred at T14 so ``pre.json`` / ``post.json`` are retained as deploy evidence.)
5. Keep pre.json / post.json (and the PASS output) as the deploy-record continuity evidence.

Constraints: stdlib + repo imports only (no new deps); Decimal STRINGS everywhere (never
float); deterministic, sorted-key output. This script is analysis tooling — it imports the
calculation core read-only and adds no money math of its own.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from portfolio_dash.data_ingestion.moomoo_merge import migrate_moomoo_accounts
from portfolio_dash.data_ingestion.schema import create_tables
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.portfolio.dashboard_models import DashboardData
from portfolio_dash.portfolio.twr import twr_index
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.wire import decimal_str

# --------------------------------------------------------------------------- constants

SCHEMA_VERSION = "merge-reconcile/1"
APP_TZ = ZoneInfo("Asia/Taipei")  # the app's day-anchor tz (deps.APP_TZ); naive --as-of adopts it

_MERGED_ID = "moomoo_my"
_LEGACY_IDS: tuple[str, ...] = ("moomoo_my_us", "moomoo_my_my")
# Per-account row-count tables (the 4 flow ledgers + opening_inventory).
_COUNT_TABLES: tuple[str, ...] = (
    "transactions",
    "dividends",
    "fx_conversions",
    "cash_movements",
    "opening_inventory",
)
# Holding fields that are additive across a same-symbol fold (weighted-avg cost is additive
# in shares & total cost; market_value = shares * price is likewise additive at a shared price).
_ADDITIVE_HOLDING_FIELDS: tuple[str, ...] = (
    "shares",
    "original_cost_total",
    "adjusted_cost_total",
    "market_value",
)

# SQLite authorizer action codes that mutate the database. Any of these on the read-only
# connection is denied (belt-and-suspenders atop mode=ro). Resolved via getattr so a
# typeshed/runtime missing constant can never crash import; isinstance narrows to int.
_WRITE_ACTION_NAMES: tuple[str, ...] = (
    "SQLITE_CREATE_INDEX", "SQLITE_CREATE_TABLE", "SQLITE_CREATE_TEMP_INDEX",
    "SQLITE_CREATE_TEMP_TABLE", "SQLITE_CREATE_TEMP_TRIGGER", "SQLITE_CREATE_TEMP_VIEW",
    "SQLITE_CREATE_TRIGGER", "SQLITE_CREATE_VIEW", "SQLITE_DELETE", "SQLITE_DROP_INDEX",
    "SQLITE_DROP_TABLE", "SQLITE_DROP_TEMP_INDEX", "SQLITE_DROP_TEMP_TABLE",
    "SQLITE_DROP_TEMP_TRIGGER", "SQLITE_DROP_TEMP_VIEW", "SQLITE_DROP_TRIGGER",
    "SQLITE_DROP_VIEW", "SQLITE_INSERT", "SQLITE_UPDATE", "SQLITE_ALTER_TABLE",
    "SQLITE_REINDEX", "SQLITE_ANALYZE", "SQLITE_CREATE_VTABLE", "SQLITE_DROP_VTABLE",
    "SQLITE_ATTACH", "SQLITE_DETACH",
)
_WRITE_ACTIONS: frozenset[int] = frozenset(
    a for a in (getattr(sqlite3, name, None) for name in _WRITE_ACTION_NAMES)
    if isinstance(a, int)
)


# --------------------------------------------------------------------------- read-only DB


def _deny_writes(
    action: int,
    _arg1: str | None,
    _arg2: str | None,
    _dbname: str | None,
    _source: str | None,
) -> int:
    """SQLite authorizer: DENY every write action, allow reads/pragmas/functions."""
    if action in _WRITE_ACTIONS:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    """Open ``db_path`` strictly read-only (URI ``mode=ro`` + write-denying authorizer).

    ``mode=ro`` physically forbids writes at the SQLite level; the authorizer is an explicit
    second guarantee (the "assert no write"). Row factory matches the app so the store's
    name-based row access works. The caller must pass a clean single-file DB (WAL checkpointed
    — e.g. from ``.backup`` / ``VACUUM INTO``); ``run`` handles WAL consolidation for its copy.
    """
    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.set_authorizer(_deny_writes)
    return conn


# --------------------------------------------------------------------------- capture


def _dstr(value: Decimal | None) -> str | None:
    """Canonical Decimal wire string, None-preserving."""
    return None if value is None else decimal_str(value)


def _ccy_map(mapping: dict[Currency, Decimal]) -> dict[str, str]:
    """Currency-keyed Decimal map → sorted {ccy_value: decimal_string}."""
    return {
        ccy.value: decimal_str(amount)
        for ccy, amount in sorted(mapping.items(), key=lambda kv: kv[0].value)
    }


def _capture_engine(data: DashboardData) -> dict[str, Any]:
    """Money-of-record figures pulled straight from the computed DashboardData."""
    kpis = data.kpis
    port_index = twr_index(data.trend.points) if data.trend.available else []
    twr_terminal = _dstr(port_index[-1].value) if port_index else None

    holdings: dict[str, Any] = {}
    for h in data.holdings:
        holdings.setdefault(h.account_id, {})[h.symbol] = {
            "shares": _dstr(h.shares),
            "original_cost_total": _dstr(h.original_cost_total),
            "adjusted_cost_total": _dstr(h.adjusted_cost_total),
            "market_value": _dstr(h.market_value),
        }

    fx_by_account: dict[str, Any] | None
    fx_reporting: dict[str, Any] | None
    if data.fx is None:
        fx_by_account = None
        fx_reporting = None
    else:
        fx_by_account = {}
        for account_id, r in data.fx.by_account.items():
            fx_by_account[account_id] = {
                "home_ccy": r.home_ccy.value,
                "foreign_ccy": r.foreign_ccy.value,
                "avg_rate": _dstr(r.avg_rate),
                "current_spot": _dstr(r.current_spot),
                "foreign_cash": _dstr(r.foreign_cash),
                "foreign_stock_value": _dstr(r.foreign_stock_value),
                "realized_fx": _dstr(r.realized_fx),
                "unrealized_fx_stocks": _dstr(r.unrealized_fx_stocks),
                "unrealized_fx_cash": _dstr(r.unrealized_fx_cash),
            }
        fx_reporting = {
            "reporting_realized_fx": _dstr(data.fx.reporting_realized_fx),
            "reporting_unrealized_fx": _dstr(data.fx.reporting_unrealized_fx),
        }

    return {
        "kpis": {
            "total_market_value": _dstr(kpis.total_market_value),
            "total_return": _dstr(kpis.total_return),
            "total_return_rate": _dstr(kpis.total_return_rate),
            "realized_total": _dstr(kpis.realized_total),
            "unrealized_total": _dstr(kpis.unrealized_total),
            "xirr": _dstr(kpis.xirr),
            "xirr_window_days": kpis.xirr_window_days,
            "fx_realized": _dstr(kpis.fx_realized),
            "fx_unrealized": _dstr(kpis.fx_unrealized),
        },
        "realized_by_currency": _ccy_map(data.realized.by_currency),
        "dividends": {
            "total_by_currency": _ccy_map(data.dividends.total_by_currency),
            "ttm_net": _ccy_map(data.dividends.ttm_net),
            "by_year": {
                str(row.year): _ccy_map(row.by_currency) for row in data.dividends.by_year
            },
        },
        "twr_terminal": twr_terminal,
        "twr_points": len(port_index),
        "holdings": holdings,
        "fx_by_account": fx_by_account,
        "fx_reporting": fx_reporting,
    }


def _capture_raw(conn: sqlite3.Connection) -> dict[str, Any]:
    """Independent raw-SQL aggregates (never through the engine)."""
    cash: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for account_id, ccy, amount in conn.execute(
        "SELECT account_id, ccy, amount FROM cash_movements"
    ):
        cash[str(account_id)][str(ccy)] += Decimal(str(amount))
    cash_out = {
        account_id: {ccy: decimal_str(v) for ccy, v in sorted(per_ccy.items())}
        for account_id, per_ccy in cash.items()
    }

    row_counts: dict[str, dict[str, int]] = {}
    for table in _COUNT_TABLES:
        counts: dict[str, int] = {}
        for account_id, n in conn.execute(
            f"SELECT account_id, COUNT(*) FROM {table} GROUP BY account_id"  # noqa: S608 (const table)
        ):
            counts[str(account_id)] = int(n)
        row_counts[table] = counts

    accounts: dict[str, Any] = {}
    for r in conn.execute(
        "SELECT account_id, name, broker, settlement_ccy, funding_ccy, fee_rule_set, "
        "dividend_model FROM accounts"
    ):
        accounts[str(r["account_id"])] = {
            "name": r["name"],
            "broker": r["broker"],
            "settlement_ccy": r["settlement_ccy"],
            "funding_ccy": r["funding_ccy"],
            "fee_rule_set": r["fee_rule_set"],
            "dividend_model": r["dividend_model"],
        }

    return {
        "cash_by_account_ccy": cash_out,
        "row_counts": row_counts,
        "accounts": accounts,
    }


def snapshot_db(
    db_path: Path, *, as_of: datetime, reporting: Currency
) -> dict[str, Any]:
    """Capture a full canonical snapshot from a read-only view of ``db_path``.

    Opens the DB with :func:`_open_readonly` (mode=ro + write-deny authorizer), runs the real
    ``build_dashboard`` for engine figures, adds the independent raw aggregates, and returns a
    JSON-safe dict (Decimals already stringified). Never migrates, never writes.
    """
    conn = _open_readonly(db_path)
    try:
        data = build_dashboard(conn, now=as_of, reporting=reporting)
        engine = _capture_engine(data)
        raw = _capture_raw(conn)
    finally:
        conn.close()
    return {
        "meta": {
            "as_of": as_of.isoformat(),
            "reporting_currency": reporting.value,
            "schema_version": SCHEMA_VERSION,
        },
        "engine": engine,
        "raw": raw,
    }


# --------------------------------------------------------------------------- alias fold


def _fold_account_key(account_id: str) -> str:
    return _MERGED_ID if account_id in _LEGACY_IDS else account_id


def _sum_holding_fields(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Sum the additive holding aggregates of two same-symbol rows (None-preserving)."""
    out: dict[str, Any] = {}
    for field in _ADDITIVE_HOLDING_FIELDS:
        va, vb = a.get(field), b.get(field)
        if va is None and vb is None:
            out[field] = None
        elif va is None:
            out[field] = vb
        elif vb is None:
            out[field] = va
        else:
            out[field] = decimal_str(Decimal(va) + Decimal(vb))
    return out


def _fold_holdings(holdings: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for account_id, symbols in holdings.items():
        folded = _fold_account_key(account_id)
        dst = out.setdefault(folded, {})
        for symbol, fields in symbols.items():
            if symbol in dst:
                dst[symbol] = _sum_holding_fields(dst[symbol], fields)
            else:
                dst[symbol] = dict(fields)
    return out


def _fold_cash(cash: dict[str, Any]) -> dict[str, Any]:
    acc: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for account_id, per_ccy in cash.items():
        folded = _fold_account_key(account_id)
        for ccy, value in per_ccy.items():
            acc[folded][ccy] += Decimal(str(value))
    return {
        account_id: {ccy: decimal_str(v) for ccy, v in sorted(per_ccy.items())}
        for account_id, per_ccy in acc.items()
    }


def _fold_counts(counts: dict[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for account_id, n in counts.items():
        folded = _fold_account_key(account_id)
        out[folded] = out.get(folded, 0) + int(n)
    return out


def _fold_fx(fx: dict[str, Any] | None) -> dict[str, Any] | None:
    if fx is None:
        return None
    out: dict[str, Any] = {}
    for account_id, value in fx.items():
        folded = _fold_account_key(account_id)
        if folded in out and out[folded] != value:
            # Two legacy accounts each carrying an FX entry that disagree is impossible on
            # real data (only moomoo_my_us settles != funds); surface it loudly if it ever
            # happens rather than silently dropping one.
            out[folded] = {"__fold_conflict__": [out[folded], value]}
        else:
            out[folded] = value
    return out


def _fold_accounts(accounts: dict[str, Any]) -> dict[str, Any]:
    """Fold the accounts dump to a comparable form: folded id-set + exact bystander rows.

    The merged/legacy rows legitimately differ (that IS the migration); their correctness is
    the migration's own contract. Here we only prove no stray account appeared/vanished and
    that every unrelated account is byte-identical.
    """
    ids = sorted({_fold_account_key(account_id) for account_id in accounts})
    bystanders = {
        account_id: row
        for account_id, row in accounts.items()
        if account_id not in _LEGACY_IDS and account_id != _MERGED_ID
    }
    return {"account_ids": ids, "bystander_rows": bystanders}


def fold(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Normalize a snapshot under the alias fold so PRE and POST become directly comparable.

    Applied identically to both sides. On an already-migrated POST snapshot the fold is a
    no-op for the account keys (no legacy ids present), so folded-POST == raw-POST; folding
    PRE collapses the two legacy accounts into ``moomoo_my`` with the summed aggregates.
    """
    engine = dict(snapshot["engine"])
    engine["holdings"] = _fold_holdings(snapshot["engine"]["holdings"])
    engine["fx_by_account"] = _fold_fx(snapshot["engine"]["fx_by_account"])

    raw = dict(snapshot["raw"])
    raw["cash_by_account_ccy"] = _fold_cash(snapshot["raw"]["cash_by_account_ccy"])
    raw["row_counts"] = {
        table: _fold_counts(counts) for table, counts in snapshot["raw"]["row_counts"].items()
    }
    raw["accounts"] = _fold_accounts(snapshot["raw"]["accounts"])

    return {"meta": snapshot["meta"], "engine": engine, "raw": raw}


# --------------------------------------------------------------------------- diff


@dataclass(frozen=True)
class Mismatch:
    """One reconciliation mismatch: a JSON path and the two (folded) values that differ."""

    path: str
    pre: str
    post: str


def _short(value: Any, limit: int = 120) -> str:
    text = json.dumps(value, sort_keys=True, ensure_ascii=False) if isinstance(
        value, dict | list
    ) else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _deep_diff(a: Any, b: Any, path: str = "") -> list[Mismatch]:
    out: list[Mismatch] = []
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            sub = f"{path}.{key}" if path else str(key)
            if key not in a:
                out.append(Mismatch(sub, "<absent>", _short(b[key])))
            elif key not in b:
                out.append(Mismatch(sub, _short(a[key]), "<absent>"))
            else:
                out.extend(_deep_diff(a[key], b[key], sub))
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(Mismatch(path, f"list(len={len(a)})", f"list(len={len(b)})"))
        else:
            for i, (x, y) in enumerate(zip(a, b, strict=True)):
                out.extend(_deep_diff(x, y, f"{path}[{i}]"))
    elif a != b:
        out.append(Mismatch(path or "<root>", _short(a), _short(b)))
    return out


def diff_snapshots(pre: dict[str, Any], post: dict[str, Any]) -> list[Mismatch]:
    """Alias-fold both snapshots, then deep-diff. Empty list == reconciled (continuity proven)."""
    return _deep_diff(fold(pre), fold(post))


# --------------------------------------------------------------------------- run (copy+migrate)


@dataclass(frozen=True)
class ReconcileResult:
    pre: dict[str, Any]
    post: dict[str, Any]
    mismatches: list[Mismatch]
    migrated: bool


def _sidecars(db_path: Path) -> list[Path]:
    return [db_path.parent / (db_path.name + suffix) for suffix in ("-wal", "-shm")]


def _copy_db_files(src: Path, dst: Path) -> None:
    """Copy the main DB file + any WAL/SHM sidecars via the filesystem (never opens the input
    as a database — the input is only ever read, never written)."""
    shutil.copy2(src, dst)
    for side in _sidecars(src):
        if side.exists():
            shutil.copy2(side, dst.parent / (dst.name + side.name[len(src.name):]))


def _consolidate_wal(copy: Path) -> None:
    """Fold any WAL into the main file of the COPY and switch it to a rollback journal, so the
    read-only ``mode=ro`` open needs no write access. Touches only the temp copy."""
    conn = sqlite3.connect(copy)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.commit()
    finally:
        conn.close()
    for side in _sidecars(copy):
        if side.exists():
            side.unlink()


def _apply_migration(copy: Path) -> bool:
    """Run the real merge migration on the COPY; returns whether it performed the merge."""
    conn = sqlite3.connect(copy)
    conn.row_factory = sqlite3.Row
    try:
        migrated = migrate_moomoo_accounts(conn)
        conn.commit()
    finally:
        conn.close()
    return migrated


def _prep_schema(copy: Path) -> None:
    """Bring the COPY's schema up to the current release before the PRE snapshot.

    A DB produced by the PREVIOUS release lacks this release's additive tables/columns
    (e.g. ``account_market_rules``), and the engine readers assume the boot seam
    (``create_tables``) has run — exactly as the real app boot does before the merge
    migration. Mirroring bootstrap on the COPY (schema-only, idempotent) lets the PRE
    snapshot read a previous-release DB; it touches no account/ledger row the merge or
    the diff cares about. (Found live on the first prod-copy run, 2026-07-22.)
    """
    conn = sqlite3.connect(copy)
    conn.row_factory = sqlite3.Row
    try:
        create_tables(conn)
        conn.commit()
    finally:
        conn.close()


def run_reconcile(
    db_path: Path, *, as_of: datetime, reporting: Currency
) -> ReconcileResult:
    """Copy → schema-prep → snapshot(pre) → migrate copy → snapshot(post) → diff.

    The input file is never touched; the schema prep runs on the COPY only, mirroring
    the real boot order (bootstrap_db → merge migration)."""
    with tempfile.TemporaryDirectory(prefix="merge_reconcile_") as tmp:
        copy = Path(tmp) / "copy.db"
        _copy_db_files(db_path, copy)
        _consolidate_wal(copy)
        _prep_schema(copy)
        pre = snapshot_db(copy, as_of=as_of, reporting=reporting)
        migrated = _apply_migration(copy)
        post = snapshot_db(copy, as_of=as_of, reporting=reporting)
        mismatches = diff_snapshots(pre, post)
    return ReconcileResult(pre=pre, post=post, mismatches=mismatches, migrated=migrated)


# --------------------------------------------------------------------------- CLI


def _parse_as_of(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=APP_TZ)


def _parse_reporting(raw: str) -> Currency:
    return Currency(raw.upper())


def _write_json(snapshot: dict[str, Any], out_path: Path) -> None:
    out_path.write_text(
        json.dumps(snapshot, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _print_mismatches(mismatches: list[Mismatch]) -> None:
    print(f"FAIL: {len(mismatches)} mismatch(es):")
    for m in mismatches:
        print(f"  {m.path}: pre={m.pre} post={m.post}")


def _cmd_snapshot(args: argparse.Namespace) -> int:
    snapshot = snapshot_db(
        Path(args.db_path),
        as_of=_parse_as_of(args.as_of),
        reporting=_parse_reporting(args.reporting),
    )
    _write_json(snapshot, Path(args.out))
    print(f"snapshot written: {args.out}")
    return 0


def _cmd_diff(args: argparse.Namespace) -> int:
    pre = json.loads(Path(args.pre).read_text(encoding="utf-8"))
    post = json.loads(Path(args.post).read_text(encoding="utf-8"))
    mismatches = diff_snapshots(pre, post)
    if mismatches:
        _print_mismatches(mismatches)
        return 1
    print("PASS: snapshots reconcile under the alias fold (merge changed only account labels).")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    as_of = _parse_as_of(args.as_of) if args.as_of else datetime.now(APP_TZ)
    result = run_reconcile(
        Path(args.db_path), as_of=as_of, reporting=_parse_reporting(args.reporting)
    )
    if args.pre_out:
        _write_json(result.pre, Path(args.pre_out))
    if args.post_out:
        _write_json(result.post, Path(args.post_out))
    print(f"as-of={as_of.isoformat()} reporting={args.reporting} migrated={result.migrated}")
    if not result.migrated:
        print("note: migration was a no-op (no legacy Moomoo accounts present — already merged?)")
    if result.mismatches:
        _print_mismatches(result.mismatches)
        return 1
    print("PASS: real-data merge reconciles (only account labels changed).")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="merge_reconcile",
        description="Pre/post Moomoo-merge reconciliation harness (read-only continuity proof).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("snapshot", help="capture a read-only snapshot to JSON")
    sp.add_argument("db_path", help="path to the SQLite ledger (opened read-only)")
    sp.add_argument("out", help="output JSON path")
    sp.add_argument("--as-of", required=True, help="ISO datetime; naive adopts Asia/Taipei")
    sp.add_argument("--reporting", default="TWD", help="reporting currency (default TWD)")
    sp.set_defaults(func=_cmd_snapshot)

    dp = sub.add_parser("diff", help="diff two snapshots under the alias fold")
    dp.add_argument("pre", help="pre-migration snapshot JSON")
    dp.add_argument("post", help="post-migration snapshot JSON")
    dp.set_defaults(func=_cmd_diff)

    rp = sub.add_parser("run", help="copy → snapshot → migrate copy → snapshot → diff")
    rp.add_argument("db_path", help="path to the SQLite ledger (never modified)")
    rp.add_argument("--as-of", default=None, help="ISO datetime; defaults to now (Asia/Taipei)")
    rp.add_argument("--reporting", default="TWD", help="reporting currency (default TWD)")
    rp.add_argument("--pre-out", default=None, help="optional: write the pre snapshot here")
    rp.add_argument("--post-out", default=None, help="optional: write the post snapshot here")
    rp.set_defaults(func=_cmd_run)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    func: Any = args.func
    result: int = func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
