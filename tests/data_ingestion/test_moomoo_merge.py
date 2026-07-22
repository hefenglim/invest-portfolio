"""Unit tests for the one-time Moomoo account merge (``data_ingestion.moomoo_merge``).

Covers the full happy path (every V self-check), idempotency, the S0 partial-release guards,
the opening_inventory collision abort with a full rollback, and a mid-migration crash rollback
— both proving the DB is byte-identical (table-dump equal) after the aborted transaction.
"""

import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion import moomoo_merge
from portfolio_dash.data_ingestion.config_seed import AccountConfig
from portfolio_dash.data_ingestion.moomoo_merge import (
    migrate_moomoo_accounts,
    needs_moomoo_merge,
)
from portfolio_dash.data_ingestion.store import (
    insert_cash_movement,
    insert_dividend,
    insert_fx_conversion,
    insert_transaction,
    upsert_instrument,
    upsert_opening,
)
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side

_LEGACY = ("moomoo_my_us", "moomoo_my_my")
_BUILD = date(2026, 1, 2)


def _new_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _seed_legacy_accounts(conn: sqlite3.Connection) -> None:
    """The pre-merge account shape: the two legacy Moomoo accounts + a bystander (schwab)."""
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


def _seed_side_tables(conn: sqlite3.Connection) -> None:
    """The two lazily-created / TEXT-embedded tables the merge also fixes up."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS data_source_fallbacks "
        "(account_id TEXT PRIMARY KEY, chain TEXT NOT NULL)"
    )
    conn.executemany(
        "INSERT INTO data_source_fallbacks (account_id, chain) VALUES (?, ?)",
        [("moomoo_my_us", "a>b"), ("moomoo_my_my", "c>d"), ("schwab", "e>f")],
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS pending_dividend_skips "
        "(fingerprint TEXT PRIMARY KEY, skipped_at TEXT NOT NULL)"
    )
    conn.executemany(
        "INSERT INTO pending_dividend_skips (fingerprint, skipped_at) VALUES (?, ?)",
        [
            ("div:moomoo_my_us:AAPL:2026-03-01", "2026-03-02T00:00:00"),
            ("div:moomoo_my_us:2330:2026-03-01:stock", "2026-03-02T00:00:00"),  # :stock suffix
            ("div:moomoo_my_my:1155:2026-04-05", "2026-04-06T00:00:00"),
            # Same detection skipped under BOTH legacy accounts -> collapses to one merged row.
            ("div:moomoo_my_us:AAPL:2026-06-01", "2026-06-02T00:00:00"),
            ("div:moomoo_my_my:AAPL:2026-06-01", "2026-06-02T00:00:00"),
            ("div:schwab:AAPL:2026-03-01", "2026-03-02T00:00:00"),  # bystander: untouched
        ],
    )


def _seed_legacy_db(conn: sqlite3.Connection) -> None:
    """A populated pre-merge DB: rows in all six account-scoped ledgers + side tables."""
    bootstrap_db(conn)
    _seed_legacy_accounts(conn)
    _seed_side_tables(conn)
    for sym, mkt, ccy in (
        ("AAPL", Market.US, Currency.USD),
        ("NVDA", Market.US, Currency.USD),
        ("1155", Market.MY, Currency.MYR),
    ):
        upsert_instrument(conn, Instrument(symbol=sym, market=mkt, quote_ccy=ccy, name=sym,
                                           sector="Information Technology"))
    # transactions (US under moomoo_my_us, MY under moomoo_my_my)
    insert_transaction(conn, account_id="moomoo_my_us", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("10"), price=Decimal("100"), fees=Decimal("1"),
                       tax=Decimal("0"), trade_date=date(2026, 1, 10))
    insert_transaction(conn, account_id="moomoo_my_us", symbol="NVDA", side=Side.BUY,
                       quantity=Decimal("5"), price=Decimal("200"), fees=Decimal("2"),
                       tax=Decimal("0"), trade_date=date(2026, 1, 11))
    insert_transaction(conn, account_id="moomoo_my_my", symbol="1155", side=Side.BUY,
                       quantity=Decimal("100"), price=Decimal("9"), fees=Decimal("3"),
                       tax=Decimal("1"), trade_date=date(2026, 1, 12))
    # dividends
    insert_dividend(conn, account_id="moomoo_my_us", symbol="AAPL", div_date=date(2026, 3, 1),
                    div_type="DRIP", gross=Decimal("30"), withholding=Decimal("9"),
                    net=Decimal("21"))
    insert_dividend(conn, account_id="moomoo_my_my", symbol="1155", div_date=date(2026, 4, 5),
                    div_type="NET", gross=Decimal("50"), withholding=Decimal("0"),
                    net=Decimal("50"))
    # fx conversion (Moomoo funds USD from MYR)
    insert_fx_conversion(conn, account_id="moomoo_my_us", date=date(2026, 1, 8),
                         from_ccy=Currency.MYR, from_amount=Decimal("4400"),
                         to_ccy=Currency.USD, to_amount=Decimal("1000"))
    # cash movements
    insert_cash_movement(conn, account_id="moomoo_my_us", move_date=date(2026, 1, 5),
                         kind="deposit", ccy=Currency.USD, amount=Decimal("1000"))
    insert_cash_movement(conn, account_id="moomoo_my_my", move_date=date(2026, 1, 5),
                         kind="deposit", ccy=Currency.MYR, amount=Decimal("5000"))
    # opening inventory (DISTINCT symbols per account -> no PK collision)
    upsert_opening(conn, account_id="moomoo_my_us", symbol="AAPL", shares=Decimal("5"),
                   original_cost_total=Decimal("500"), build_date=_BUILD)
    upsert_opening(conn, account_id="moomoo_my_my", symbol="1155", shares=Decimal("200"),
                   original_cost_total=Decimal("1800"), build_date=_BUILD)
    # ledger_audit: an immutable history row whose before_json embeds a legacy id — it must
    # survive the merge untouched (V.a EXEMPTs ledger_audit).
    conn.execute(
        "INSERT INTO ledger_audit (table_name, row_id, action, before_json, at) "
        "VALUES (?,?,?,?,?)",
        ("transactions", "1", "update",
         '{"account_id": "moomoo_my_us", "symbol": "AAPL"}', "2026-01-01T00:00:00"),
    )
    conn.commit()


def _dump(conn: sqlite3.Connection) -> list[str]:
    """Logical table dump for a byte-identical (rollback) comparison."""
    return list(conn.iterdump())


def _count_where_account(conn: sqlite3.Connection, table: str, ids: tuple[str, ...]) -> int:
    ph = ",".join("?" * len(ids))
    return int(conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE account_id IN ({ph})", ids).fetchone()[0])


# --------------------------------------------------------------------------- happy path


def test_full_migration_relabels_every_ledger() -> None:
    conn = _new_conn()
    _seed_legacy_db(conn)
    assert needs_moomoo_merge(conn) is True

    # V.b baseline: per-currency cash-pool totals BEFORE (over the two legacy accounts).
    pre_sums = moomoo_merge._cash_pool_sums(conn, _LEGACY)

    assert migrate_moomoo_accounts(conn) is True

    # V.a — no legacy id survives in any account-scoped table.
    for table in (
        "transactions", "dividends", "fx_conversions", "cash_movements",
        "opening_inventory", "accounts", "account_market_rules", "data_source_fallbacks",
    ):
        assert _count_where_account(conn, table, _LEGACY) == 0, table
    # every relabelled row now lives on moomoo_my
    assert _count_where_account(conn, "transactions", ("moomoo_my",)) == 3
    assert _count_where_account(conn, "dividends", ("moomoo_my",)) == 2
    assert _count_where_account(conn, "fx_conversions", ("moomoo_my",)) == 1
    assert _count_where_account(conn, "cash_movements", ("moomoo_my",)) == 2
    assert _count_where_account(conn, "opening_inventory", ("moomoo_my",)) == 2
    # bystander schwab untouched
    assert _count_where_account(conn, "data_source_fallbacks", ("schwab",)) == 1

    # V.a — pending_dividend_skips fingerprints rewritten (legacy prefixes gone).
    fps = {r[0] for r in conn.execute("SELECT fingerprint FROM pending_dividend_skips")}
    assert fps == {
        "div:moomoo_my:AAPL:2026-03-01",
        "div:moomoo_my:2330:2026-03-01:stock",   # :stock suffix preserved
        "div:moomoo_my:1155:2026-04-05",
        "div:moomoo_my:AAPL:2026-06-01",          # the two legacy rows collapsed to one
        "div:schwab:AAPL:2026-03-01",             # bystander untouched
    }

    # V.b — per-currency cash-pool totals conserved after the relabel.
    post_sums = moomoo_merge._cash_pool_sums(conn, ("moomoo_my",))
    assert post_sums == pre_sums

    # V.c — merged account row + its two market bindings.
    acct = conn.execute(
        "SELECT settlement_ccy, funding_ccy, fee_rule_set, dividend_model "
        "FROM accounts WHERE account_id='moomoo_my'").fetchone()
    assert (acct["settlement_ccy"], acct["funding_ccy"]) == ("USD", "MYR")
    assert (acct["fee_rule_set"], acct["dividend_model"]) == ("moomoo_us", "drip_us")
    bindings = {
        r["market"]: (r["fee_rule_set"], r["dividend_model"])
        for r in conn.execute(
            "SELECT market, fee_rule_set, dividend_model FROM account_market_rules "
            "WHERE account_id='moomoo_my'")
    }
    assert bindings == {"US": ("moomoo_us", "drip_us"), "MY": ("moomoo_my", "cash")}

    # ledger_audit history is EXEMPT: the legacy-id-bearing before_json row survives verbatim.
    audit = conn.execute("SELECT before_json FROM ledger_audit").fetchall()
    assert len(audit) == 1 and "moomoo_my_us" in audit[0]["before_json"]


def test_second_call_is_a_noop() -> None:
    conn = _new_conn()
    _seed_legacy_db(conn)
    assert migrate_moomoo_accounts(conn) is True
    dump_after_first = _dump(conn)
    assert needs_moomoo_merge(conn) is False
    assert migrate_moomoo_accounts(conn) is False  # idempotent no-op
    assert _dump(conn) == dump_after_first


def test_needs_predicate_false_without_both_legacy_ids() -> None:
    conn = _new_conn()
    bootstrap_db(conn)
    # only ONE legacy id present -> not a merge candidate
    conn.execute(
        "INSERT INTO accounts (account_id, name, broker, settlement_ccy, funding_ccy, "
        "fee_rule_set, dividend_model) VALUES "
        "('moomoo_my_us','x','Moomoo MY','USD','MYR','moomoo_us','drip_us')")
    conn.commit()
    assert needs_moomoo_merge(conn) is False
    assert migrate_moomoo_accounts(conn) is False


# --------------------------------------------------------------- S0 partial-release guards


def test_partial_release_config_keeps_legacy_id_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _new_conn()
    _seed_legacy_db(conn)
    bad = [
        AccountConfig(account_id="moomoo_my_us", name="x", broker="b",
                      settlement_ccy=Currency.USD, funding_ccy=Currency.MYR,
                      fee_rule_set="moomoo_us", dividend_model="drip_us"),
        AccountConfig(account_id="moomoo_my", name="Moomoo MY", broker="Moomoo MY",
                      settlement_ccy=Currency.USD, funding_ccy=Currency.MYR,
                      fee_rule_set="moomoo_us", dividend_model="drip_us"),
    ]
    monkeypatch.setattr(moomoo_merge, "DEFAULT_ACCOUNTS", bad)
    with pytest.raises(RuntimeError, match="legacy id"):
        migrate_moomoo_accounts(conn)


def test_partial_release_config_missing_merged_id_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _new_conn()
    _seed_legacy_db(conn)
    bad = [
        AccountConfig(account_id="schwab", name="x", broker="b",
                      settlement_ccy=Currency.USD, funding_ccy=Currency.TWD,
                      fee_rule_set="schwab", dividend_model="drip_us"),
    ]
    monkeypatch.setattr(moomoo_merge, "DEFAULT_ACCOUNTS", bad)
    with pytest.raises(RuntimeError, match="lacks 'moomoo_my'"):
        migrate_moomoo_accounts(conn)


def test_partial_release_missing_binding_table_raises() -> None:
    conn = _new_conn()
    _seed_legacy_db(conn)
    conn.execute("DROP TABLE account_market_rules")
    conn.commit()
    with pytest.raises(RuntimeError, match="account_market_rules table missing"):
        migrate_moomoo_accounts(conn)


# ------------------------------------------------------------- collision + crash rollback


def test_opening_inventory_collision_aborts_and_rolls_back() -> None:
    conn = _new_conn()
    _seed_legacy_db(conn)
    # Same symbol under BOTH legacy accounts -> would collide on the (account_id, symbol) PK.
    upsert_opening(conn, account_id="moomoo_my_my", symbol="AAPL", shares=Decimal("7"),
                   original_cost_total=Decimal("700"), build_date=_BUILD)
    before = _dump(conn)
    with pytest.raises(RuntimeError, match="collision"):
        migrate_moomoo_accounts(conn)
    # full rollback -> DB byte-identical, legacy ids intact.
    assert _dump(conn) == before
    assert needs_moomoo_merge(conn) is True


def test_mid_migration_crash_rolls_back_then_next_call_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _new_conn()
    _seed_legacy_db(conn)
    before = _dump(conn)

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("boom: injected mid-migration failure")

    # _self_check runs AFTER every U-step -> a raise here proves the UPDATEs roll back.
    monkeypatch.setattr(moomoo_merge, "_self_check", _boom)
    with pytest.raises(RuntimeError, match="boom"):
        migrate_moomoo_accounts(conn)
    assert _dump(conn) == before  # every relabel rolled back

    # Un-patch: the next boot re-runs the migration cleanly.
    monkeypatch.undo()
    assert migrate_moomoo_accounts(conn) is True
    assert needs_moomoo_merge(conn) is False
    assert _count_where_account(conn, "transactions", ("moomoo_my",)) == 3
