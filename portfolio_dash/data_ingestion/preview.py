"""Generic import preview/commit core — reused by all CSV ledger importers."""

import sqlite3
from collections.abc import Callable
from decimal import Decimal

from pydantic import BaseModel, Field

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
    """Result of :func:`commit_preview`: which rows were written vs skipped."""

    written: list[int] = Field(default_factory=list)
    skipped: list[int] = Field(default_factory=list)


Writer = Callable[[sqlite3.Connection, PreviewRow], int]


def commit_preview(
    conn: sqlite3.Connection,
    preview: ImportPreview,
    *,
    accept: set[int],
    writer: Writer,
) -> ImportSummary:
    """Commit accepted rows from a preview, skipping any with hard issues.

    Args:
        conn:    Active SQLite connection.
        preview: The preview produced by a ledger-specific builder.
        accept:  Set of row indices the caller has accepted for writing.
        writer:  Ledger-specific callable that inserts one row and returns its id.

    Returns:
        :class:`ImportSummary` listing written row ids and skipped row indices.
    """
    summary = ImportSummary()
    for row in preview.rows:
        if row.index in accept and not row.has_hard_issue:
            summary.written.append(writer(conn, row))
        else:
            summary.skipped.append(row.index)
    return summary
