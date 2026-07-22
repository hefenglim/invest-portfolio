"""Hermetic tests for the pre/post Moomoo-merge reconciliation harness.

``scripts/merge_reconcile.py`` is loaded dynamically (it is a top-level script under
``scripts/``, not an importable package, and its module path does not match mypy's
``scripts/probe`` package base — a static import would not resolve). The module object is
typed ``Any`` so this test stays mypy-strict clean while exercising the real functions.

Coverage:

* ``run`` on a synthetic legacy-shaped DB reconciles end-to-end (only labels change).
* the snapshot is genuinely read-only (file bytes unchanged; a write is denied).
* two independent mutation cases (a cash amount; a transaction price) make ``diff`` FAIL and
  name the offending JSON path.

Network-free by construction (pytest-socket is globally armed via ``--disable-socket``); the
whole flow is local SQLite + the pure calculation core.
"""

import hashlib
import importlib.util
import shutil
import sqlite3
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.moomoo_merge import migrate_moomoo_accounts
from portfolio_dash.data_ingestion.store import (
    insert_cash_movement,
    insert_dividend,
    insert_fx_conversion,
    insert_transaction,
    upsert_instrument,
    upsert_opening,
)
from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.pricing.store import upsert_fx, upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side

# --------------------------------------------------------------------------- dynamic load

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "merge_reconcile.py"


