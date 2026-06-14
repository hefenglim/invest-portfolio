"""Atomicity of batch import commit (#1).

``commit_preview`` must be all-or-nothing on an UNEXPECTED writer error: a mid-batch
raise rolls the whole batch back (no partial ledger write — CLAUDE.md 重算/append-only).
Intentional skips of hard-issue rows stay as designed (contract-level partial success,
NOT a rollback trigger), and the ``ImportSummary(written, skipped)`` shape is unchanged.
"""

import sqlite3

import pytest

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.csv_import import (
    build_transaction_preview,
    write_transaction_row,
)
from portfolio_dash.data_ingestion.preview import (
    ImportPreview,
    PreviewRow,
    commit_preview,
)
from portfolio_dash.data_ingestion.store import list_transactions, upsert_instrument
from portfolio_dash.data_ingestion.validate import Issue
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument


def _setup(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    upsert_instrument(
        conn,
        Instrument(
            symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
            sector="Tech", name="台積電",
        ),
    )


def _valid_rows(n: int) -> ImportPreview:
    """N valid (no-issue) transaction PreviewRows ready for the real writer."""
    rows: list[PreviewRow] = []
    for i in range(n):
        rows.append(
            PreviewRow(
                index=i,
                raw={},
                payload={
                    "account_id": "tw_broker",
                    "symbol": "2330",
                    "side": "BUY",
                    "quantity": "1000",
                    "price": "600",
                    "trade_date": "2026-06-01",
                    "note": "",
                },
                fee=None,
                tax=None,
            )
        )
    return ImportPreview(rows=rows)


def test_mid_batch_error_rolls_back_whole_batch(conn: sqlite3.Connection) -> None:
    """An unexpected writer error on row k leaves NONE of the batch persisted."""
    _setup(conn)
    preview = _valid_rows(5)

    calls = {"n": 0}

    def flaky_writer(c: sqlite3.Connection, row: PreviewRow, *, commit: bool = True) -> int:
        # Persist rows 0..2 via the real (non-committing) writer, then blow up on row 3.
        if calls["n"] == 3:
            raise RuntimeError("simulated unexpected DB error mid-batch")
        calls["n"] += 1
        return write_transaction_row(c, row, commit=commit)

    with pytest.raises(RuntimeError, match="simulated unexpected DB error"):
        commit_preview(
            conn, preview, accept=set(range(5)), writer=flaky_writer
        )

    # Full rollback: the rows the flaky writer already inserted are gone.
    assert list_transactions(conn, account_id="tw_broker") == []


class _CommitCountingConn:
    """Thin proxy that counts ``commit()`` calls and delegates everything else.

    ``sqlite3.Connection.commit`` is a read-only C attribute, so we wrap rather than
    monkeypatch to assert the batch commits exactly once (not once per row).
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1
        self._conn.commit()

    def __getattr__(self, name: str) -> object:
        return getattr(self._conn, name)


def test_happy_path_writes_all_with_one_effective_commit(
    conn: sqlite3.Connection,
) -> None:
    """All accepted non-hard rows persist, committed once as a single batch."""
    _setup(conn)
    preview = _valid_rows(4)

    spy = _CommitCountingConn(conn)
    summary = commit_preview(
        spy,  # type: ignore[arg-type]
        preview,
        accept={0, 1, 2, 3},
        writer=write_transaction_row,
    )

    assert len(summary.written) == 4
    assert summary.skipped == []
    assert len(list_transactions(conn, account_id="tw_broker")) == 4
    # One effective commit for the whole batch (not one per row).
    assert spy.commits == 1


def test_hard_rows_skipped_rest_written_no_rollback(
    conn: sqlite3.Connection,
) -> None:
    """Intentional hard-issue skips are partial success, NOT a rollback trigger."""
    _setup(conn)
    csv = (
        "account,symbol,side,date,shares,price\n"
        "tw_broker,2330,BUY,2026-06-01,1000,600\n"   # ok
        "nope,2330,BUY,2026-06-03,100,600\n"         # hard: unknown_account
        "tw_broker,2330,BUY,2026-06-02,1000,600\n"   # ok
    )
    preview = build_transaction_preview(conn, csv)
    accept = {r.index for r in preview.rows if not r.has_hard_issue}
    summary = commit_preview(
        conn, preview, accept=accept, writer=write_transaction_row
    )
    # The two valid rows are written; the hard row is skipped (no rollback).
    assert len(summary.written) == 2
    assert summary.skipped == [1]
    assert len(list_transactions(conn, account_id="tw_broker")) == 2


def test_hard_row_only_issue_property() -> None:
    """A non-confirmable issue marks the row as a hard issue (skip, not error)."""
    row = PreviewRow(
        index=0, raw={}, issues=[Issue(kind="unknown_account", message="x")]
    )
    assert row.has_hard_issue is True
