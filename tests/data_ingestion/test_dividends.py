import sqlite3
from decimal import Decimal

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.dividend_import import (
    build_dividend_preview,
    write_dividend_row,
)
from portfolio_dash.data_ingestion.dividend_model import apply_dividend_model
from portfolio_dash.data_ingestion.preview import commit_preview
from portfolio_dash.data_ingestion.store import list_dividends, upsert_instrument
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument


def test_drip_model_30pct_withholding_and_reinvest() -> None:
    r = apply_dividend_model("DRIP", gross=Decimal("100"), reinvest_price=Decimal("20"))
    assert r.withholding == Decimal("30") and r.net == Decimal("70")
    assert r.reinvest_shares == Decimal("3.5")  # 70 / 20


def test_cash_model_net_equals_gross() -> None:
    r = apply_dividend_model("cash", gross=Decimal("50"))
    assert r.withholding == Decimal("0") and r.net == Decimal("50")
    assert r.reinvest_shares is None


def test_csv_preview_and_commit(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    csv = ("account,symbol,date,type,gross,reinvest_price\n"
           "schwab,AAPL,2026-05-01,DRIP,100,20\n"
           "tw_broker,2330,2026-06-01,cash,50,\n")
    p = build_dividend_preview(conn, csv)
    assert len(p.rows) == 2 and all(not r.has_hard_issue for r in p.rows)
    summary = commit_preview(conn, p, accept={0, 1}, writer=write_dividend_row)
    assert len(summary.written) == 2
    drip = [d for d in list_dividends(conn, account_id="schwab")][0]
    assert drip.withholding == Decimal("30") and drip.net == Decimal("70")
    assert drip.reinvest_shares == Decimal("3.5")


def test_csv_unknown_account_hard_blocks(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    csv = "account,symbol,date,type,gross\nnope,X,2026-06-01,cash,10\n"
    p = build_dividend_preview(conn, csv)
    assert p.rows[0].has_hard_issue


def test_csv_type_normalized_to_upper(conn: sqlite3.Connection) -> None:
    """Regression (2026-07-03): a lowercase type ("cash") used to be stored RAW,
    poisoning the ledger (readers do DividendType(s.type) and raise). The importer
    now normalizes to upper and hard-rejects unknown types."""
    seed_accounts(conn)
    csv = ("account,symbol,date,type,gross\n"
           "tw_broker,2330,2026-06-01,cash,50\n")
    p = build_dividend_preview(conn, csv)
    commit_preview(conn, p, accept={0}, writer=write_dividend_row)
    stored = list_dividends(conn, account_id="tw_broker")[-1]
    assert stored.type == "CASH"


def test_csv_unknown_type_hard_blocks(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    csv = ("account,symbol,date,type,gross\n"
           "tw_broker,2330,2026-06-01,bogus,50\n")
    p = build_dividend_preview(conn, csv)
    assert p.rows[0].has_hard_issue
    assert any(i.kind == "parse_error" for i in p.rows[0].issues)


# --- Batch B (F01): dividend type/market coherence -------------------------------------


def _register(conn: sqlite3.Connection, symbol: str, market: Market, ccy: Currency) -> None:
    upsert_instrument(conn, Instrument(symbol=symbol, market=market, quote_ccy=ccy,
                                       sector="Tech", name=symbol))


def test_csv_dividend_type_market_mismatch_needs_confirm(conn: sqlite3.Connection) -> None:
    # A registered US symbol on schwab (DRIP model) booked as a CASH dividend is a
    # type/market mismatch -> soft needs_confirm (importable only after explicit confirm),
    # never a hard block. This is the merged-account corruption guard (MY cash-as-DRIP etc.).
    seed_accounts(conn)
    _register(conn, "AAPL", Market.US, Currency.USD)
    csv = "account,symbol,date,type,gross\nschwab,AAPL,2026-06-01,CASH,100\n"
    p = build_dividend_preview(conn, csv)
    row = p.rows[0]
    mism = [i for i in row.issues if i.kind == "dividend_type_mismatch"]
    assert len(mism) == 1
    assert mism[0].needs_confirm is True
    assert mism[0].message == "股利類型與該市場模型不符，請確認"
    assert not row.has_hard_issue  # soft -> importable after confirm


def test_csv_dividend_type_market_coherent_has_no_mismatch(
    conn: sqlite3.Connection,
) -> None:
    # A coherent row (DRIP on schwab US) carries NO mismatch issue — dormant for the
    # correct case. Also proves a single-market account's normal rows are unaffected.
    seed_accounts(conn)
    _register(conn, "AAPL", Market.US, Currency.USD)
    csv = ("account,symbol,date,type,gross,reinvest_price\n"
           "schwab,AAPL,2026-06-01,DRIP,100,20\n")
    p = build_dividend_preview(conn, csv)
    assert not any(i.kind == "dividend_type_mismatch" for i in p.rows[0].issues)


def test_csv_dividend_unregistered_symbol_skips_coherence(
    conn: sqlite3.Connection,
) -> None:
    # An UNREGISTERED symbol keeps its existing soft unresolved handling and is NOT
    # coherence-checked (its market is unknown until registered) — no mismatch issue.
    seed_accounts(conn)
    csv = "account,symbol,date,type,gross\nschwab,NOPE,2026-06-01,CASH,100\n"
    p = build_dividend_preview(conn, csv)
    kinds = {i.kind for i in p.rows[0].issues}
    assert "symbol_unresolved" in kinds
    assert "dividend_type_mismatch" not in kinds
