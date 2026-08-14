"""``commit_preview`` reports FOUR outcomes, and only one of them is a problem (C3).

Until 2026-08-14 a row the caller deselected and a row the importer REFUSED shared one
counter. The two mean opposite things to whoever reads the number — 「跳過」 says *you did
not tick it*, a refusal says *the ledger is now missing this* — and the conflation had a
measured cost: an import of 5 corporate actions wrote 2, announced 「成功 2 筆・跳過 3 筆」,
and left every share count for those symbols wrong by a split ratio with nothing on any
screen saying so (demo corpus, 2026-08-12).

These tests drive ``commit_preview`` directly rather than through a router, because the
bucketing is its decision and a router test would also be asserting the router's mapping.
"""

import sqlite3

import pytest

from portfolio_dash.data_ingestion.preview import (
    ImportPreview,
    ImportSummary,
    PreviewRow,
    commit_preview,
)
from portfolio_dash.data_ingestion.validate import Issue


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    c.commit()
    return c


def _writer(conn: sqlite3.Connection, row: PreviewRow, *, commit: bool = True) -> int:
    cur = conn.execute("INSERT INTO t (v) VALUES (?)", (row.raw.get("v", ""),))
    if commit:
        conn.commit()
    return int(cur.lastrowid or 0)


def _row(index: int, *, hard: bool = False, warn: bool = False) -> PreviewRow:
    issues: list[Issue] = []
    if hard:
        issues.append(Issue(kind="parse_error", message=f"第 {index} 列壞掉了"))
    if warn:
        issues.append(Issue(kind="duplicate_trade", needs_confirm=True, message="重複?"))
    return PreviewRow(index=index, raw={"v": str(index)}, issues=issues)


def _run(conn: sqlite3.Connection, rows: list[PreviewRow], accept: set[int]) -> ImportSummary:
    return commit_preview(conn, ImportPreview(rows=rows), accept=accept, writer=_writer)


def test_a_deselected_row_is_skipped_and_a_hard_row_is_rejected(
    conn: sqlite3.Connection,
) -> None:
    """The whole point, in one assertion: two rows are not written, for different reasons,
    and the summary distinguishes them."""
    summary = _run(conn, [_row(0), _row(1), _row(2, hard=True)], accept={0, 2})
    assert len(summary.written) == 1                       # row 0
    assert summary.skipped == [1]                          # row 1: the caller said no
    assert [r.index for r in summary.rejected] == [2]       # row 2: the importer said no


def test_a_rejection_carries_the_validator_s_OWN_message(conn: sqlite3.Connection) -> None:
    """Verbatim, not re-summarised. A second wording of a rejection is a second thing to
    keep in step, and the two disagree the first time one of them changes."""
    summary = _run(conn, [_row(0, hard=True)], accept={0})
    assert summary.rejected[0].kind == "parse_error"
    assert summary.rejected[0].message == "第 0 列壞掉了"


def test_a_row_that_is_BOTH_deselected_and_hard_counts_as_rejected(
    conn: sqlite3.Connection,
) -> None:
    """It could not have been written even if it had been ticked, so the refusal is the
    honest reason. Reporting it as 「跳過」 would tell the owner that ticking it next time
    would fix it."""
    summary = _run(conn, [_row(0, hard=True)], accept=set())
    assert [r.index for r in summary.rejected] == [0]
    assert summary.skipped == []


def test_a_confirmable_warning_alone_does_NOT_reject(conn: sqlite3.Connection) -> None:
    """A soft issue rides the ack; it is not a refusal. Without this pair, ``rejected``
    could quietly widen into "any row with an issue" and every acked oversell would be
    reported as a failure."""
    summary = _run(conn, [_row(0, warn=True)], accept={0})
    assert summary.rejected == [] and summary.skipped == []
    assert len(summary.written) == 1


def test_a_clean_batch_leaves_every_problem_bucket_empty(conn: sqlite3.Connection) -> None:
    summary = _run(conn, [_row(0), _row(1)], accept={0, 1})
    assert summary.rejected == [] and summary.skipped == [] and summary.duplicates == []
    assert len(summary.written) == 2
