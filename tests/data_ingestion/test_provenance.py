"""Import provenance: the idempotency key, and the batch that makes an import reversible.

The two properties that matter are adversarial to each other, and both are tested here:

* re-importing the same file must write NOTHING, and
* a file that legitimately repeats a row must write BOTH copies.

An implementation that hashes row content alone satisfies the first and silently fails the
second — it drops a real movement and reports success. The occurrence ordinal is what makes
both hold at once.
"""

import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.data_ingestion.cash_import import (
    CASH_MOVEMENT_COLUMNS,
    build_cash_movement_preview,
    write_cash_movement_row,
)
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.preview import BatchContext, commit_preview
from portfolio_dash.data_ingestion.provenance import (
    TABLE_BY_KIND,
    delete_batch,
    existing_hashes,
    open_batch,
    row_hash,
    row_hashes,
    source_sha256,
)
from portfolio_dash.data_ingestion.schema import create_tables
from portfolio_dash.data_ingestion.store import insert_cash_movement, list_cash_movements
from portfolio_dash.data_ingestion.validate import CashPool
from portfolio_dash.shared.enums import Currency

_HEADER = ",".join(CASH_MOVEMENT_COLUMNS) + "\n"
_DEPOSIT = "tw_broker,2026-07-01,DEPOSIT,TWD,50,,\n"


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    create_tables(c)
    seed_accounts(c)
    return c


def _rich_pool(account_id: str, ccy: Currency, **_: object) -> CashPool:
    """A pool with room for anything — provenance is what is under test, not the guard."""
    return CashPool(balance=Decimal("10000000"), low=Decimal("10000000"))


def _commit(conn: sqlite3.Connection, csv_text: str) -> tuple[int, list[int], list[int]]:
    """Import *csv_text* as one batch; return (batch_id, written ids, duplicate indices)."""
    preview = build_cash_movement_preview(conn, csv_text, pool=_rich_pool)
    hashes = dict(
        zip(
            (r.index for r in preview.rows),
            row_hashes("cash", [r.raw for r in preview.rows]),
            strict=True,
        )
    )
    batch_id = open_batch(conn, kind="cash", csv_text=csv_text, source_name="t.csv")
    summary = commit_preview(
        conn,
        preview,
        accept={r.index for r in preview.rows if not r.has_hard_issue},
        writer=write_cash_movement_row,
        provenance=BatchContext(
            kind="cash",
            batch_id=batch_id,
            hashes=hashes,
            already_present=existing_hashes(conn, "cash", hashes.values()),
        ),
    )
    return batch_id, summary.written, summary.duplicates


# ----------------------------------------------------------------- the key itself


def test_hash_ignores_column_order() -> None:
    """Rearranging columns in Excel does not create new rows."""
    a = row_hash("cash", {"account": "x", "amount": "1"}, 0)
    b = row_hash("cash", {"amount": "1", "account": "x"}, 0)
    assert a == b


def test_hash_separates_kinds() -> None:
    same = {"account": "x", "amount": "1"}
    assert row_hash("cash", same, 0) != row_hash("transactions", same, 0)


def test_identical_rows_get_distinct_hashes() -> None:
    """THE trap. Two identical $50 deposits on one date are a real statement line pair; a
    content-only hash collapses them and the import silently drops one."""
    raws = [{"a": "1"}, {"a": "1"}, {"a": "2"}]
    hs = row_hashes("cash", raws)
    assert len(set(hs)) == 3


def test_occurrence_is_positional_only_among_identical_rows() -> None:
    """Inserting an unrelated row must not renumber anything, or a re-upload of an edited
    file would re-write every row below the edit."""
    before = row_hashes("cash", [{"a": "1"}, {"a": "2"}, {"a": "1"}])
    after = row_hashes("cash", [{"a": "9"}, {"a": "1"}, {"a": "2"}, {"a": "1"}])
    assert before == after[1:]


def test_source_digest_is_the_text() -> None:
    assert source_sha256("a") == source_sha256("a")
    assert source_sha256("a") != source_sha256("b")


