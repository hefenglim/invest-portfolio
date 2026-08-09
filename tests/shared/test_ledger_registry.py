"""The ledger catalogue and its four consumers (corporate-actions spec §6.4).

Detection power — what each test catches, and when:

* :func:`test_every_account_scoped_schema_table_is_registered` fires the moment a NEW
  account-scoped ledger table lands in ``data_ingestion/schema.py`` without a registry
  entry. That is the exact failure the registry exists to prevent, and it used to be
  silent: an account merge orphaned the rows on the dead account id and the
  reconciliation still reported PASS.
* The four consumer tests assert the derived lists EQUAL the registry. On today's five
  ledgers a re-hardcoded copy would still match — but the moment a sixth ledger is
  declared, any consumer that stopped deriving fails here instead of in production.
  The two halves are only useful together.
"""

import importlib.util
import sqlite3
import sys
from pathlib import Path
from types import ModuleType

from portfolio_dash.data_ingestion import moomoo_merge
from portfolio_dash.data_ingestion.schema import create_tables
from portfolio_dash.export import ledgers as export_ledgers
from portfolio_dash.shared import ledger_registry
from portfolio_dash.shared.ledger_registry import LEDGER_TABLES

# Account-scoped tables in data_ingestion/schema.py that are deliberately NOT ledgers.
# Keep this list short and justified — every entry is a table a future reader might
# otherwise expect to see moved by an account merge.
_NOT_A_LEDGER = {
    "accounts",              # the account registry itself (relabelled, but not a ledger)
    "account_market_rules",  # per-account CONFIG, not a flow
    "ledger_audit",          # immutable history: its before_json must never be rewritten
}


def _schema_tables_with_account_id() -> set[str]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        create_tables(conn)
        names = [
            str(r["name"])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {
            n for n in names
            if any(str(c["name"]) == "account_id"
                   for c in conn.execute(f'PRAGMA table_info("{n}")'))
        }
    finally:
        conn.close()


def _load_merge_reconcile() -> ModuleType:
    """``scripts/merge_reconcile.py`` is a top-level script, not an importable package."""
    cached = sys.modules.get("merge_reconcile")
    if cached is not None:
        return cached
    path = Path(__file__).resolve().parents[2] / "scripts" / "merge_reconcile.py"
    spec = importlib.util.spec_from_file_location("merge_reconcile", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register BEFORE exec, so its dataclasses can resolve sys.modules[cls.__module__].
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_every_account_scoped_schema_table_is_registered() -> None:
    """A new ledger cannot ship unregistered — it fails HERE, by name."""
    unregistered = _schema_tables_with_account_id() - set(ledger_registry.TABLE_NAMES)
    assert unregistered <= _NOT_A_LEDGER, (
        f"account-scoped table(s) {sorted(unregistered - _NOT_A_LEDGER)} are in "
        "data_ingestion/schema.py but not in shared/ledger_registry.py — add a "
        "LedgerTable row, or add them to _NOT_A_LEDGER with a reason. Unregistered, "
        "an account merge orphans their rows and the reconciliation still says PASS."
    )


def test_registry_declares_only_tables_that_exist() -> None:
    """The reverse guard: a registry row naming a table nobody creates is a typo."""
    present = _schema_tables_with_account_id()
    assert set(ledger_registry.TABLE_NAMES) <= present


def test_export_zip_and_csv_kinds_derive_from_the_registry() -> None:
    exportable = [t for t in LEDGER_TABLES if t.export_kind is not None]
    assert export_ledgers._LEDGER_TABLES == [t.table for t in exportable]
    assert export_ledgers.LEDGER_KINDS == {
        t.export_kind: (t.table, t.date_col) for t in exportable
    }
    # cash_movements is deliberately outside the "four-ledger zip".
    assert "cash_movements" not in export_ledgers._LEDGER_TABLES


def test_db_stats_lists_every_ledger_under_the_帳本_category() -> None:
    from portfolio_dash.api.routers.db_stats import _PORTFOLIO_REGISTRY

    specs = {s.name: s for s in _PORTFOLIO_REGISTRY}
    for t in LEDGER_TABLES:
        assert t.table in specs, f"{t.table} missing from the db-stats registry"
        assert specs[t.table].label == t.label
        assert specs[t.table].category == "帳本"
        assert specs[t.table].date_col == t.date_col


def test_moomoo_merge_checks_and_relabels_every_ledger() -> None:
    assert set(ledger_registry.TABLE_NAMES) <= set(moomoo_merge._ACCOUNT_ID_TABLES)
    assert moomoo_merge._FLOW_TABLES == tuple(
        t.table for t in LEDGER_TABLES if t.account_relabel == "update"
    )
    # opening_inventory is account-keyed: relabelled at U2 behind a collision pre-check,
    # never by the blind UPDATE loop.
    assert "opening_inventory" not in moomoo_merge._FLOW_TABLES


def test_merge_reconcile_counts_every_ledger() -> None:
    module = _load_merge_reconcile()
    assert module._COUNT_TABLES == ledger_registry.TABLE_NAMES
