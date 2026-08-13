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
from collections.abc import Iterable

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


def delete_batch(conn: sqlite3.Connection, batch_id: int, *, commit: bool = True) -> int:
    """Delete every ledger row this batch wrote, and the batch record. Returns rows removed.

    This is the half that makes an import safe to attempt on real data: a bad batch is
    undone exactly, rather than by restoring a backup and losing everything entered since.
    """
    removed = 0
    for table in sorted(set(TABLE_BY_KIND.values())):
        cur = conn.execute(
            f"DELETE FROM {table} WHERE import_batch_id=?",  # noqa: S608 - fixed map
            (batch_id,),
        )
        removed += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    conn.execute("DELETE FROM import_batches WHERE id=?", (batch_id,))
    if commit:
        conn.commit()
    return removed