def _load_harness() -> Any:
    spec = importlib.util.spec_from_file_location("merge_reconcile", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclasses (with `from __future__ import annotations`) can
    # resolve the module via sys.modules[cls.__module__].
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mr: Any = _load_harness()

AS_OF = datetime(2026, 7, 22, 0, 0, tzinfo=ZoneInfo("Asia/Taipei"))
_BUILD = date(2026, 1, 2)
_PRICE_DATE = date(2026, 7, 20)
_FX_EARLY = date(2026, 1, 1)  # on-or-before rate for trade-date XIRR
_FETCHED = datetime(2026, 7, 20, 12, 0, tzinfo=ZoneInfo("Asia/Taipei"))


# --------------------------------------------------------------------------- synthetic DB


def _seed_accounts(conn: sqlite3.Connection) -> None:
    """Two legacy Moomoo accounts + a schwab bystander (settlement != funding, so it also
    surfaces in fx.by_account — proving a bystander FX row stays byte-identical)."""
    conn.executemany(
        "INSERT INTO accounts (account_id, name, broker, settlement_ccy, funding_ccy, "
        "fee_rule_set, dividend_model) VALUES (?,?,?,?,?,?,?)",
        [
            ("moomoo_my_us", "Moomoo MY (US)", "Moomoo MY", "USD", "MYR", "moomoo_us", "drip_us"),
            ("moomoo_my_my", "Moomoo MY (MY)", "Moomoo MY", "MYR", "MYR", "moomoo_my", "cash"),
            ("schwab", "Charles Schwab", "Schwab", "USD", "TWD", "schwab", "drip_us"),
        ],
    )
    conn.executemany(
        "INSERT INTO account_market_rules (account_id, market, fee_rule_set, dividend_model) "
        "VALUES (?,?,?,?)",
        [
            ("moomoo_my_us", "US", "moomoo_us", "drip_us"),
            ("moomoo_my_my", "MY", "moomoo_my", "cash"),
            ("schwab", "US", "schwab", "drip_us"),
        ],
    )


def _build_legacy_db(path: Path) -> None:
    """A populated pre-merge DB with holdings/dividends/fx/cash across both legacy accounts +
    the schwab bystander, plus prices + FX so build_dashboard produces real figures."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")  # keep a single-file DB (no WAL sidecars)
    try:
        bootstrap_db(conn)
        create_pricing_tables(conn)
        _seed_accounts(conn)
        for sym, mkt, ccy in (
            ("AAPL", Market.US, Currency.USD),
            ("NVDA", Market.US, Currency.USD),
            ("1155", Market.MY, Currency.MYR),
        ):
            upsert_instrument(
                conn,
                Instrument(symbol=sym, market=mkt, quote_ccy=ccy, name=sym,
                           sector="Information Technology"),
            )

        # transactions: AAPL under moomoo_my_us (US), 1155 under moomoo_my_my (MY),
        # NVDA under the schwab bystander.
        insert_transaction(conn, account_id="moomoo_my_us", symbol="AAPL", side=Side.BUY,
                           quantity=Decimal("10"), price=Decimal("100"), fees=Decimal("1"),
                           tax=Decimal("0"), trade_date=date(2026, 1, 10))
        insert_transaction(conn, account_id="moomoo_my_my", symbol="1155", side=Side.BUY,
                           quantity=Decimal("100"), price=Decimal("9"), fees=Decimal("3"),
                           tax=Decimal("1"), trade_date=date(2026, 1, 12))
        insert_transaction(conn, account_id="schwab", symbol="NVDA", side=Side.BUY,
                           quantity=Decimal("5"), price=Decimal("200"), fees=Decimal("2"),
                           tax=Decimal("0"), trade_date=date(2026, 1, 11))
        # a realized sell on 1155 (exercises realized P&L continuity)
        insert_transaction(conn, account_id="moomoo_my_my", symbol="1155", side=Side.SELL,
                           quantity=Decimal("40"), price=Decimal("11"), fees=Decimal("2"),
                           tax=Decimal("1"), trade_date=date(2026, 3, 15))

        # dividends: MY cash (net) under moomoo_my_my; a TW-style cash reduction is not needed.
        insert_dividend(conn, account_id="moomoo_my_my", symbol="1155", div_date=date(2026, 4, 5),
                        div_type="NET", gross=Decimal("50"), withholding=Decimal("0"),
                        net=Decimal("50"))

        # fx conversions: Moomoo funds USD from MYR; Schwab funds USD from TWD.
        insert_fx_conversion(conn, account_id="moomoo_my_us", date=date(2026, 1, 8),
                             from_ccy=Currency.MYR, from_amount=Decimal("4400"),
                             to_ccy=Currency.USD, to_amount=Decimal("1000"))
        insert_fx_conversion(conn, account_id="schwab", date=date(2026, 1, 6),
                             from_ccy=Currency.TWD, from_amount=Decimal("32000"),
                             to_ccy=Currency.USD, to_amount=Decimal("1000"))

        # cash movements (both legacy accounts + bystander)
        insert_cash_movement(conn, account_id="moomoo_my_us", move_date=date(2026, 1, 5),
                             kind="deposit", ccy=Currency.USD, amount=Decimal("1000"))
        insert_cash_movement(conn, account_id="moomoo_my_my", move_date=date(2026, 1, 5),
                             kind="deposit", ccy=Currency.MYR, amount=Decimal("5000"))
        insert_cash_movement(conn, account_id="schwab", move_date=date(2026, 1, 4),
                             kind="deposit", ccy=Currency.TWD, amount=Decimal("50000"))

        # opening inventory (distinct symbols per account -> no PK collision on merge)
        upsert_opening(conn, account_id="moomoo_my_us", symbol="AAPL", shares=Decimal("5"),
                       original_cost_total=Decimal("500"), build_date=_BUILD)
        upsert_opening(conn, account_id="schwab", symbol="NVDA", shares=Decimal("3"),
                       original_cost_total=Decimal("540"), build_date=_BUILD)

        # prices (recent) + an early FX row (trade-date/on-or-before) + a recent FX row (current)
        upsert_prices(
            conn,
            [
                PriceRow(instrument="AAPL", market=Market.US, as_of=_PRICE_DATE,
                         close=Decimal("130.00"), source="test"),
                PriceRow(instrument="NVDA", market=Market.US, as_of=_PRICE_DATE,
                         close=Decimal("210.00"), source="test"),
                PriceRow(instrument="1155", market=Market.MY, as_of=_PRICE_DATE,
                         close=Decimal("9.500"), source="test"),
            ],
            fetched_at=_FETCHED,
        )
        fx_rows: list[FxRow] = []
        for as_of in (_FX_EARLY, _PRICE_DATE):
            fx_rows += [
                FxRow(base=Currency.USD, quote=Currency.TWD, as_of=as_of,
                      rate=Decimal("32.000000"), source="test"),
                FxRow(base=Currency.MYR, quote=Currency.TWD, as_of=as_of,
                      rate=Decimal("7.000000"), source="test"),
                FxRow(base=Currency.USD, quote=Currency.MYR, as_of=as_of,
                      rate=Decimal("4.400000"), source="test"),
            ]
        upsert_fx(conn, fx_rows, fetched_at=_FETCHED)
        conn.commit()
    finally:
        conn.close()


def _snapshot(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = mr.snapshot_db(path, as_of=AS_OF, reporting=Currency.TWD)
    return result


def _migrate(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        assert migrate_moomoo_accounts(conn) is True
        conn.commit()
    finally:
        conn.close()


def _tamper(path: Path, sql: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(sql)
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- happy path


def test_run_reconciles_end_to_end(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    _build_legacy_db(db)

    result = mr.run_reconcile(db, as_of=AS_OF, reporting=Currency.TWD)

    assert result.migrated is True
    assert result.mismatches == [], [
        (m.path, m.pre, m.post) for m in result.mismatches
    ]
    # The reconciliation is meaningful only if it actually captured figures: the pre snapshot
    # must carry the two legacy accounts, and the post the merged one.
    assert "moomoo_my_us" in result.pre["raw"]["accounts"]
    assert "moomoo_my_my" in result.pre["raw"]["accounts"]
    assert set(result.post["raw"]["accounts"]) == {"moomoo_my", "schwab"}
    # a real market value was computed (not a degenerate all-None snapshot)
    assert result.pre["engine"]["kpis"]["total_market_value"] is not None


def test_run_leaves_input_file_untouched(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    _build_legacy_db(db)
    before = hashlib.sha256(db.read_bytes()).hexdigest()

    mr.run_reconcile(db, as_of=AS_OF, reporting=Currency.TWD)

    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    # input still pre-migration (legacy accounts intact)
    conn = sqlite3.connect(db)
    try:
        ids = {r[0] for r in conn.execute("SELECT account_id FROM accounts")}
    finally:
        conn.close()
    assert {"moomoo_my_us", "moomoo_my_my"} <= ids


# --------------------------------------------------------------------------- read-only proof


def test_snapshot_does_not_write_the_file(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    _build_legacy_db(db)
    before = hashlib.sha256(db.read_bytes()).hexdigest()

    _snapshot(db)

    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    # no WAL/SHM sidecars were created by the read-only open
    assert not (tmp_path / "legacy.db-wal").exists()
    assert not (tmp_path / "legacy.db-shm").exists()


def test_readonly_connection_denies_writes(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    _build_legacy_db(db)
    conn = mr._open_readonly(db)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(
                "INSERT INTO cash_movements (account_id, date, kind, ccy, amount) "
                "VALUES ('schwab', '2026-01-01', 'deposit', 'USD', '1')"
            )
    finally:
        conn.close()


# --------------------------------------------------------------------------- mutation cases


def test_mutation_in_cash_amount_is_detected(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    _build_legacy_db(db)
    pre = _snapshot(db)

    # a clean migrated copy reconciles (sanity: the harness passes on the untampered merge)
    good = tmp_path / "good.db"
    shutil.copy2(db, good)
    _migrate(good)
    assert mr.diff_snapshots(pre, _snapshot(good)) == []

    # tamper one merged-account cash amount on a second migrated copy
    bad = tmp_path / "bad.db"
    shutil.copy2(db, bad)
    _migrate(bad)
    _tamper(
        bad,
        "UPDATE cash_movements SET amount = '999999' "
        "WHERE id = (SELECT MIN(id) FROM cash_movements "
        "WHERE account_id = 'moomoo_my' AND ccy = 'USD')",
    )

    mismatches = mr.diff_snapshots(pre, _snapshot(bad))
    paths = [m.path for m in mismatches]
    assert any(
        p.startswith("raw.cash_by_account_ccy.moomoo_my") for p in paths
    ), paths


def test_mutation_in_transaction_price_is_detected(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    _build_legacy_db(db)
    pre = _snapshot(db)

    bad = tmp_path / "bad.db"
    shutil.copy2(db, bad)
    _migrate(bad)
    _tamper(
        bad,
        "UPDATE transactions SET price = '99999' "
        "WHERE id = (SELECT MIN(id) FROM transactions "
        "WHERE account_id = 'moomoo_my' AND symbol = 'AAPL')",
    )

    mismatches = mr.diff_snapshots(pre, _snapshot(bad))
    paths = [m.path for m in mismatches]
    # the tampered buy price flows into the AAPL holding cost basis (money-of-record)
    assert any(p.startswith("engine.holdings.moomoo_my.AAPL") for p in paths), paths
