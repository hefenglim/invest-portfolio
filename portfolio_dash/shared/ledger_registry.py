"""The ledger table catalogue — ONE declaration per ledger table.

Four modules used to enumerate the ledger tables by hand, each taking a slightly
different slice:

* ``api/routers/db_stats.py`` — zh label + the oldest-record date column
* ``export/ledgers.py`` — which tables the export zip carries, and each one's CSV tab key
* ``data_ingestion/moomoo_merge.py`` — which tables carry ``account_id``, and which of
  those an account merge may relabel with a bare ``UPDATE``
* ``scripts/merge_reconcile.py`` — the per-account row-count tables

Every failure mode of a MISSED entry is silent: an account merge orphans rows on the dead
account id and the reconciliation still reports PASS (corporate-actions spec, §6.4). So
the enumeration lives here once and those four read it. Adding a ledger means adding one
row to :data:`LEDGER_TABLES` — and there is deliberately no registration hook, because a
second place to register is the same disease.

``shared/`` depends on nothing internal (``architecture.md``), so every layer — api,
export, data_ingestion, and a top-level script — reaches this without a lateral import.
"""

from dataclasses import dataclass
from typing import Literal

# How an account merge rewrites ``account_id`` on this table:
#   "update" — a bare ``UPDATE ... SET account_id`` is safe (surrogate PK, no
#              account-scoped UNIQUE, so a blind rewrite cannot collide)
#   "keyed"  — the table is keyed on (account_id, ...), so the merge needs its own
#              collision pre-check before rewriting
AccountRelabel = Literal["update", "keyed"]


@dataclass(frozen=True)
class LedgerTable:
    """One ledger table, and everything its four consumers need to know about it."""

    table: str
    label: str
    """zh display label (db-stats panel)."""
    date_col: str
    """The table's natural date column: oldest-record stat AND export range filter."""
    export_kind: str | None
    """Ledger-page tab key. ``None`` -> not individually exportable and NOT in the zip.

    One field, not two: the export zip is exactly the set of individually exportable
    ledgers. ``cash_movements`` is deliberately outside both today.

    ⚠ This sentence named a count until 2026-08-16 — in the very file whose existence is the
    argument against counting ledgers by hand. It was found by the guard written for the
    eight sites downstream (``tests/contract/test_ledger_count_not_hardcoded.py``), which is
    the whole case for having the guard rather than a careful reading.
    """
    account_relabel: AccountRelabel


# Declaration order is observable: it drives the db-stats 帳本 group order and the
# export zip's file order. Keep new ledgers at the end unless you mean to move them.
LEDGER_TABLES: tuple[LedgerTable, ...] = (
    LedgerTable("transactions", "交易帳本", "trade_date", "transactions", "update"),
    LedgerTable("dividends", "股利帳本", "date", "dividends", "update"),
    LedgerTable("fx_conversions", "換匯帳本", "date", "fx", "update"),
    LedgerTable("opening_inventory", "期初庫存", "build_date", "opening", "keyed"),
    LedgerTable("cash_movements", "資金收支", "date", None, "update"),
    # The 6th ledger (spec 2026-08-06). This ONE line is the whole registration: db-stats,
    # the export zip + its CSV tab, the Moomoo account merge and merge_reconcile.py all
    # derive from here. Surrogate PK, no account-scoped UNIQUE -> "update".
    LedgerTable("corporate_actions", "公司行動", "date", "actions", "update"),
)

TABLE_NAMES: tuple[str, ...] = tuple(t.table for t in LEDGER_TABLES)
"""Every ledger table name. All of them carry an ``account_id`` column."""

EXPORT_KINDS: dict[str, LedgerTable] = {
    t.export_kind: t for t in LEDGER_TABLES if t.export_kind is not None
}
"""Tab key -> table, in declaration order. Its values ARE the export zip's contents."""

PLAIN_RELABEL_TABLES: tuple[str, ...] = tuple(
    t.table for t in LEDGER_TABLES if t.account_relabel == "update"
)
"""Tables an account merge relabels with a bare UPDATE (no collision pre-check needed)."""
