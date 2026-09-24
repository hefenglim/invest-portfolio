"""Import provenance — which file a ledger row came from, and whether it is already here.

``data-and-pricing.md`` requires *"Source is recorded per row, so data provenance is always
auditable."*  That held for ``prices`` and, until 2026-08-13, for none of the ledgers.  Two
consequences, both of which only bite at import scale:

* **Re-importing the same export duplicated the entire ledger.**  There was no idempotency
  key, so a re-run after a partial failure — or simply after forgetting — silently doubled
  every row.
* **No number could be traced back to a statement line**, so a reconciliation difference
  could be seen but not localised.  The failure mode is "the totals disagree and nobody can
  say which row".

This matters now because the first real ledger this project will hold arrives as a
four-figure broker import.  An import you cannot undo is one you cannot safely try.

Two records, deliberately at different grains
---------------------------------------------
**The batch is a property of the FILE.**  It is created from the uploaded text itself (its
name and SHA-256), so nothing upstream has to invent or carry an id — an offline converter
emits ordinary template CSVs and provenance still works.  Deleting a batch deletes exactly
the rows it wrote, which is what makes trying an import reversible.

**The row hash is a property of the ROW.**  It is DERIVED from the row's own normalized
content rather than read from a column, for the same reason: a column would have to be
filled by whoever produced the file, and the case that needs protecting most is the plain
re-upload of a file nobody annotated.

⚠ **The hash includes an occurrence ordinal, and that is load-bearing.**  Two genuinely
identical rows in one file — the same $50 deposit entered twice on the same day, which is a
real thing a statement contains — hash to the same content.  Without the ordinal the second
would be skipped as a duplicate of the first, and the import would silently drop a real
movement while reporting success.  The ordinal counts *among identical rows*, not
absolutely, so inserting an unrelated row at the top of the file does not renumber anything
and a re-upload still matches every prior row.

⚠ Stated limitation: the occurrence ordinal is per-FILE, not per-ledger
-----------------------------------------------------------------------
Two identical rows in ONE file both import (that is the ordinal's job).  Two identical rows
in **two different files** do not: the second is skipped as a duplicate of the first.

That is the right default for the case this exists for — the assessed broker export arrives
as five files with **overlapping date windows**, and de-duplicating the overlap is a
feature, not a loss.  It is nonetheless a wrong answer for a genuinely separate movement
that happens to match an earlier import cell-for-cell.  The escape hatch is the manual form,
which carries no provenance and is unaffected; the response also reports ``duplicates`` so
the skip is visible rather than silent.

Making the hash file-specific (folding the source digest in) would fix that case and break a
more common one: re-uploading an export with a single corrected row would then re-import
every unchanged row alongside it.  Left as recorded behaviour rather than decided quietly.

``opening_inventory`` is deliberately EXCLUDED
----------------------------------------------
It has a composite primary key ``(account_id, symbol)`` and its writer UPSERTs, so a
re-import replaces rather than duplicates — it is already idempotent by construction, and it
has no surrogate id to stamp.  Excluding it is a property of that table's shape, not an
oversight; the five append-only ledgers are the ones that needed this.
"""

import hashlib
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol

from portfolio_dash.data_ingestion.store import (
    StoredCorporateAction,
    audit_source,
    delete_cash_movement,
    delete_dividend,
    delete_fx_conversion,
    delete_transaction,
    list_corporate_actions,
)
from portfolio_dash.shared.clock import app_now

#: import kind -> the ledger table its writer inserts into. ``openings`` is absent on
#: purpose (see the module docstring). A kind missing from this map imports exactly as it
#: did before provenance existed — no stamping, no idempotency check — rather than failing,
#: so adding a template kind is not silently blocked on updating this file.
TABLE_BY_KIND: dict[str, str] = {
    "transactions": "transactions",
    "dividends": "dividends",
    "fx": "fx_conversions",
    "cash": "cash_movements",
    "corporate_actions": "corporate_actions",
}

_UNIT = "\x1f"
_RECORD = "\x1e"


def source_sha256(csv_text: str) -> str:
    """The uploaded file's digest — the batch's identity, computed from the text itself."""
    return hashlib.sha256(csv_text.encode("utf-8")).hexdigest()


