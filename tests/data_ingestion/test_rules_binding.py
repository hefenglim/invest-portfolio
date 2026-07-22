"""Tests for the (account, market) rule binding foundation (Batch B, Wave 1).

Covers the binding table + seed (T1), the resolvers + Account carriage (T2), and the
INERT property: every path falls back to the accounts scalars, so existing accounts are
unchanged.
"""

import sqlite3

import pytest

from portfolio_dash.data_ingestion.config_seed import DEFAULT_ACCOUNTS, seed_accounts
from portfolio_dash.data_ingestion.markets import market_for_settlement_ccy
from portfolio_dash.data_ingestion.rules_binding import (
    allowed_markets,
    dividend_model_for,
    fee_rule_for,
    rule_sets_for,
)
from portfolio_dash.data_ingestion.schema import create_tables
from portfolio_dash.data_ingestion.store import list_accounts
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import MarketRule


def _insert_account(
    conn: sqlite3.Connection,
    account_id: str,
    settlement_ccy: str,
    fee_rule_set: str,
    dividend_model: str,
) -> None:
    conn.execute(
        "INSERT INTO accounts (account_id, name, broker, settlement_ccy, funding_ccy, "
        "fee_rule_set, dividend_model) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (account_id, account_id, account_id, settlement_ccy, settlement_ccy,
         fee_rule_set, dividend_model),
    )


def _bind(
    conn: sqlite3.Connection,
    account_id: str,
    market: str,
    fee_rule_set: str,
    dividend_model: str,
) -> None:
    conn.execute(
        "INSERT INTO account_market_rules (account_id, market, fee_rule_set, "
        "dividend_model) VALUES (?, ?, ?, ?)",
        (account_id, market, fee_rule_set, dividend_model),
    )


# --- T1: table + seed -------------------------------------------------------


def test_table_created_idempotently() -> None:
    c = sqlite3.connect(":memory:")
    create_tables(c)
    create_tables(c)  # must not error
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "account_market_rules" in names
    cols = {r[1] for r in c.execute("PRAGMA table_info(account_market_rules)")}
    assert cols == {"account_id", "market", "fee_rule_set", "dividend_model"}


