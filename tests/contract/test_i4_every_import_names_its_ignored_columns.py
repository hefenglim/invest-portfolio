"""I-4 (F-1): every import kind NAMES the columns it will not read — not only transactions.

DEF-026 gave the transaction door ``unread_columns_issues`` (「已忽略欄位：…」 on each row), and
the other five doors kept dropping an export's system columns — and any mistyped column — in
silence: a dividend export re-imported with ``id`` / ``import_batch_id`` / ``source_row_hash``
said nothing, and a cash file whose ``amount`` header was typed ``amout`` reported a blank
amount rather than a column nobody reads. Fixed at the seam every door passes through — each
kind's builder — so the CSV door, the broker door and the AI door all see the same notice.

The notice is ADVISORY (``info``): it never blocks a row and never asks for ``ack_warnings``.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.conftest import DashboardClientFactory
from tests.contract.test_def026_ledger_export_reimports import (
    _ORDER,
    IMPORT_KIND,
    _instruments,
    _Pair,
    _round_trip,
)

#: Export tab -> the system columns its file carries that no importer reads, in header order.
#: ``opening`` has none: its table has no surrogate id or provenance columns, and every
#: column it exports is a template column (``account_id`` through the alias).
_EXPECTED: dict[str, list[str]] = {
    "transactions": ["id", "import_batch_id", "source_row_hash"],
    "dividends": ["id", "import_batch_id", "source_row_hash"],
    "fx": ["id", "import_batch_id", "source_row_hash"],
    "opening": [],
    # DEF-009 added ``rebate_period`` (the confirmed 折讓款 credit's trade month).
    "cash": ["id", "corporate_action_id", "rebate_period", "import_batch_id", "source_row_hash"],
    "actions": ["id", "band_move_json", "weight_move_json", "import_batch_id",
                "source_row_hash"],
}


@pytest.fixture
def pair(dashboard_client_factory: DashboardClientFactory) -> Iterator[_Pair]:
    yield _Pair(dashboard_client_factory)


@pytest.mark.parametrize("kind", _ORDER)
def test_each_exported_ledger_names_what_it_drops_on_every_row(pair: _Pair, kind: str) -> None:
    previews = _round_trip(pair, _ORDER[: _ORDER.index(kind) + 1])
    rows = previews[kind]["rows"]
    assert isinstance(rows, list) and rows
    for row in rows:
        assert row["status"] != "error", row          # advisory only — never a block
        info = " ".join(row.get("info") or [])
        if _EXPECTED[kind]:
            assert f"已忽略欄位：{'、'.join(_EXPECTED[kind])}" in info, (kind, row)
        else:
            assert "已忽略欄位" not in info, (kind, row)


_TYPO_FILES: dict[str, str] = {
    "dividends": ("account,symbol,date,type,gross,withholding,net,memo\n"
                  "tw_broker,2330,2026-03-01,CASH,5000,0,5000,x\n"),
    "fx": ("account,date,from_ccy,from_amount,to_ccy,to_amount,memo\n"
           "schwab,2026-01-08,TWD,32000,USD,1000,x\n"),
    "openings": ("account,symbol,shares,original_cost_total,build_date,memo\n"
                 "tw_broker,2330,1000,500000,2026-01-02,x\n"),
    "cash": ("account,date,kind,ccy,amount,note,memo\n"
             "schwab,2026-01-03,DEPOSIT,TWD,100000,,x\n"),
    "corporate_actions": ("account,date,kind,from_symbol,to_symbol,ratio_to,ratio_from,memo\n"
                          "schwab,2026-04-01,SPLIT,AAPL,AAPL,2,1,x\n"),
}


@pytest.mark.parametrize("kind", sorted(_TYPO_FILES))
def test_an_unknown_column_is_named_as_a_probable_typo(
    dashboard_client_factory: DashboardClientFactory, kind: str
) -> None:
    """The other advisory: a column that is not an export system column either."""
    client = dashboard_client_factory(_instruments)
    r = client.post("/api/import/preview", json={"kind": kind, "csv_text": _TYPO_FILES[kind]})
    assert r.status_code == 200, r.text
    (row,) = r.json()["rows"]
    info = " ".join(row.get("info") or [])
    assert "已忽略欄位：memo" in info and "請對照範本檢查欄名" in info, row


def test_the_import_kind_map_covers_every_export() -> None:
    assert set(_EXPECTED) == set(IMPORT_KIND)