def test_table_map_matches_the_migrated_ledgers(conn: sqlite3.Connection) -> None:
    """Every table this module claims to stamp must actually carry the columns — a kind
    mapped to a table without them fails at UPDATE time, on a real import."""
    for table in set(TABLE_BY_KIND.values()):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        assert {"import_batch_id", "source_row_hash"} <= cols, table


def test_opening_inventory_is_deliberately_absent() -> None:
    """Its composite PK makes the writer an UPSERT — already idempotent, no id to stamp."""
    assert "openings" not in TABLE_BY_KIND


# ---------------------------------------------------------------- through a commit


def test_a_committed_row_carries_its_batch(conn: sqlite3.Connection) -> None:
    batch_id, written, dupes = _commit(conn, _HEADER + _DEPOSIT)
    assert len(written) == 1 and dupes == []
    row = conn.execute(
        "SELECT import_batch_id, source_row_hash FROM cash_movements"
    ).fetchone()
    assert row[0] == batch_id
    assert row[1] is not None


def test_re_importing_the_same_file_writes_nothing(conn: sqlite3.Connection) -> None:
    """Before provenance this doubled the ledger, silently and with a success message."""
    _commit(conn, _HEADER + _DEPOSIT)
    _, written, dupes = _commit(conn, _HEADER + _DEPOSIT)
    assert written == []
    assert len(dupes) == 1
    assert len(list_cash_movements(conn)) == 1


def test_a_file_that_repeats_a_row_writes_both(conn: sqlite3.Connection) -> None:
    """The other half of the property. Same date, same amount, same account, twice —
    two real movements, not one movement and one duplicate."""
    _, written, dupes = _commit(conn, _HEADER + _DEPOSIT + _DEPOSIT)
    assert len(written) == 2
    assert dupes == []
    assert len(list_cash_movements(conn)) == 2
    # ...and re-importing THAT file still writes nothing.
    _, written2, dupes2 = _commit(conn, _HEADER + _DEPOSIT + _DEPOSIT)
    assert written2 == [] and len(dupes2) == 2


def test_the_batch_records_what_it_wrote(conn: sqlite3.Connection) -> None:
    batch_id, written, _ = _commit(conn, _HEADER + _DEPOSIT + _DEPOSIT)
    row = conn.execute(
        "SELECT kind, source_name, row_count, status FROM import_batches WHERE id=?",
        (batch_id,),
    ).fetchone()
    assert tuple(row) == ("cash", "t.csv", len(written), "committed")


# ------------------------------------------------------------------ reversibility


def test_deleting_a_batch_removes_exactly_its_rows(conn: sqlite3.Connection) -> None:
    """The half that makes an import safe to ATTEMPT on real data: a bad batch is undone
    exactly, instead of by restoring a backup and losing everything entered since."""
    insert_cash_movement(
        conn, account_id="tw_broker", move_date=date(2026, 6, 1), kind="DEPOSIT",
        ccy=Currency.TWD, amount=Decimal("999"), note="typed by hand",
    )
    batch_id, written, _ = _commit(conn, _HEADER + _DEPOSIT + _DEPOSIT)
    assert len(list_cash_movements(conn)) == 3

    removed = delete_batch(conn, batch_id)
    assert removed == len(written)
    remaining = list_cash_movements(conn)
    assert len(remaining) == 1
    assert remaining[0].note == "typed by hand"  # the hand-entered row is untouched
    assert conn.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0] == 0


def test_deleting_a_batch_lets_the_same_file_import_again(
    conn: sqlite3.Connection,
) -> None:
    """Undo must be complete, not merely visible: if the hashes survived the delete, the
    re-import would report every row as a duplicate and the ledger would stay empty."""
    batch_id, _, _ = _commit(conn, _HEADER + _DEPOSIT)
    delete_batch(conn, batch_id)
    _, written, dupes = _commit(conn, _HEADER + _DEPOSIT)
    assert len(written) == 1 and dupes == []
