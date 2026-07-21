"""Tests for opening_inventory store functions and CSV import (A6 contract).

A6 (2026-07-21): ``original_cost_total`` is the authoritative money of record; the stored
``original_avg_cost`` column is retired (dropped by a boot-seam migration). The CSV contract
requires the total and treats ``original_avg_cost`` as a legacy-optional column.
"""

import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.opening_import import (
    build_opening_preview,
    write_opening_row,
)
from portfolio_dash.data_ingestion.preview import commit_preview
from portfolio_dash.data_ingestion.schema import create_tables
from portfolio_dash.data_ingestion.store import list_opening, upsert_instrument, upsert_opening
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument


def _setup(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    upsert_instrument(
        conn,
        Instrument(
            symbol="2330",
            market=Market.TW,
            quote_ccy=Currency.TWD,
            sector="Tech",
            name="台積電",
        ),
    )


def _kinds(preview: object) -> set[str]:
    return {i.kind for row in preview.rows for i in row.issues}  # type: ignore[attr-defined]


def test_store_upsert_roundtrip_and_avg_on_read(conn: sqlite3.Connection) -> None:
    upsert_opening(
        conn,
        account_id="tw_broker",
        symbol="2330",
        shares=Decimal("1000"),
        original_cost_total=Decimal("500000"),
        build_date=date(2025, 1, 1),
    )
    upsert_opening(
        conn,
        account_id="tw_broker",
        symbol="2330",
        shares=Decimal("1000"),
        original_cost_total=Decimal("500000"),
        build_date=date(2025, 1, 1),
    )  # idempotent (PK account+symbol)
    rows = list_opening(conn, account_id="tw_broker")
    assert len(rows) == 1 and rows[0].original_cost_total == Decimal("500000")
    assert rows[0].original_avg == Decimal("500")  # computed on read (total / shares)


def test_csv_total_required_and_commits(conn: sqlite3.Connection) -> None:
    _setup(conn)
    csv = (
        "account,symbol,shares,original_cost_total,build_date\n"
        "tw_broker,2330,1000,500000,2025-01-01\n"
    )
    p = build_opening_preview(conn, csv)
    assert p.rows[0].issues == []  # valid — total supplied, no legacy avg
    summary = commit_preview(conn, p, accept={0}, writer=write_opening_row)
    assert len(summary.written) == 1
    rows = list_opening(conn, account_id="tw_broker")
    assert rows[0].original_cost_total == Decimal("500000")


def test_csv_only_avg_derives_total_soft_issue(conn: sqlite3.Connection) -> None:
    """Legacy CSV (avg, no total) still imports: total is derived (avg * shares) with a soft
    ``opening_total_derived`` needs-confirm issue (never a hard block)."""
    _setup(conn)
    csv = (
        "account,symbol,shares,original_avg_cost,build_date\n"
        "tw_broker,2330,1000,500,2025-01-01\n"
    )
    p = build_opening_preview(conn, csv)
    assert "opening_total_derived" in _kinds(p)
    assert not p.rows[0].has_hard_issue  # soft — commit still writes
    summary = commit_preview(conn, p, accept={0}, writer=write_opening_row)
    assert len(summary.written) == 1
    rows = list_opening(conn, account_id="tw_broker")
    assert rows[0].original_cost_total == Decimal("500000")  # 500 * 1000 derived


def test_csv_both_matching_no_mismatch(conn: sqlite3.Connection) -> None:
    _setup(conn)
    csv = (
        "account,symbol,shares,original_cost_total,build_date,original_avg_cost\n"
        "tw_broker,2330,1000,500000,2025-01-01,500\n"
    )
    p = build_opening_preview(conn, csv)
    assert p.rows[0].issues == []  # avg * shares == total, within tolerance
    assert p.rows[0].payload["original_cost_total"] == "500000"


def test_csv_mismatch_flags_needs_confirm(conn: sqlite3.Connection) -> None:
    """total and legacy avg disagree beyond max(1 minor unit, 0.5% * total) -> soft
    ``opening_cost_mismatch``; the authoritative total is stored regardless."""
    _setup(conn)
    csv = (
        "account,symbol,shares,original_cost_total,build_date,original_avg_cost\n"
        "tw_broker,2330,1000,500000,2025-01-01,600\n"  # 600*1000=600000 != 500000
    )
    p = build_opening_preview(conn, csv)
    assert "opening_cost_mismatch" in _kinds(p)
    assert not p.rows[0].has_hard_issue  # needs-confirm, not a hard block
    assert p.rows[0].payload["original_cost_total"] == "500000"  # total wins, not avg*shares


def test_csv_small_rounding_within_tolerance_no_flag(conn: sqlite3.Connection) -> None:
    """A rounded legacy avg within 0.5% of the total does NOT raise a mismatch."""
    _setup(conn)
    csv = (
        "account,symbol,shares,original_cost_total,build_date,original_avg_cost\n"
        "tw_broker,2330,1000,333333,2025-01-01,333\n"  # 333*1000=333000, diff 333 < 1666
    )
    p = build_opening_preview(conn, csv)
    assert "opening_cost_mismatch" not in _kinds(p)


def test_csv_missing_both_is_parse_error(conn: sqlite3.Connection) -> None:
    _setup(conn)
    csv = (
        "account,symbol,shares,original_cost_total,build_date\n"
        "tw_broker,2330,1000,,2025-01-01\n"  # total blank, no avg column
    )
    p = build_opening_preview(conn, csv)
    assert p.rows[0].has_hard_issue and "parse_error" in _kinds(p)


def test_csv_unknown_account_hard_blocks(conn: sqlite3.Connection) -> None:
    _setup(conn)
    csv = (
        "account,symbol,shares,original_cost_total,build_date\n"
        "nope,2330,1000,500000,2025-01-01\n"
    )
    p = build_opening_preview(conn, csv)
    assert p.rows[0].has_hard_issue
    summary = commit_preview(conn, p, accept={0}, writer=write_opening_row)
    assert summary.written == [] and 0 in summary.skipped


# --- boot-seam migration: legacy DB drops the retired column, data preserved ---------------

_LEGACY_DDL = """
CREATE TABLE opening_inventory (
    account_id TEXT NOT NULL, symbol TEXT NOT NULL,
    shares TEXT NOT NULL, original_avg_cost TEXT NOT NULL, original_cost_total TEXT NOT NULL,
    build_date TEXT NOT NULL,
    PRIMARY KEY (account_id, symbol)
);
"""


def _opening_cols(conn: sqlite3.Connection) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(opening_inventory)")}


def test_boot_migration_drops_legacy_avg_column() -> None:
    """A legacy DB carrying original_avg_cost: create_tables drops it (idempotently) and the
    row's total survives; the average is computed on read. Second boot is a no-op."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_LEGACY_DDL)
        conn.execute(
            "INSERT INTO opening_inventory "
            "(account_id, symbol, shares, original_avg_cost, original_cost_total, build_date) "
            "VALUES ('tw_broker','2330','1000','500','500000','2026-01-02')"
        )
        conn.commit()
        assert "original_avg_cost" in _opening_cols(conn)

        create_tables(conn)  # boot seam runs the drop migration
        assert "original_avg_cost" not in _opening_cols(conn)

        rows = list_opening(conn, account_id="tw_broker")
        assert len(rows) == 1
        assert rows[0].original_cost_total == Decimal("500000")
        assert rows[0].original_avg == Decimal("500")  # computed on read

        create_tables(conn)  # idempotent: second boot leaves the column gone
        assert "original_avg_cost" not in _opening_cols(conn)
    finally:
        conn.close()


def test_fresh_db_never_has_legacy_avg_column(conn: sqlite3.Connection) -> None:
    """The bootstrap_db-seeded fixture DB never creates the retired column."""
    assert "original_avg_cost" not in _opening_cols(conn)


@pytest.mark.parametrize("bad", ["", "  "])
def test_csv_blank_total_and_avg_columns_present_but_empty(
    conn: sqlite3.Connection, bad: str
) -> None:
    """Both columns present but blank -> parse_error (no cost of record can be determined)."""
    _setup(conn)
    csv = (
        "account,symbol,shares,original_cost_total,build_date,original_avg_cost\n"
        f"tw_broker,2330,1000,{bad},2025-01-01,{bad}\n"
    )
    p = build_opening_preview(conn, csv)
    assert p.rows[0].has_hard_issue and "parse_error" in _kinds(p)
