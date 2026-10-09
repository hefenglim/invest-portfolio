"""DEF-063 (owner ruling ④, 2026-09-25): ``scripts/clean_orphan_seed_prices.py`` on a temp DB.

R2 / R3 left SPINOFF seed prices on demo that no corporate action owns any more; since R4 the
seed writer only fills EMPTY slots and a delete takes back only its RECORDED row, so nothing in
the app ever removes them. The script removes exactly those — and the ownership test it applies
is ``pricing.seed.owned_seed_slot``, the one the SPINOFF delete / undo / edit ask.

The ledger below holds one row of every shape the script must tell apart:

====================  ==================================================  =============
slot                  what it is                                           expected
====================  ==================================================  =============
ORPH 2026-03-02       seed signature, no action at all                     DELETED
NONE 2026-03-07       seed signature; its SPINOFF recorded writing NOTHING DELETED
NEWO 2026-03-03       seed owned through ``child_seed_json``               kept
LEGA 2026-03-04       seed owned by a legacy SPINOFF (NULL record, R3)     kept
ORPH 2026-03-05       a provider's quote (source yfinance)                 kept
ORPH 2026-03-06       ``manual`` but fetched_at 09:15 — broken signature   kept
====================  ==================================================  =============
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    ChildSeedRecord,
    insert_corporate_action,
    upsert_instrument,
)
from portfolio_dash.ops import backup as ops_backup
from portfolio_dash.pricing import defaults as pricing_defaults
from portfolio_dash.pricing.refs import InstrumentRef
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from scripts import clean_orphan_seed_prices as script

_TZ = ZoneInfo("Asia/Taipei")
_ORPHANS = {("ORPH", "2026-03-02"), ("NONE", "2026-03-07")}
_KEPT = {("NEWO", "2026-03-03"), ("LEGA", "2026-03-04"), ("ORPH", "2026-03-05"),
         ("ORPH", "2026-03-06")}


def _price(conn: sqlite3.Connection, sym: str, day: date, close: str, *, source: str,
           fetched_at: datetime) -> None:
    upsert_prices(conn, [PriceRow(instrument=sym, market=Market.TW, as_of=day,
                                  close=Decimal(close), source=source)],
                  fetched_at=fetched_at)


def _seed(conn: sqlite3.Connection, sym: str, day: date, close: str) -> None:
    """A row with the full seed signature: source manual, fetched_at = its day at 00:00."""
    _price(conn, sym, day, close, source="manual",
           fetched_at=datetime(day.year, day.month, day.day, tzinfo=_TZ))


def _spinoff(conn: sqlite3.Connection, child: str, day: date,
             record: ChildSeedRecord | None) -> int:
    return insert_corporate_action(
        conn, account_id="tw_broker", action_date=day, kind=CorporateActionKind.SPINOFF,
        from_symbol="PAR", to_symbol=child, ratio_to=Decimal("1"), ratio_from=Decimal("10"),
        cost_carry=Decimal("0.1"), child_seed=record)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "demo.db"
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    bootstrap_db(conn)
    create_pricing_tables(conn)
    seed_accounts(conn)
    for sym in ("PAR", "ORPH", "NONE", "NEWO", "LEGA"):
        upsert_instrument(conn, Instrument(symbol=sym, market=Market.TW, quote_ccy=Currency.TWD,
                                           sector="Tech", name=sym, board="TWSE"))
    _seed(conn, "ORPH", date(2026, 3, 2), "11")
    _seed(conn, "NONE", date(2026, 3, 7), "12")
    _seed(conn, "NEWO", date(2026, 3, 3), "13")
    _seed(conn, "LEGA", date(2026, 3, 4), "14")
    _price(conn, "ORPH", date(2026, 3, 5), "15", source="yfinance",
           fetched_at=datetime(2026, 3, 5, 14, 30, tzinfo=_TZ))
    _price(conn, "ORPH", date(2026, 3, 6), "16", source="manual",
           fetched_at=datetime(2026, 3, 6, 9, 15, tzinfo=_TZ))
    # NEWO: owned through the R4 record. NONE: its SPINOFF recorded writing nothing (the slot
    # was already taken when it was saved), so it owns nothing and the row there is an orphan.
    _spinoff(conn, "NEWO", date(2026, 3, 3),
             ChildSeedRecord(close=Decimal("13"), symbol="NEWO", as_of=date(2026, 3, 3)))
    _spinoff(conn, "NONE", date(2026, 3, 7), ChildSeedRecord())
    # LEGA: a SPINOFF saved before the record existed — child_seed_json NULL (R3's rule).
    legacy = _spinoff(conn, "LEGA", date(2026, 3, 4), None)
    conn.execute("UPDATE corporate_actions SET child_seed_json = NULL WHERE id = ?", (legacy,))
    conn.commit()
    conn.close()
    yield path


def _open(path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    return c


def _slots(path: Path) -> set[tuple[str, str]]:
    with _open(path) as c:
        return {(r[0], r[1]) for r in c.execute("SELECT instrument, as_of_date FROM prices")}


def _rows(path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    with _open(path) as c:
        return {(r["instrument"], r["as_of_date"]): dict(r)
                for r in c.execute("SELECT * FROM prices")}


def _count(path: Path, table: str) -> int:
    with _open(path) as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                         (table,)).fetchone():
            return 0
        return int(c.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def _apply(path: Path, *extra: str) -> int:
    return script.main(["--db", str(path), "--apply", "--reason", "DEF-063 測試", *extra])


def test_dry_run_lists_the_orphans_with_reasons_and_writes_nothing(
    db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = _rows(db)
    assert script.main(["--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "孤兒起始價：2 筆" in out
    for sym, day in _ORPHANS:
        assert f"→ 刪除 {sym} {day}" in out
    assert "保留：仍被擁有" in out and "舊格式分拆" in out and "child_seed_json 記錄" in out
    assert "保留：簽章不完整" in out
    assert "ORPH 2026-03-05" not in out  # a provider row is not even a candidate
    assert _rows(db) == before
    assert _count(db, "action_log") == 0
    assert not (db.parent / "snapshots").exists()


def test_apply_deletes_exactly_the_orphans(db: Path) -> None:
    assert _apply(db) == 0
    left = _slots(db)
    assert not _ORPHANS & left
    assert left >= _KEPT


@pytest.mark.parametrize("slot", sorted(_KEPT))
def test_every_non_orphan_survives_apply_byte_identically(
    db: Path, slot: tuple[str, str]
) -> None:
    before = _rows(db)[slot]
    assert _apply(db) == 0
    assert _rows(db)[slot] == before


def test_audit_rows_carry_the_full_deleted_row_and_one_action_log(
    db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = _rows(db)
    audits_before = _count(db, "ledger_audit")
    assert _apply(db) == 0
    out = capsys.readouterr().out
    with _open(db) as c:
        audits = c.execute(
            "SELECT * FROM ledger_audit WHERE table_name = 'prices' ORDER BY id").fetchall()
        logs = c.execute("SELECT * FROM action_log").fetchall()
    assert len(audits) == len(_ORPHANS) == _count(db, "ledger_audit") - audits_before
    for a in audits:
        sym, day = str(a["row_id"]).split("/")
        assert (sym, day) in _ORPHANS
        assert a["action"] == "delete"
        assert "clean_orphan_seed_prices" in str(a["source"])
        assert json.loads(a["before_json"]) == before[(sym, day)]  # the WHOLE original row
    assert len(logs) == 1
    assert logs[0]["username"] == "script" and "DEF-063 測試" in logs[0]["action"]
    assert f"action_log #{logs[0]['id']}" in out


def test_the_backup_exists_before_the_first_delete(
    db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, Any] = {}
    real = ops_backup.pre_write_snapshot

    def spy(**kw: Any) -> Path:
        seen["orphans_present_at_backup"] = _ORPHANS <= _slots(db)
        path = real(**kw)
        seen["path"] = path
        return path

    monkeypatch.setattr(ops_backup, "pre_write_snapshot", spy)
    assert _apply(db) == 0
    assert seen["orphans_present_at_backup"] is True
    snap = Path(seen["path"])
    assert snap.is_file() and snap.name.startswith("pre_clean_orphan_seed_")
    assert str(snap) in capsys.readouterr().out
    restored = db.parent / "restored.db"
    restored.write_bytes(gzip.decompress(snap.read_bytes()))
    assert _ORPHANS <= _slots(restored)  # the snapshot still holds what was deleted


def test_rerun_is_a_no_op(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _apply(db) == 0
    capsys.readouterr()
    audits, logs, rows = _count(db, "ledger_audit"), _count(db, "action_log"), _rows(db)
    assert script.main(["--db", str(db)]) == 0
    assert "孤兒起始價：0 筆" in capsys.readouterr().out
    assert _apply(db) == 0
    assert (_count(db, "ledger_audit"), _count(db, "action_log"), _rows(db)) == (
        audits, logs, rows)


def test_refusals_write_nothing(db: Path, tmp_path: Path) -> None:
    missing = tmp_path / "nope.db"
    assert script.main(["--db", str(missing)]) == 2
    assert not missing.exists()  # sqlite3.connect would have CREATED it
    before = _rows(db)
    assert script.main(["--db", str(db), "--apply"]) == 2  # no --reason
    assert script.main(["--db", str(db), "--apply", "--reason", "  "]) == 2
    assert _rows(db) == before


class _FakeRegistry:
    """A provider that re-delivers a real quote for every day from ``start``."""

    def fetch_quote_history(self, instruments: list[InstrumentRef], start: date
                            ) -> tuple[list[PriceRow], dict[str, str], list[str]]:
        rows = [PriceRow(instrument=r.symbol, market=r.market, as_of=start,
                         close=Decimal("99"), source="yfinance") for r in instruments]
        return rows, {r.symbol: "yfinance" for r in instruments}, []

    def fetch_quote_history_explained(
        self, instruments: list[InstrumentRef], start: date
    ) -> tuple[list[PriceRow], dict[str, str], list[str], list[str]]:
        # DEF-067 ④: ``refresh_history`` reads the explained variant (empty set apart).
        return (*self.fetch_quote_history(instruments, start), [])


def test_backfill_recovers_the_provider_quote_and_reports_rows_written(
    db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(pricing_defaults, "default_registry", lambda conn: _FakeRegistry())
    assert _apply(db, "--backfill") == 0
    out = capsys.readouterr().out
    rows = _rows(db)
    assert rows[("ORPH", "2026-03-02")]["source"] == "yfinance"
    assert rows[("NONE", "2026-03-07")]["source"] == "yfinance"
    assert "回補寫入 2 列報價" in out
    assert script.main(["--db", str(db)]) == 0  # the recovered quotes are not orphans
    assert "孤兒起始價：0 筆" in capsys.readouterr().out


class _OfflineRegistry:
    """What the VM sees with no network: every provider fails. ``raises`` = the fetch blows
    up; otherwise every symbol comes back in the ``failed`` list with no rows."""

    def __init__(self, *, raises: bool) -> None:
        self.raises = raises

    def fetch_quote_history(self, instruments: list[InstrumentRef], start: date
                            ) -> tuple[list[PriceRow], dict[str, str], list[str]]:
        if self.raises:
            raise ConnectionError("network is unreachable")
        return [], {}, [r.symbol for r in instruments]

    def fetch_quote_history_explained(
        self, instruments: list[InstrumentRef], start: date
    ) -> tuple[list[PriceRow], dict[str, str], list[str], list[str]]:
        # DEF-067 ④: ``refresh_history`` reads the explained variant (empty set apart).
        return (*self.fetch_quote_history(instruments, start), [])


@pytest.mark.parametrize("mode", ["fetch-raises", "all-failed", "no-registry"])
def test_backfill_without_network_degrades_and_keeps_the_cleanup(
    db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], mode: str
) -> None:
    """The provider seam is replaced — never a real call (yfinance's curl transport is not
    covered by pytest-socket). ``no-registry`` leaves the real ``default_registry`` in place on
    a database without the provider tables, so building it fails. Every mode exits 0 and the
    committed cleanup stands."""
    if mode != "no-registry":
        offline = _OfflineRegistry(raises=mode == "fetch-raises")
        monkeypatch.setattr(pricing_defaults, "default_registry", lambda conn: offline)
    assert _apply(db, "--backfill") == 0
    out = capsys.readouterr().out
    assert "執行歷史回補" in out
    if mode == "all-failed":
        assert "回補寫入 0 列報價" in out and "失敗" in out
    else:
        assert "回補失敗" in out and "清理已完成、不受影響" in out
    assert not _ORPHANS & _slots(db)
    with _open(db) as c:
        assert c.execute("SELECT COUNT(*) FROM action_log").fetchone()[0] == 1