def test_seed_writes_exactly_four_binding_rows(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    rows = conn.execute(
        "SELECT account_id, market, fee_rule_set, dividend_model "
        "FROM account_market_rules"
    ).fetchall()
    # tw_broker(TW) + schwab(US) + the merged moomoo_my's TWO bindings (US, MY) = 4.
    assert len(rows) == 4
    got = {
        (r["account_id"], r["market"]): (r["fee_rule_set"], r["dividend_model"]) for r in rows
    }
    # Single-market accounts mirror their scalars for the settlement-derived market; the
    # merged dual-market account (moomoo_my) carries its explicit per-market bindings.
    for a in DEFAULT_ACCOUNTS:
        if a.market_bindings:
            for b in a.market_bindings:
                assert got[(a.account_id, b.market.value)] == (b.fee_rule_set, b.dividend_model)
            continue
        market = market_for_settlement_ccy(a.settlement_ccy.value)
        assert market is not None
        assert got[(a.account_id, market.value)] == (a.fee_rule_set, a.dividend_model)
    # Concrete mapping pin.
    assert got[("tw_broker", "TW")] == ("tw", "cash_cost_reduction")
    assert got[("schwab", "US")] == ("schwab", "drip_us")
    assert got[("moomoo_my", "US")] == ("moomoo_us", "drip_us")
    assert got[("moomoo_my", "MY")] == ("moomoo_my", "cash")


def test_reseed_is_a_noop(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    before = conn.execute(
        "SELECT account_id, market, fee_rule_set, dividend_model "
        "FROM account_market_rules ORDER BY account_id, market"
    ).fetchall()
    seed_accounts(conn)  # idempotent
    after = conn.execute(
        "SELECT account_id, market, fee_rule_set, dividend_model "
        "FROM account_market_rules ORDER BY account_id, market"
    ).fetchall()
    assert [tuple(r) for r in before] == [tuple(r) for r in after]
    assert len(after) == 4  # no duplicates


# --- T2: resolvers ----------------------------------------------------------


def test_fee_rule_for_binding_hit(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    assert fee_rule_for(conn, "schwab", Market.US) == "schwab"
    # The merged account resolves per market: US -> moomoo_us, MY -> moomoo_my.
    assert fee_rule_for(conn, "moomoo_my", Market.US) == "moomoo_us"
    assert fee_rule_for(conn, "moomoo_my", Market.MY) == "moomoo_my"


def test_fee_rule_for_falls_back_to_scalar_when_binding_absent(
    conn: sqlite3.Connection,
) -> None:
    seed_accounts(conn)
    conn.execute("DELETE FROM account_market_rules WHERE account_id = 'schwab'")
    # No binding row -> falls back to accounts.fee_rule_set (unchanged scalar).
    assert fee_rule_for(conn, "schwab", Market.US) == "schwab"


def test_dividend_model_for_hit_and_fallback(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    assert dividend_model_for(conn, "schwab", Market.US) == "drip_us"
    # Merged account resolves per market: US -> drip_us (scalar too), MY -> cash (binding).
    assert dividend_model_for(conn, "moomoo_my", Market.US) == "drip_us"
    assert dividend_model_for(conn, "moomoo_my", Market.MY) == "cash"
    # Drop the MY binding -> falls back to the account scalar (drip_us).
    conn.execute(
        "DELETE FROM account_market_rules WHERE account_id = 'moomoo_my' AND market = 'MY'")
    assert dividend_model_for(conn, "moomoo_my", Market.MY) == "drip_us"  # scalar fallback


def test_unknown_account_raises_keyerror(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    with pytest.raises(KeyError):
        fee_rule_for(conn, "does_not_exist", Market.US)
    with pytest.raises(KeyError):
        dividend_model_for(conn, "does_not_exist", Market.US)
    with pytest.raises(KeyError):
        allowed_markets(conn, "does_not_exist")
    with pytest.raises(KeyError):
        rule_sets_for(conn, "does_not_exist")


def test_allowed_markets_from_bindings(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    assert allowed_markets(conn, "schwab") == frozenset({Market.US})
    assert allowed_markets(conn, "tw_broker") == frozenset({Market.TW})


def test_allowed_markets_fallback_singleton_from_settlement_ccy(
    conn: sqlite3.Connection,
) -> None:
    seed_accounts(conn)
    conn.execute("DELETE FROM account_market_rules WHERE account_id = 'schwab'")
    # Empty bindings -> settlement-ccy (USD) derived singleton {US}.
    assert allowed_markets(conn, "schwab") == frozenset({Market.US})


def test_allowed_markets_multi_market(conn: sqlite3.Connection) -> None:
    # Simulate the future merged dual-market account (not created here).
    _insert_account(conn, "merged", "USD", "moomoo_us", "drip_us")
    _bind(conn, "merged", "US", "moomoo_us", "drip_us")
    _bind(conn, "merged", "MY", "moomoo_my", "cash")
    assert allowed_markets(conn, "merged") == frozenset({Market.US, Market.MY})


def test_rule_sets_for_dedupe_and_order(conn: sqlite3.Connection) -> None:
    # Two markets sharing ONE rule set -> DISTINCT collapses to a single entry.
    _insert_account(conn, "shared", "USD", "moomoo_us", "drip_us")
    _bind(conn, "shared", "US", "moomoo_us", "drip_us")
    _bind(conn, "shared", "MY", "moomoo_us", "cash")
    assert rule_sets_for(conn, "shared") == ["moomoo_us"]
    # Two distinct rule sets -> sorted, stable order.
    _insert_account(conn, "dual", "USD", "moomoo_us", "drip_us")
    _bind(conn, "dual", "US", "moomoo_us", "drip_us")
    _bind(conn, "dual", "MY", "moomoo_my", "cash")
    assert rule_sets_for(conn, "dual") == ["moomoo_my", "moomoo_us"]


def test_rule_sets_for_fallback(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    conn.execute("DELETE FROM account_market_rules WHERE account_id = 'schwab'")
    assert rule_sets_for(conn, "schwab") == ["schwab"]  # scalar fallback


# --- T2: Account carriage ---------------------------------------------------


def test_list_accounts_populates_market_rules(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    by_id = {a.account_id: a for a in list_accounts(conn)}
    for cfg in DEFAULT_ACCOUNTS:
        acct = by_id[cfg.account_id]
        if cfg.market_bindings:  # merged account: one MarketRule per explicit binding
            assert acct.market_rules == {
                b.market.value: MarketRule(
                    fee_rule_set=b.fee_rule_set, dividend_model=b.dividend_model
                )
                for b in cfg.market_bindings
            }
            continue
        market = market_for_settlement_ccy(cfg.settlement_ccy.value)
        assert market is not None
        assert acct.market_rules == {
            market.value: MarketRule(
                fee_rule_set=cfg.fee_rule_set, dividend_model=cfg.dividend_model
            )
        }


def test_list_accounts_market_rules_empty_without_bindings(
    conn: sqlite3.Connection,
) -> None:
    # accounts seeded, then bindings wiped -> market_rules degrades to {} (inert).
    seed_accounts(conn)
    conn.execute("DELETE FROM account_market_rules")
    for acct in list_accounts(conn):
        assert acct.market_rules == {}


def test_account_model_round_trips_market_rules() -> None:
    from portfolio_dash.shared.models.assets import Account

    a = Account(
        account_id="x", name="X", broker="X",
        settlement_ccy=Currency.USD, funding_ccy=Currency.USD,
        dividend_model="drip_us",
        market_rules={"US": MarketRule(fee_rule_set="schwab", dividend_model="drip_us")},
    )
    assert Account.model_validate(a.model_dump()) == a
