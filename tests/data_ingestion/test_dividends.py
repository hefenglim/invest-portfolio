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
    # A registered US symbol on schwab (drip_us) booked with MY's single-tier NET type is a
    # type/market mismatch -> soft needs_confirm (importable only after explicit confirm),
    # never a hard block. This is the merged-account corruption guard (MY cash-as-DRIP etc.).
    #
    # The example used to be CASH here. P1b (2026-08-13) ADMITTED CASH on drip_us — a US
    # payout that is not reinvested is ordinary, not a corruption — so the guard is now
    # demonstrated with a type that genuinely does not belong to this market's model.
    seed_accounts(conn)
    _register(conn, "AAPL", Market.US, Currency.USD)
    csv = "account,symbol,date,type,gross\nschwab,AAPL,2026-06-01,NET,100\n"
    p = build_dividend_preview(conn, csv)
    row = p.rows[0]
    mism = [i for i in row.issues if i.kind == "dividend_type_mismatch"]
    assert len(mism) == 1
    assert mism[0].needs_confirm is True
    assert mism[0].message == "股利類型與該市場模型不符，請確認"
    assert not row.has_hard_issue  # soft -> importable after confirm


# --- P1b (2026-08-13): a US payout that arrives as plain CASH --------------------------


def test_us_cash_dividend_is_not_a_mismatch(conn: sqlite3.Connection) -> None:
    """The friction P1b removes: a US cash dividend used to need a per-row confirmation."""
    seed_accounts(conn)
    _register(conn, "AAPL", Market.US, Currency.USD)
    csv = ("account,symbol,date,type,gross,withholding\n"
           "schwab,AAPL,2026-06-01,CASH,100,30\n")
    p = build_dividend_preview(conn, csv)
    assert [i.kind for i in p.rows[0].issues] == []
    assert p.rows[0].payload["type"] == "CASH"
    assert p.rows[0].payload["withholding"] == "30"
    assert p.rows[0].payload["net"] == "70"


def test_us_cash_dividend_without_withholding_asks(conn: sqlite3.Connection) -> None:
    """``apply_dividend_model`` keys on the dividend TYPE, not on the account's model, so a
    blank withholding on a CASH row books 0 — right for a TW/MY payout, wrong for a US one
    under W-8BEN. The consequence is not cosmetic: net would equal gross, over-reducing
    ``adjusted_total`` and under-reporting the position's unrealized gain for its whole life.

    Soft, not hard: a withholding-free US distribution genuinely exists (return of capital).
    """
    seed_accounts(conn)
    _register(conn, "AAPL", Market.US, Currency.USD)
    csv = "account,symbol,date,type,gross\nschwab,AAPL,2026-06-01,CASH,100\n"
    p = build_dividend_preview(conn, csv)
    row = p.rows[0]
    ask = [i for i in row.issues if i.kind == "us_cash_dividend_no_withholding"]
    assert len(ask) == 1
    assert ask[0].needs_confirm is True
    assert not row.has_hard_issue
    # The row still books what it says it books — the warning does not silently alter it.
    assert row.payload["withholding"] == "0"
    assert row.payload["net"] == "100"


def test_tw_cash_dividend_is_not_asked_about_withholding(
    conn: sqlite3.Connection,
) -> None:
    """The warning is bound to the drip_us model, not to the CASH type: a TW cash dividend
    legitimately has no withholding and must stay silent."""
    seed_accounts(conn)
    _register(conn, "2330", Market.TW, Currency.TWD)
    csv = "account,symbol,date,type,gross\ntw_broker,2330,2026-06-01,CASH,5000\n"
    p = build_dividend_preview(conn, csv)
    assert [i.kind for i in p.rows[0].issues] == []


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
