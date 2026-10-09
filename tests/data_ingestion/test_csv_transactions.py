import sqlite3
from decimal import Decimal

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.csv_import import (
    build_transaction_preview,
    write_transaction_row,
)
from portfolio_dash.data_ingestion.preview import commit_preview
from portfolio_dash.data_ingestion.store import list_transactions, upsert_instrument
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument

_CSV = (
    "account,symbol,side,date,shares,price\n"
    "tw_broker,2330,BUY,2026-06-01,1000,600\n"
    "tw_broker,2330,SELL,2026-06-02,2000,610\n"
    "nope,2330,BUY,2026-06-03,100,600\n"
)


def _setup(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    upsert_instrument(
        conn,
        Instrument(
            symbol="2330",
            market=Market.TW,
            quote_ccy=Currency.TWD,
            sector="Tech",
            name="台積電",
        ),
    )


def test_preview_builds_rows_with_autocomputed_fee_and_issues(
    conn: sqlite3.Connection,
) -> None:
    _setup(conn)
    p = build_transaction_preview(conn, _CSV)
    assert len(p.rows) == 3
    assert p.rows[0].fee == Decimal("855")  # auto-computed TW buy
    assert any(i.kind == "sell_exceeds_holdings" for i in p.rows[1].issues)  # soft
    assert any(i.kind == "unknown_account" for i in p.rows[2].issues)  # hard


def test_commit_writes_only_accepted_non_hard_rows(conn: sqlite3.Connection) -> None:
    _setup(conn)
    p = build_transaction_preview(conn, _CSV)
    summary = commit_preview(conn, p, accept={0, 1, 2}, writer=write_transaction_row)
    # row0 buy written; row1 sell soft-issue accepted -> written; row2 hard -> REJECTED
    # (its own bucket since C3: 「跳過」 means the caller deselected it, not that the
    # importer refused it, and the two need different sentences on screen).
    assert len(summary.written) == 2
    assert [r.index for r in summary.rejected] == [2] and summary.skipped == []
    assert len(list_transactions(conn, account_id="tw_broker")) == 2


def test_blank_fee_autofilled_provided_fee_kept(conn: sqlite3.Connection) -> None:
    _setup(conn)
    csv = (
        "account,symbol,side,date,shares,price,fee\n"
        "tw_broker,2330,BUY,2026-06-01,1000,600,10\n"
    )
    p = build_transaction_preview(conn, csv)
    assert p.rows[0].fee == Decimal("10")  # provided fee preserved


def test_etf_sell_tax_comes_from_registry_not_input(conn: sqlite3.Connection) -> None:
    """Stress-audit finding (2026-07-15): the registered instrument's is_etf flag must
    reach the fee engine on the CSV path — an ETF sell is taxed 0.1%, not 現股 0.3%."""
    _setup(conn)
    upsert_instrument(
        conn,
        Instrument(symbol="0050", market=Market.TW, quote_ccy=Currency.TWD,
                   sector="ETF", name="元大台灣50", is_etf=True),
    )
    csv = (
        "account,symbol,side,date,shares,price\n"
        "tw_broker,0050,SELL,2026-06-02,50,140\n"
    )
    p = build_transaction_preview(conn, csv)
    # notional 7,000 -> tax 0.001 * 7000 = 7 (the pre-fix bug charged 21 = 0.3%)
    assert p.rows[0].tax == Decimal("7")


def test_daytrade_csv_column_uses_daytrade_tax_rate(conn: sqlite3.Connection) -> None:
    _setup(conn)
    csv = (
        "account,symbol,side,date,shares,price,daytrade\n"
        "tw_broker,2330,SELL,2026-06-02,100,600,1\n"
    )
    p = build_transaction_preview(conn, csv)
    # notional 60,000 -> tax 0.0015 * 60000 = 90 (現股 would be 180)
    assert p.rows[0].tax == Decimal("90")
    assert p.rows[0].payload["daytrade"] == "1"  # persisted through the writer (MED-1)


def test_short_sale_csv_column_reaches_the_stored_row(conn: sqlite3.Connection) -> None:
    """A DECLARED short must survive the CSV → preview → writer → ledger path.

    Added 2026-08-11. The engine has carried ``short_sale`` since 2026-07-31 (owner ruling,
    spec option C) but the canonical CSV had no column for it, so **every imported declared
    short became an ordinary sell** — which the 賣超 guard flags as an undeclared oversell and
    which DISCARDS the position's cost basis, stickily. Found by §10.5's acceptance run: the
    owner's real export contains a declared short, so the acceptance gate reported a failure
    that had nothing to do with corporate actions, and its 「缺少的公司行動筆數」 hint sent the
    owner hunting for an action row that does not exist.

    Asserted at the LEDGER, not at the preview: the payload carrying "1" proves the parser
    read it, and only ``list_transactions`` proves the writer passed it on. A column that
    parses and is then dropped one call later is the shape this defect already had once.
    """
    _setup(conn)
    csv = (
        "account,symbol,side,date,shares,price,short_sale\n"
        "tw_broker,2330,SELL,2026-06-02,1000,600,1\n"
    )
    p = build_transaction_preview(conn, csv)
    assert p.rows[0].payload["short_sale"] == "1"
    # No 賣超 confirm on a DECLARED short, even though nothing was ever bought.
    assert "sell_exceeds_holdings" not in {i.kind for i in p.rows[0].issues}
    write_transaction_row(conn, p.rows[0])
    assert [t.short_sale for t in list_transactions(conn)] == [True]


def test_an_undeclared_sell_is_still_flagged(conn: sqlite3.Connection) -> None:
    """…and the column must not become a way to silence the guard by omission.

    Same CSV without the flag: the sell exceeds holdings and the confirm-tier issue fires.
    Without this pair the test above passes just as well against a parser that hard-codes
    ``short_sale=True``.
    """
    _setup(conn)
    csv = (
        "account,symbol,side,date,shares,price,short_sale\n"
        "tw_broker,2330,SELL,2026-06-02,1000,600,\n"
    )
    p = build_transaction_preview(conn, csv)
    assert p.rows[0].payload["short_sale"] == "0"
    assert "sell_exceeds_holdings" in {i.kind for i in p.rows[0].issues}
