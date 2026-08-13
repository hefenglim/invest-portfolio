"""Generic import preview/commit core — reused by all CSV ledger importers."""

import sqlite3
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, Field

from portfolio_dash.data_ingestion.provenance import close_batch, stamp_row
from portfolio_dash.data_ingestion.validate import Issue


class PreviewRow(BaseModel):
    """One parsed CSV row plus validation findings and auto-computed amounts."""

    index: int
    raw: dict[str, str]
    payload: dict[str, str] = Field(default_factory=dict)  # ledger-specific commit data
    fee: Decimal | None = None
    tax: Decimal | None = None
    issues: list[Issue] = Field(default_factory=list)

    @property
    def has_hard_issue(self) -> bool:
        """True when at least one issue is non-confirmable (blocks the commit)."""
        return any(not i.needs_confirm for i in self.issues)


class ImportPreview(BaseModel):
    """All rows parsed from a CSV, with issues and computed amounts."""

    rows: list[PreviewRow]


class ImportSummary(BaseModel):
    """Result of :func:`commit_preview`: which rows were written, skipped, or already here.

    ``duplicates`` is separate from ``skipped`` on purpose. A skip is a REFUSAL (the row has
    a hard issue, or the caller did not accept it) and needs the user's attention; a
    duplicate is a row this ledger already holds, which is the correct and uneventful
    outcome of re-running an import. Folding them together would report a clean re-import as
    a file full of problems.
    """

    written: list[int] = Field(default_factory=list)
    skipped: list[int] = Field(default_factory=list)
    duplicates: list[int] = Field(default_factory=list)


class BatchContext(BaseModel):
    """Provenance for one commit — see :mod:`data_ingestion.provenance`.

    ``hashes`` is keyed by :attr:`PreviewRow.index` rather than positional, so it cannot
    silently mis-align with a preview whose rows are filtered or reordered.
    """

    kind: str
    #: ``None`` when this commit will write nothing (every acceptable row is already in the
    #: ledger). The context is still supplied in that case — it carries the duplicate
    #: detection — but no batch row is created, because a batch that owns no data would
    #: claim in the import history that an import happened.
    batch_id: int | None = None
    hashes: dict[int, str] = Field(default_factory=dict)
    already_present: set[str] = Field(default_factory=set)


class Writer(Protocol):
    """A ledger-specific writer that inserts one preview row and returns its id.

    ``commit`` lets the batch path defer the commit so the whole batch is one
    transaction (all-or-nothing, #1); the writer's default is ``commit=True`` for
    any single-row caller.
    """

    def __call__(
        self, conn: sqlite3.Connection, row: PreviewRow, *, commit: bool = ...
    ) -> int: ...


def commit_preview(
    conn: sqlite3.Connection,
    preview: ImportPreview,
    *,
    accept: set[int],
    writer: Writer,
    provenance: BatchContext | None = None,
) -> ImportSummary:
    """Commit accepted rows from a preview, skipping any with hard issues.

    The batch is **all-or-nothing on an unexpected error**: every accepted row is
    written with ``commit=False`` and the whole batch is committed once at the end.
    Any exception rolls the entire batch back and re-raises, so a mid-batch failure
    never leaves a partial ledger write (CLAUDE.md 重算/append-only). Intentional skips
    of hard-issue rows are contract-level partial success, NOT a rollback trigger.

    Args:
        conn:    Active SQLite connection.
        preview: The preview produced by a ledger-specific builder.
        accept:  Set of row indices the caller has accepted for writing.
        writer:  Ledger-specific callable that inserts one row and returns its id.

    Returns:
        :class:`ImportSummary` listing written row ids and skipped row indices.

    Raises:
        Exception: re-raises any writer error after rolling the whole batch back.
    """
    summary = ImportSummary()
    try:
        seen_in_batch: set[str] = set()
        for row in preview.rows:
            source_hash = provenance.hashes.get(row.index) if provenance else None
            if source_hash is not None and (
                source_hash in provenance.already_present  # type: ignore[union-attr]
                or source_hash in seen_in_batch
            ):
                # Already in the ledger, or written earlier in THIS batch. The second case
                # matters because a preview is re-derived per commit and a caller may
                # submit overlapping files; without it a within-file repeat would insert
                # once and then fail the "not already here" property on the next re-run.
                summary.duplicates.append(row.index)
                continue
            if row.index in accept and not row.has_hard_issue:
                row_id = writer(conn, row, commit=False)
                summary.written.append(row_id)
                if (
                    provenance is not None
                    and source_hash is not None
                    and provenance.batch_id is not None
                ):
                    stamp_row(
                        conn,
                        kind=provenance.kind,
                        row_id=row_id,
                        batch_id=provenance.batch_id,
                        source_hash=source_hash,
                    )
                    seen_in_batch.add(source_hash)
            else:
                summary.skipped.append(row.index)
        if provenance is not None and provenance.batch_id is not None:
            # Inside the SAME transaction as the rows, so a rollback takes the batch record
            # with it and cannot leave an orphan claiming rows that were never written.
            close_batch(conn, provenance.batch_id, row_count=len(summary.written))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return summary