def row_hash(kind: str, raw: dict[str, str], occurrence: int) -> str:
    """A stable per-row idempotency key.

    Derived from *kind*, the row's *occurrence* among identical rows in the same file, and
    the row's cells in sorted-key order — so it is independent of column ORDER (a user who
    rearranges columns in Excel has not created new rows) and of position in the file.
    """
    canon = _UNIT.join(f"{k}={raw.get(k, '')}" for k in sorted(raw))
    return hashlib.sha256(
        f"{kind}{_RECORD}{occurrence}{_RECORD}{canon}".encode()
    ).hexdigest()


def row_hashes(kind: str, raws: Iterable[dict[str, str]]) -> list[str]:
    """:func:`row_hash` for a whole file, assigning each row its occurrence ordinal."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for raw in raws:
        base = _UNIT.join(f"{k}={raw.get(k, '')}" for k in sorted(raw))
        n = seen.get(base, 0)
        seen[base] = n + 1
        out.append(row_hash(kind, raw, n))
    return out


def existing_hashes(
    conn: sqlite3.Connection, kind: str, hashes: Iterable[str]
) -> set[str]:
    """Which of *hashes* this ledger already holds — the rows a re-import must skip.

    Returns an empty set for a kind with no provenance table, so an unmapped kind behaves
    exactly as it did before this module existed.
    """
    table = TABLE_BY_KIND.get(kind)
    wanted = [h for h in hashes]
    if table is None or not wanted:
        return set()
    found: set[str] = set()
    # Chunked so a four-figure import stays well inside SQLite's variable limit (999).
    for i in range(0, len(wanted), 400):
        chunk = wanted[i : i + 400]
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT source_row_hash FROM {table} "  # noqa: S608 - table from a fixed map
            f"WHERE source_row_hash IN ({marks})",
            chunk,
        ).fetchall()
        found.update(r[0] for r in rows)
    return found


def open_batch(
    conn: sqlite3.Connection,
    *,
    kind: str,
    csv_text: str,
    source_name: str | None = None,
    broker: str | None = None,
) -> int:
    """Record a new import batch and return its id. Does NOT commit (the batch path does)."""
    cur = conn.execute(
        "INSERT INTO import_batches "
        "(kind, broker, source_name, source_sha256, imported_at, row_count, status) "
        "VALUES (?,?,?,?,?,0,'open')",
        (kind, broker, source_name, source_sha256(csv_text), app_now().isoformat()),
    )
    return int(cur.lastrowid or 0)


def stamp_row(
    conn: sqlite3.Connection, *, kind: str, row_id: int, batch_id: int, source_hash: str
) -> None:
    """Attach the batch + row hash to one just-written ledger row.

    A post-insert UPDATE rather than an extra argument threaded through five writers and
    five store inserts: the writers already return the surrogate id, and widening ten
    signatures to carry two columns that no calculation reads would put provenance in the
    path of every single-row manual entry too.
    """
    table = TABLE_BY_KIND.get(kind)
    if table is None:
        return
    conn.execute(
        f"UPDATE {table} SET import_batch_id=?, source_row_hash=? "  # noqa: S608 - fixed map
        "WHERE id=?",
        (batch_id, source_hash, row_id),
    )


def close_batch(conn: sqlite3.Connection, batch_id: int, *, row_count: int) -> None:
    """Mark a batch complete with the number of rows it actually wrote."""
    conn.execute(
        "UPDATE import_batches SET row_count=?, status='committed' WHERE id=?",
        (row_count, batch_id),
    )


def is_undoable(kind: str) -> bool:
    """Whether a batch of *kind* can be undone by batch — i.e. its rows carry the batch id.

    ``openings`` cannot (see the module docstring: its table upserts on ``(account, symbol)``
    and has no surrogate id to stamp), so its batch record is history, not a handle.
    """
    return kind in TABLE_BY_KIND


def list_batches(conn: sqlite3.Connection, *, limit: int) -> list[dict[str, object]]:
    """The import history, newest first, with each batch's row count read LIVE (DEF-017).

    ``import_batches.row_count`` is what the commit WROTE, and nothing ever updated it: a
    row deleted on its own ledger tab (six DELETE doors, none of which knows a batch exists)
    left the batch claiming rows it no longer owned — the verifier deleted a hand-entered
    dividend and the history kept 「1 筆」 with a 復原 that then deleted 0. The count is
    therefore derived from the rows that still carry the batch id, whichever door removed
    the others, and a batch with nothing left is not listed: it has nothing to undo.

    Each entry: the stored columns, ``written_count`` (the stored count, as written),
    ``row_count`` (live; the stored count for a kind that cannot be tracked) and
    ``undoable``. A kind without a provenance table (``openings``) is listed with its
    stored count and ``undoable: False``, since no live count exists for it.
    """
    tables = sorted(set(TABLE_BY_KIND.values()))
    live = " + ".join(
        f"(SELECT COUNT(*) FROM {t} WHERE import_batch_id = b.id)"  # noqa: S608 - fixed map
        for t in tables
    )
    rows = conn.execute(
        "SELECT b.id, b.kind, b.broker, b.source_name, b.source_sha256, b.imported_at, "
        f"b.row_count, b.status, {live} AS live "  # noqa: S608 - fixed map
        "FROM import_batches b ORDER BY b.id DESC"
    ).fetchall()
    out: list[dict[str, object]] = []
    for r in rows:
        kind = str(r["kind"])
        undoable = is_undoable(kind)
        remaining = int(r["live"]) if undoable else int(r["row_count"])
        if undoable and remaining == 0:
            continue
        out.append({
            "id": r["id"], "kind": kind, "broker": r["broker"],
            "source_name": r["source_name"], "source_sha256": r["source_sha256"],
            "imported_at": r["imported_at"], "row_count": remaining,
            "written_count": int(r["row_count"]), "status": r["status"],
            "undoable": undoable,
        })
        if len(out) >= limit:
            break
    return out


#: I-3: how one corporate-action EVENT (the rows of one ``(from_symbol, date, kind)`` set) of
#: an undone batch leaves the ledger — INJECTED, never defaulted. A corporate-action row is
#: not a plain row: deleting it must also take its linked reorganisation fee (DEF-020) and
#: move back the price-alert band (DEF-021) and the target weight (F-3) its EXCHANGE carried
#: across, and the weight lives in ``strategy/``, which ``data_ingestion`` may not import. The
#: binder is ``api/routers/input_center.py::import_batch_delete``, which binds the ledger tab's
#: own ``ledgers._delete_actions`` (with ``commit=False``) — so the undo and the 刪除 button are
#: ONE delete, not two that agree today. The callable closes over the request's connection and
#: must not commit: this function owns the transaction.
#:
#: Rejected: **the bare ``DELETE … WHERE import_batch_id=?``** this table used to get (the
#: band and weight stayed on the new ticker, a linked fee survived its action, and no audit
#: row was written — measured 2026-09-23); **a second copy of the restore logic here** (a
#: second owner of "what leaves with an action", which is exactly how the two doors drifted).
ActionSetDeleter = Callable[[list[StoredCorporateAction]], None]


def _action_events(rows: list[StoredCorporateAction]) -> list[list[StoredCorporateAction]]:
    """The batch's corporate-action rows grouped into events, NEWEST FIRST.

    One event per ``(from_symbol, date, kind)`` — the ledger's own set key (F-32) — because
    the set delete restores a band/weight ONCE per set. Newest first because a chain's moves
    must be undone in reverse: ``A→B`` then ``B→C`` carried a band A→B→C, and only C→B then
    B→A finds each destination still holding exactly the band it recorded.
    """
    events: dict[tuple[str, str, str], list[StoredCorporateAction]] = {}
    for a in rows:
        events.setdefault((a.from_symbol, a.date.isoformat(), a.kind), []).append(a)
    return sorted(events.values(),
                  key=lambda g: (max(a.date for a in g), max(a.id for a in g)),
                  reverse=True)


@dataclass(frozen=True)
class BatchRows:
    """Every ledger row one batch still owns, by table — exactly what its undo would delete.

    Read ONCE inside :func:`delete_batch` and handed to the guard before anything is written,
    so the guard judges the very id set the delete then removes.
    """

    batch_id: int
    ids: Mapping[str, frozenset[int]]

    def of(self, table: str) -> frozenset[int]:
        """The ids this batch owns in *table* (empty for a table it wrote nothing to)."""
        return self.ids.get(table, frozenset())


def batch_rows(conn: sqlite3.Connection, batch_id: int) -> BatchRows:
    """The rows that still carry *batch_id*, per provenance table (DEF-017: whichever door
    removed the others, this is what is left to undo)."""
    ids: dict[str, frozenset[int]] = {}
    for table in sorted(set(TABLE_BY_KIND.values())):
        ids[table] = frozenset(int(r[0]) for r in conn.execute(
            f"SELECT id FROM {table} WHERE import_batch_id=?",  # noqa: S608 - fixed map
            (batch_id,)))
    return BatchRows(batch_id=batch_id, ids=ids)


#: DEF-049 (2026-09-25): the REPLAY GUARD a batch undo must pass — INJECTED, never defaulted,
#: exactly like :data:`ActionSetDeleter`. It receives the :class:`BatchRows` the undo is about
#: to delete and answers ``True`` (go ahead) or ``False`` (refused — the binder keeps its own
#: refusal to return, and NOTHING has been written). The binder is
#: ``api/routers/input_center.py::import_batch_delete``, which binds
#: ``api/replay_guard.py::batch_undo_guard`` — the SAME ``_replay_block`` every ledger-tab
#: delete runs (orphan = hard, new/worsened 賣超 = ack-able) plus the cash doors' ack-able
#: ``negative_cash`` check for the cash/FX rows the batch removes.
#:
#: This door used to run a bare ``DELETE … WHERE import_batch_id=?``: undoing an imported buy
#: that a later hand-entered sell depended on turned that sell into a 賣超 and discarded the
#: position's cost basis with no question asked, while the 刪除 button on the same buy
#: answered 422 ``oversell`` (measured on demo 2026-09-24, batch 15 / 2884).
#:
#: Rejected: **importing the guard from ``api/``** (``data_ingestion`` may not import upward —
#: ``architecture.md``); **running it only in the router before calling this function**
#: (legal, but the id set would be read twice, and the next caller of ``delete_batch`` — a
#: script, a second bulk door — would undo with no guard at all, which is how this door came
#: to have none; a REQUIRED argument makes forgetting it a mypy error and a ``TypeError``);
#: **re-deriving the replay here** (a second owner of the oversell decision — the drift C3's
#: injection convention exists to prevent).
BatchUndoGuard = Callable[[BatchRows], bool]


class _RowDeleter(Protocol):
    def __call__(self, conn: sqlite3.Connection, row_id: int, /, *, commit: bool = ...
                 ) -> bool: ...


#: table -> the store's OWN delete for one row: before-image to ``ledger_audit``, then the
#: keyed DELETE (DEF-049 — the batch undo writes the same audit row the ledger tab's 刪除 does,
#: tagged 「批次復原 #id」 via ``store.audit_source``; there is no second audit format).
_ROW_DELETERS: dict[str, _RowDeleter] = {
    "transactions": delete_transaction,
    "dividends": delete_dividend,
    "fx_conversions": delete_fx_conversion,
    "cash_movements": delete_cash_movement,
}


def undo_source(batch_id: int) -> str:
    """The ``ledger_audit.source`` every row of this undo is tagged with."""
    return f"批次復原 #{batch_id}"


def delete_batch(
    conn: sqlite3.Connection, batch_id: int, *, delete_actions: ActionSetDeleter,
    guard: BatchUndoGuard, commit: bool = True,
) -> int | None:
    """Delete every ledger row this batch wrote, and the batch record.

    Returns the rows removed, or ``None`` when *guard* refused the undo — in which case
    nothing at all has been written.

    This is the half that makes an import safe to attempt on real data: a bad batch is
    undone exactly, rather than by restoring a backup and losing everything entered since.

    The rows are read once (:func:`batch_rows`) and judged by *guard* (see
    :data:`BatchUndoGuard`) before the first delete. The batch's corporate actions then go
    through *delete_actions* (see :data:`ActionSetDeleter`), one event at a time, newest
    first; every other row through the store's own per-row delete, which audits it. All of it
    is ONE transaction: a failure part-way rolls the whole undo back.
    """
    rows = batch_rows(conn, batch_id)
    if not guard(rows):
        return None
    removed = 0
    try:
        with audit_source(undo_source(batch_id)):
            action_ids = rows.of("corporate_actions")
            if action_ids:
                owned = [a for a in list_corporate_actions(conn) if a.id in action_ids]
                for event in _action_events(owned):
                    delete_actions(event)
                    removed += len(event)
            # Iterated over the provenance map, not the deleter map: a ledger added to
            # ``TABLE_BY_KIND`` without a deleter here fails LOUD (KeyError, rolled back)
            # instead of leaving its rows behind a batch record that is then deleted.
            for table in sorted(set(TABLE_BY_KIND.values()) - {"corporate_actions"}):
                for row_id in sorted(rows.of(table)):
                    # False = already gone (e.g. a linked fee its corporate action took).
                    if _ROW_DELETERS[table](conn, row_id, commit=False):
                        removed += 1
        conn.execute("DELETE FROM import_batches WHERE id=?", (batch_id,))
        if commit:
            conn.commit()
    except Exception:
        if commit:
            conn.rollback()
        raise
    return removed
