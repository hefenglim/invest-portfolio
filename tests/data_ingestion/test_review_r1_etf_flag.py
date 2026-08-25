"""R1/① counter-evidence: the value that SEEDS the registry's ETF flag (AI-D40).

The 2026-07-15 stress audit made a registered instrument's ``is_etf`` win over the input
flag at both fee seams, and both seams carry a comment saying so.  Nothing guarded the
value that put the flag into the registry in the first place: ``quick_register`` defaulted
``is_etf=False`` and the manual door's auto-registration did not pass the argument at all,
so a TW ETF entered by trading it was permanently recorded as *not* an ETF — 現股 0.3%
instead of ETF 0.1%, three times the tax, with ``tax_rate: 0.003`` written into
``fee_rule_snapshot`` as though it were established.

The fix is the third state, not a better guess: ``etf_flag_unknown``.  The engine still has
to produce a number, so it computes with False — but it says so, on the trade where the
choice actually moves money.
"""

import sqlite3
from datetime import date
from decimal import Decimal

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import get_fee_rule_set, seed_accounts
from portfolio_dash.data_ingestion.csv_import import build_transaction_preview
from portfolio_dash.data_ingestion.fees import etf_flag_issue_applies, resolve_etf_flag
from portfolio_dash.data_ingestion.manual import enter_transaction
from portfolio_dash.data_ingestion.store import get_instrument, upsert_instrument
from portfolio_dash.data_ingestion.validate import TxnInput
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side

D = Decimal


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    bootstrap_db(conn)
    seed_accounts(conn)
    return conn


def _register(conn: sqlite3.Connection, *, unknown: bool, is_etf: bool = False) -> None:
    upsert_instrument(conn, Instrument(
        symbol="0050", market=Market.TW, quote_ccy=Currency.TWD, sector="ETF",
        name="元大台灣50", board="TWSE", is_etf=is_etf, etf_flag_unknown=unknown))


def _sell(conn: sqlite3.Connection) -> tuple[Decimal, list[str]]:
    draft = enter_transaction(
        conn,
        TxnInput(account_id="tw_broker", symbol="0050", side=Side.SELL,
                 quantity=D("1000"), price=D("110"), trade_date=date(2026, 5, 20)),
        confirm=False, today=date(2026, 5, 20))
    return draft.tax, [i.kind for i in draft.issues]


# --- the third state round-trips through the registry ------------------------------------

def test_unknown_flag_persists_so_the_next_trade_still_knows_nobody_answered() -> None:
    conn = _conn()
    _register(conn, unknown=True)
    saved = get_instrument(conn, "0050")
    assert saved is not None
    assert saved.is_etf is False and saved.etf_flag_unknown is True
    conn.close()


def test_existing_rows_migrate_in_as_KNOWN_and_are_never_relabelled() -> None:
    """The owner ruling verbatim: 既有 0/1 列一律不動 — only FUTURE auto-registrations
    land unknown; nothing reaches back and re-labels what is already in the ledger."""
    conn = _conn()
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW,
                                       quote_ccy=Currency.TWD, sector="Tech", name="TSMC"))
    saved = get_instrument(conn, "2330")
    assert saved is not None and saved.etf_flag_unknown is False
    conn.close()


# --- the fee seams disclose rather than assume -------------------------------------------

def test_tw_sell_on_an_unanswered_flag_raises_the_issue_and_marks_the_snapshot() -> None:
    conn = _conn()
    _register(conn, unknown=True)
    tax, kinds = _sell(conn)
    # It still computes — a number has to come out — but at the 現股 rate it must DISCLOSE.
    assert tax == D("110000") * D("0.003")
    assert "etf_flag_unknown" in kinds, kinds
    conn.close()


def test_an_answered_ETF_flag_is_silent_and_taxed_at_the_ETF_rate() -> None:
    conn = _conn()
    _register(conn, unknown=False, is_etf=True)
    tax, kinds = _sell(conn)
    assert tax == D("110000") * D("0.001")
    assert "etf_flag_unknown" not in kinds
    conn.close()


def test_an_answered_NON_etf_flag_is_equally_silent() -> None:
    """False-because-answered must not be confused with False-because-unasked."""
    conn = _conn()
    _register(conn, unknown=False, is_etf=False)
    tax, kinds = _sell(conn)
    assert tax == D("110000") * D("0.003")
    assert "etf_flag_unknown" not in kinds
    conn.close()


def test_a_BUY_never_raises_it_because_no_tw_tax_depends_on_the_answer() -> None:
    conn = _conn()
    _register(conn, unknown=True)
    draft = enter_transaction(
        conn,
        TxnInput(account_id="tw_broker", symbol="0050", side=Side.BUY,
                 quantity=D("1000"), price=D("110"), trade_date=date(2026, 5, 20)),
        confirm=False, today=date(2026, 5, 20))
    assert "etf_flag_unknown" not in [i.kind for i in draft.issues]
    conn.close()


def test_the_bulk_door_ships_the_same_guard_as_the_single_row_form() -> None:
    """csv_import must not be weaker than manual — both read fees.resolve_etf_flag."""
    conn = _conn()
    _register(conn, unknown=True)
    prev = build_transaction_preview(
        conn, "account,symbol,side,date,shares,price\ntw_broker,0050,SELL,2026-05-20,1000,110\n")
    assert "etf_flag_unknown" in [i.kind for i in prev.rows[0].issues]
    assert prev.rows[0].payload["snap.etf_flag"] == "unknown"
    conn.close()


# --- the shared resolver's own table ------------------------------------------------------

def test_an_unregistered_symbol_is_unanswered_by_construction() -> None:
    """TxnInput.is_etf defaults to False, and a default is not an answer."""
    assert resolve_etf_flag(None, False) == (False, True)


def test_the_issue_is_narrow_enough_not_to_become_noise() -> None:
    tw = get_fee_rule_set("tw", sqlite3.connect(":memory:"))
    assert etf_flag_issue_applies(tw, Side.SELL, unknown=True) is True
    assert etf_flag_issue_applies(tw, Side.BUY, unknown=True) is False
    assert etf_flag_issue_applies(tw, Side.SELL, unknown=False) is False


def test_answering_the_flag_through_the_instruments_form_clears_the_marker() -> None:
    """A warning that cannot be cleared is a warning that gets ignored.

    Driven through the real PUT endpoint (the instruments form's own door), with the socket
    ban lifted the same way the shared api_client fixture lifts it.
    """
    from fastapi.testclient import TestClient
    from pytest_socket import disable_socket, enable_socket

    from portfolio_dash.api.app import create_app
    from portfolio_dash.api.deps import get_conn

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    bootstrap_db(conn)
    create_pricing_tables(conn)   # the PUT handler reads prices for the row payload
    seed_accounts(conn)
    _register(conn, unknown=True)
    enable_socket()   # Starlette's in-process portal uses a real self-pipe on Windows
    try:
        app = create_app()
        app.dependency_overrides[get_conn] = lambda: conn
        client = TestClient(app)   # no `with`: lifespan not run (hermetic)
        r = client.put("/api/instruments/0050", json={"is_etf": True})
        assert r.status_code == 200, r.text
    finally:
        disable_socket(allow_unix_socket=True)
    saved = get_instrument(conn, "0050")
    assert saved is not None
    assert saved.is_etf is True and saved.etf_flag_unknown is False
    _, kinds = _sell(conn)
    assert "etf_flag_unknown" not in kinds
    conn.close()
