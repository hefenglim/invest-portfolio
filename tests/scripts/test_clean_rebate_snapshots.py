"""DEF-010 data half: ``scripts/clean_rebate_snapshots.py`` on a temp database.

The demo holds ten TW trades whose ``fee_rule_snapshot`` claims both benefits
(``discount 0.23`` + ``rebate_rate 0.77``); the owner ruled they be cleaned with a backup and
an audit record. The fee of record must NOT move — only the claim that never happened (a
refund on top of a discounted fee). Everything the script promises is exercised here,
including the refusals and the re-run.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import insert_cash_movement, insert_transaction
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.enums import Side
from scripts import clean_rebate_snapshots as script

_DOUBLE = {"engine": "v2", "brokerage": "0.001425", "discount": "0.23", "min_fee": "20",
           "rebate_rate": "0.77", "rounding": "floor"}


def _tx(conn: sqlite3.Connection, d: date, fee: str, snap: dict[str, str]) -> int:
    return insert_transaction(
        conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
        quantity=Decimal("100"), price=Decimal("500"), fees=Decimal(fee),
        tax=Decimal("0"), trade_date=d, fee_rule_snapshot=snap)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[tuple[Path, dict[str, int]]]:
    path = tmp_path / "demo.db"
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    bootstrap_db(conn)
    seed_accounts(conn)
    ids = {
        "jan": _tx(conn, date(2026, 1, 20), "20", _DOUBLE),      # month already credited
        "feb": _tx(conn, date(2026, 2, 10), "199", _DOUBLE),
        "jul": _tx(conn, date(2026, 7, 3), "419", _DOUBLE),
        "clean": _tx(conn, date(2026, 7, 4), "142", {**_DOUBLE, "discount": "1"}),
        "supplied": _tx(conn, date(2026, 7, 5), "20", {"engine": "supplied", "fee": "20"}),
    }
    # The January rebate credit (the demo's cash #34): dated the 1st of the refund month.
    insert_cash_movement(conn, account_id="tw_broker", move_date=date(2026, 2, 1),
                         kind="REBATE", ccy=Currency.TWD, amount=Decimal("191"),
                         note="2026-01 折讓款")
    conn.commit()
    conn.close()
    yield path, ids


def _open(path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    return c


def _snap(path: Path, txn_id: int) -> dict[str, str]:
    with _open(path) as c:
        row = c.execute("SELECT fee_rule_snapshot FROM transactions WHERE id=?",
                        (txn_id,)).fetchone()
    out: dict[str, str] = json.loads(row[0])
    return out


def _money(path: Path) -> list[tuple[object, ...]]:
    with _open(path) as c:
        return [tuple(r) for r in c.execute(
            "SELECT id, fees, tax, quantity, price, trade_date FROM transactions ORDER BY id")]


def _audit(path: Path) -> list[sqlite3.Row]:
    with _open(path) as c:
        return c.execute("SELECT * FROM ledger_audit ORDER BY id").fetchall()


def test_a_dry_run_writes_nothing(db: tuple[Path, dict[str, int]]) -> None:
    path, ids = db
    before = path.read_bytes()
    assert script.main(["--db", str(path), "--scope", "all"]) == 0
    assert path.read_bytes() == before
    assert _snap(path, ids["feb"])["rebate_rate"] == "0.77"


def test_apply_all_cleans_every_incoherent_row_and_never_touches_the_money(
    db: tuple[Path, dict[str, int]],
) -> None:
    path, ids = db
    money = _money(path)
    assert script.main(["--db", str(path), "--scope", "all", "--apply"]) == 0
    for key in ("jan", "feb", "jul"):
        snap = _snap(path, ids[key])
        assert snap["rebate_rate"] == "0" and snap["rebate_rate_was"] == "0.77"
        assert snap["discount"] == "0.23"            # what HAPPENED stays
        assert "DEF-010" in snap["cleaned"]
    assert _snap(path, ids["clean"])["rebate_rate"] == "0.77"      # coherent: untouched
    assert "rebate_rate" not in _snap(path, ids["supplied"])        # silent: untouched
    assert _money(path) == money                     # fee / tax / qty / price / date unchanged
    audit = _audit(path)
    assert sorted(int(a["row_id"]) for a in audit) == sorted(
        ids[k] for k in ("jan", "feb", "jul"))
    assert {a["table_name"] for a in audit} == {"transactions"}
    assert {a["action"] for a in audit} == {"update"}
    feb = next(a for a in audit if int(a["row_id"]) == ids["feb"])
    before_row = json.loads(feb["before_json"])
    assert before_row["fees"] == "199"               # the FULL pre-image, not a diff
    assert json.loads(before_row["fee_rule_snapshot"])["rebate_rate"] == "0.77"
    with _open(path) as c:
        log = c.execute("SELECT action, path FROM action_log").fetchall()
    assert len(log) == 1 and "費率快照清理" in log[0]["action"]


def test_scope_uncredited_leaves_the_already_credited_month(
    db: tuple[Path, dict[str, int]],
) -> None:
    path, ids = db
    assert script.main(["--db", str(path), "--scope", "uncredited", "--apply"]) == 0
    assert _snap(path, ids["jan"])["rebate_rate"] == "0.77"          # Jan already credited
    assert _snap(path, ids["feb"])["rebate_rate"] == "0"
    assert _snap(path, ids["jul"])["rebate_rate"] == "0"
    assert len(_audit(path)) == 2


def test_a_rerun_is_a_no_op(db: tuple[Path, dict[str, int]]) -> None:
    path, _ids = db
    assert script.main(["--db", str(path), "--scope", "all", "--apply"]) == 0
    after_first = path.read_bytes()
    assert script.main(["--db", str(path), "--scope", "all", "--apply"]) == 0
    assert path.read_bytes() == after_first
    assert len(_audit(path)) == 3


def test_id_narrows_and_an_id_outside_the_set_is_refused(
    db: tuple[Path, dict[str, int]],
) -> None:
    path, ids = db
    before = path.read_bytes()
    assert script.main(["--db", str(path), "--scope", "all", "--id", str(ids["clean"]),
                        "--apply"]) == 2
    assert path.read_bytes() == before
    assert script.main(["--db", str(path), "--scope", "all", "--id", str(ids["feb"]),
                        "--apply"]) == 0
    assert _snap(path, ids["feb"])["rebate_rate"] == "0"
    assert _snap(path, ids["jul"])["rebate_rate"] == "0.77"


def test_refuses_a_missing_database_without_creating_it(tmp_path: Path) -> None:
    missing = tmp_path / "nope.db"
    assert script.main(["--db", str(missing), "--scope", "all"]) == 2
    assert not missing.exists()


def test_refuses_without_db_or_scope() -> None:
    with pytest.raises(SystemExit):
        script.main(["--scope", "all"])
    with pytest.raises(SystemExit):
        script.main(["--db", "x.db"])
