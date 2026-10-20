"""``scripts/check_split_window.py`` on a temp database (owner ruling 2026-10-20).

The script finds the ``prices`` rows the exchange-date window disagrees with — rows stored
under the old Taipei-date window that no later fetch rewrote — and with ``--apply`` restates
them through the app's own reconcile. The ledger below holds one row of each shape:

=====================  ==============================================================  ==========
row                    what it is                                                      expected
=====================  ==============================================================  ==========
AAA 2026-06-15         scheduled US fetch Mon 16:30 New York, stored under the OLD      restated
                       window: 400 x 20 = 8000, basis 20
AAA 2026-06-12         re-fetched during the ex-date session: restated close x 20       kept
BBB 2026-06-15         same fetch instant, no SPLIT on BBB                              kept
=====================  ==============================================================  ==========
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.store import insert_corporate_action
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.scheduler.jobs import split_factor_fn
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.db import connect_path
from portfolio_dash.shared.enums import Market
from scripts import check_split_window as script

TAIPEI = ZoneInfo("Asia/Taipei")
NEW_YORK = ZoneInfo("America/New_York")
_EX = date(2026, 6, 16)
_LAST = date(2026, 6, 15)
_FIRED = datetime.combine(_LAST, time(16, 30), tzinfo=NEW_YORK).astimezone(TAIPEI)
_IN_SESSION = datetime.combine(_EX, time(11, 0), tzinfo=NEW_YORK).astimezone(TAIPEI)


def _rows(path: Path) -> list[tuple[object, ...]]:
    conn = sqlite3.connect(path)
    try:
        return [tuple(r) for r in conn.execute(
            "SELECT * FROM prices ORDER BY instrument, as_of_date")]
    finally:
        conn.close()


def _row(path: Path, symbol: str, day: date) -> tuple[str, str]:
    conn = sqlite3.connect(path)
    try:
        r = conn.execute("SELECT close, split_basis FROM prices WHERE instrument=? AND "
                         "as_of_date=?", (symbol, day.isoformat())).fetchone()
    finally:
        conn.close()
    assert r is not None
    return str(r[0]), str(r[1])


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "ledger.db"
    conn = connect_path(path)
    bootstrap_db(conn)
    create_pricing_tables(conn)
    insert_corporate_action(
        conn, account_id="schwab", action_date=_EX, kind=CorporateActionKind.SPLIT,
        from_symbol="AAA", to_symbol="AAA", ratio_to=Decimal("20"), ratio_from=Decimal("1"),
    )
    real = split_factor_fn(conn)

    def old_window(symbol: str, *, after: date, through: date) -> Decimal:
        """The pre-ruling bound: the APP clock's (Taipei) date of the fetch."""
        return real(symbol, after=after, through=_FIRED.date())

    upsert_prices(conn, [PriceRow(instrument=s, market=Market.US, as_of=_LAST,
                                  close=Decimal("400"), source="test") for s in ("AAA", "BBB")],
                  fetched_at=_FIRED, factor_of=old_window)
    upsert_prices(conn, [PriceRow(instrument="AAA", market=Market.US, as_of=date(2026, 6, 12),
                                  close=Decimal("19.5"), source="test")],
                  fetched_at=_IN_SESSION, factor_of=real)
    conn.commit()
    conn.close()
    assert _row(path, "AAA", _LAST) == ("8000", "20")  # premise: the defect's bytes
    return path


def test_the_report_lists_the_folded_row_and_writes_nothing(
    db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    before = _rows(db)
    assert script.main(["--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "Symbols with a SPLIT: 1 (AAA)" in out
    assert "Rows whose stored basis differs from the exchange-date window: 1" in out
    assert (f"  AAA 2026-06-15 US  fetched_at {_FIRED.isoformat()}  basis 20 -> 1  "
            "close 8000 -> 400  [previous session 2026-06-12: 390.0]") in out
    assert "do not --apply" in out
    assert "AAA 2026-06-12 US" not in out and "BBB 2026" not in out
    assert "--apply" in out
    assert _rows(db) == before
    assert not (tmp_path / "snapshots").exists()


def test_apply_backs_up_restates_only_the_folded_row_and_leaves_none(
    db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    kept = [r for r in _rows(db) if (r[0], r[2]) != ("AAA", _LAST.isoformat())]
    backups = tmp_path / "backups"
    assert script.main(["--db", str(db), "--apply", "--backup-dir", str(backups)]) == 0
    out = capsys.readouterr().out
    assert len(list(backups.glob("pre_split_window_*.db.gz"))) == 1
    assert "  AAA 2026-06-15: close 8000 -> 400, basis 20 -> 1" in out
    assert "Restated 1 row(s); rows still differing: 0." in out
    assert _row(db, "AAA", _LAST) == ("400", "1")
    assert [r for r in _rows(db) if (r[0], r[2]) != ("AAA", _LAST.isoformat())] == kept

    assert script.main(["--db", str(db)]) == 0
    again = capsys.readouterr().out
    assert "Rows whose stored basis differs from the exchange-date window: 0" in again
    assert "Nothing to restate." in again


def test_a_missing_path_is_refused_and_never_created(tmp_path: Path) -> None:
    missing = tmp_path / "nope.db"
    assert script.main(["--db", str(missing)]) == script._REFUSED
    assert not missing.exists()


def test_a_database_without_prices_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "ledger_only.db"
    conn = connect_path(path)
    bootstrap_db(conn)
    conn.commit()
    conn.close()
    assert script.main(["--db", str(path), "--apply"]) == script._REFUSED
