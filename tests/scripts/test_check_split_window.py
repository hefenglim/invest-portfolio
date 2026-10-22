"""``scripts/check_split_window.py`` on a temp database (owner ruling 2026-10-20).

The script finds the ``prices`` rows the exchange-date window disagrees with — rows stored
under the old Taipei-date window that no later fetch rewrote — and with ``--apply`` restates
them through the app's ONE restatement seam (``api.instrument_service.restate_price_basis``,
which also clears the symbols' derived signal rows). The ledger below holds one row of each
shape:

=================  ====================================================================  ==========
row                what it is                                                            expected
=================  ====================================================================  ==========
AAA 2026-06-12     re-fetched during the ex-date session: restated close x 20            kept
AAA 2026-06-15     scheduled US fetch Mon 16:30 New York, stored under the OLD window:   restated
                   400 x 20 = 8000, basis 20
BBB 2026-06-15     same fetch instant, no SPLIT on BBB                                   kept
TTT 2026-06-12     TW, fetched on its own day (+08:00), as traded                         kept
TTT 2026-06-15     TW, stamped 18:00 UTC (02:00 Taipei on the ex-date), as traded: the   SUSPECT,
                   new window reads the split as folded, 12.5 -> 250.0                    skipped
=================  ====================================================================  ==========
"""

from __future__ import annotations

import gzip
import sqlite3
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.api import instrument_service
from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.store import insert_corporate_action
from portfolio_dash.ops import backup as ops_backup
from portfolio_dash.pricing.reconcile import Restatement
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.pricing.store import SplitFactorFn, upsert_prices
from portfolio_dash.scheduler.jobs import split_factor_fn
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.db import connect_path
from portfolio_dash.shared.enums import Market
from portfolio_dash.strategy import signal_history, signal_states
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


def _signals(path: Path) -> tuple[set[str], set[str]]:
    conn = sqlite3.connect(path)
    try:
        return ({str(r[0]) for r in conn.execute("SELECT symbol FROM signal_history")},
                {str(r[0]) for r in conn.execute("SELECT symbol FROM signal_states")})
    finally:
        conn.close()


def _split(conn: sqlite3.Connection, symbol: str, account: str) -> None:
    insert_corporate_action(
        conn, account_id=account, action_date=_EX, kind=CorporateActionKind.SPLIT,
        from_symbol=symbol, to_symbol=symbol, ratio_to=Decimal("20"), ratio_from=Decimal("1"),
    )


def _old_window(conn: sqlite3.Connection, bound: date) -> SplitFactorFn:
    real = split_factor_fn(conn)

    def factor_of(symbol: str, *, after: date, through: date) -> Decimal:
        """The pre-ruling bound: ``fetched_at.date()`` on the stamp as written."""
        return real(symbol, after=after, through=bound)

    return factor_of


def _price(conn: sqlite3.Connection, symbol: str, market: Market, day: date, close: str,
           at: datetime, *, old_bound: date | None = None) -> None:
    factor_of = (split_factor_fn(conn) if old_bound is None
                 else _old_window(conn, old_bound))
    upsert_prices(conn, [PriceRow(instrument=symbol, market=market, as_of=day,
                                  close=Decimal(close), source="test")],
                  fetched_at=at, factor_of=factor_of)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "ledger.db"
    conn = connect_path(path)
    bootstrap_db(conn)
    create_pricing_tables(conn)
    _split(conn, "AAA", "schwab")
    _split(conn, "TTT", "tw_broker")
    _price(conn, "AAA", Market.US, date(2026, 6, 12), "19.5", _IN_SESSION)
    _price(conn, "AAA", Market.US, _LAST, "400", _FIRED, old_bound=_FIRED.date())
    _price(conn, "BBB", Market.US, _LAST, "400", _FIRED, old_bound=_FIRED.date())
    _price(conn, "TTT", Market.TW, date(2026, 6, 12), "12.4",
           datetime(2026, 6, 12, 14, 0, tzinfo=TAIPEI))
    utc_stamp = datetime(2026, 6, 15, 18, 0, tzinfo=UTC)
    _price(conn, "TTT", Market.TW, _LAST, "12.5", utc_stamp, old_bound=utc_stamp.date())
    signal_history.ensure_table(conn)
    signal_states.ensure_table(conn)
    for s in ("AAA", "BBB", "TTT"):
        conn.execute("INSERT INTO signal_history (symbol, as_of, params_version, updated_at) "
                     "VALUES (?, '2026-06-15', 'v1', '2026-06-16T04:31:00+08:00')", (s,))
        conn.execute("INSERT INTO signal_states (symbol, params_version, as_of, updated_at) "
                     "VALUES (?, 'v1', '2026-06-15', '2026-06-16T04:31:00+08:00')", (s,))
    conn.commit()
    conn.close()
    assert _row(path, "AAA", _LAST) == ("8000", "20")  # premise: the defect's bytes
    assert _row(path, "TTT", _LAST) == ("12.5", "1")   # premise: right under the old window
    return path


def _main(argv: list[str]) -> int:
    return script.main([*argv, "--busy-timeout", "0.2"])


def test_the_report_lists_us_and_tw_my_rows_apart_and_writes_nothing(
    db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    before = _rows(db)
    assert _main(["--db", str(db)]) == script.EXIT_OK
    out = capsys.readouterr().out
    assert "Symbols with a SPLIT: 2 (AAA, TTT)" in out
    us, tw_my = out.split("TW / MY rows", 1)
    assert "US rows: 1" in us
    assert (f"  AAA 2026-06-15 US  fetched_at {_FIRED.isoformat()}  basis 20 -> 1  "
            "close 8000 -> 400  [previous as-traded session 2026-06-12: 390.0]") in us
    assert "AAA 2026-06-12 US" not in out and "BBB 2026" not in out
    assert ": 1 (their fetched_at is not +08:00" in tw_my
    assert ("  SUSPECT TTT 2026-06-15 TW  fetched_at 2026-06-15T18:00:00+00:00  basis 1 -> 20  "
            "close 12.5 -> 250.0  [previous as-traded session 2026-06-12: 12.4]") in tw_my
    assert "re-fetch TTT's history from 2026-06-15" in out
    assert "--apply" in out
    assert _rows(db) == before
    assert _signals(db) == ({"AAA", "BBB", "TTT"}, {"AAA", "BBB", "TTT"})
    assert not (tmp_path / "snapshots").exists()


def test_a_symbol_filter_lists_only_that_symbol(
    db: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    assert _main(["--db", str(db), "--symbol", "AAA"]) == script.EXIT_OK
    out = capsys.readouterr().out
    assert "AAA 2026-06-15 US" in out and "TTT 2026" not in out


def test_apply_backs_up_restates_clears_signals_and_skips_a_suspect_symbol(
    db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    kept = [r for r in _rows(db) if (r[0], r[2]) != ("AAA", _LAST.isoformat())]
    backups = tmp_path / "backups"
    rc = _main(["--db", str(db), "--apply", "--backup-dir", str(backups)])
    out = capsys.readouterr().out
    assert len(list(backups.glob("pre_split_window_*.db.gz"))) == 1
    assert "Backup verified" in out
    assert "Skipped TTT: SUSPECT row(s)" in out
    assert "  AAA 2026-06-15: close 8000 -> 400, basis 20 -> 1" in out
    assert "Restated 1 row(s); rows still differing: 1." in out
    assert rc == script.EXIT_ROWS_LEFT
    assert _row(db, "AAA", _LAST) == ("400", "1")
    assert [r for r in _rows(db) if (r[0], r[2]) != ("AAA", _LAST.isoformat())] == kept
    # W6 / AI-D27: the restated symbol's derived signal rows are gone, the others are kept.
    assert _signals(db) == ({"BBB", "TTT"}, {"BBB", "TTT"})


def test_apply_on_the_clean_symbols_only_leaves_nothing_and_exits_zero(
    db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    rc = _main(["--db", str(db), "--apply", "--symbol", "AAA",
                "--backup-dir", str(tmp_path / "backups")])
    out = capsys.readouterr().out
    assert "Restated 1 row(s); rows still differing: 0." in out
    assert rc == script.EXIT_OK
    assert _main(["--db", str(db), "--symbol", "AAA"]) == script.EXIT_OK
    assert "Nothing to restate." in capsys.readouterr().out


def test_include_suspect_restates_the_suspect_rows_too(
    db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    rc = _main(["--db", str(db), "--apply", "--include-suspect",
                "--backup-dir", str(tmp_path / "backups")])
    assert "Restated 2 row(s); rows still differing: 0." in capsys.readouterr().out
    assert rc == script.EXIT_OK
    assert _row(db, "TTT", _LAST) == ("250.0", "20")


def test_a_held_write_lock_is_refused_with_its_own_exit_code_and_nothing_written(
    db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    before = _rows(db)
    holder = connect_path(db)
    holder.execute("BEGIN IMMEDIATE")
    try:
        rc = _main(["--db", str(db), "--apply", "--symbol", "AAA",
                    "--backup-dir", str(tmp_path / "backups")])
    finally:
        holder.rollback()
        holder.close()
    assert rc == script.EXIT_LOCKED
    assert "write lock" in capsys.readouterr().err
    assert _rows(db) == before
    assert _signals(db) == ({"AAA", "BBB", "TTT"}, {"AAA", "BBB", "TTT"})
    assert not (tmp_path / "backups").exists()


def test_an_unverifiable_backup_is_refused_and_nothing_written(
    db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_snapshot(*, prefix: str, db_path: Path | None = None,
                        backup_dir: Path | None = None, now: datetime) -> Path:
        assert backup_dir is not None
        backup_dir.mkdir(parents=True, exist_ok=True)
        target = backup_dir / f"{prefix}broken.db.gz"
        with gzip.open(target, "wb") as gz:
            gz.write(b"not a database")
        return target

    monkeypatch.setattr(ops_backup, "pre_write_snapshot", broken_snapshot)
    before = _rows(db)
    rc = _main(["--db", str(db), "--apply", "--symbol", "AAA",
                "--backup-dir", str(tmp_path / "backups")])
    assert rc == script.EXIT_BACKUP_UNVERIFIED
    assert "could not be verified" in capsys.readouterr().err
    assert _rows(db) == before


def test_rows_still_differing_after_apply_exit_non_zero(
    db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def restates_nothing(conn: sqlite3.Connection, symbols: list[str]) -> list[Restatement]:
        return []

    monkeypatch.setattr(instrument_service, "restate_price_basis", restates_nothing)
    rc = _main(["--db", str(db), "--apply", "--symbol", "AAA",
                "--backup-dir", str(tmp_path / "backups")])
    assert "rows still differing: 1." in capsys.readouterr().out
    assert rc == script.EXIT_ROWS_LEFT


def test_a_batch_the_provider_had_already_restated_flags_every_row(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """An owner refresh at Taipei 06-16 10:00 (New York 06-15 22:00) after the provider had
    restated: the OLD window folded 19.5 / 19.8 / 20.0 correctly to 390 / 396 / 400. Each row
    is compared with the last session the new window AGREES with (06-10, 380), so all three
    show the ratio jump, not only the first."""
    path = tmp_path / "early.db"
    conn = connect_path(path)
    bootstrap_db(conn)
    create_pricing_tables(conn)
    _split(conn, "AAA", "schwab")
    _price(conn, "AAA", Market.US, date(2026, 6, 10), "380",
           datetime.combine(date(2026, 6, 10), time(16, 30), tzinfo=NEW_YORK))
    owner = datetime(2026, 6, 16, 10, 0, tzinfo=TAIPEI)
    for day, close in ((date(2026, 6, 11), "19.5"), (date(2026, 6, 12), "19.8"),
                       (_LAST, "20.0")):
        _price(conn, "AAA", Market.US, day, close, owner, old_bound=owner.date())
    conn.close()
    assert _main(["--db", str(path)]) == script.EXIT_OK
    out = capsys.readouterr().out
    flagged = [line for line in out.splitlines() if line.startswith("  SUSPECT AAA")]
    assert len(flagged) == 3
    assert all("[previous as-traded session 2026-06-10: 380]" in line for line in flagged)
    assert "re-fetch AAA's history from 2026-06-11" in out


def test_a_row_with_no_earlier_session_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "alone.db"
    conn = connect_path(path)
    bootstrap_db(conn)
    create_pricing_tables(conn)
    _split(conn, "AAA", "schwab")
    _price(conn, "AAA", Market.US, _LAST, "400", _FIRED, old_bound=_FIRED.date())
    conn.close()
    assert _main(["--db", str(path)]) == script.EXIT_OK
    assert "[no earlier as-traded session to compare]" in capsys.readouterr().out


def test_a_missing_path_is_refused_and_never_created(tmp_path: Path) -> None:
    missing = tmp_path / "nope.db"
    assert _main(["--db", str(missing)]) == script.EXIT_REFUSED
    assert not missing.exists()


def test_a_database_without_prices_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "ledger_only.db"
    conn = connect_path(path)
    bootstrap_db(conn)
    conn.commit()
    conn.close()
    assert _main(["--db", str(path), "--apply"]) == script.EXIT_REFUSED
