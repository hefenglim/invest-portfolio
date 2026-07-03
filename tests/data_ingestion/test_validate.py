import sqlite3
from datetime import date
from decimal import Decimal

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.holdings import current_shares
from portfolio_dash.data_ingestion.validate import TxnInput, validate_transaction
from portfolio_dash.shared.models.enums import Side


def _raw_tx(conn: sqlite3.Connection, acc: str, sym: str, side: Side, qty: str) -> None:
    conn.execute(
        "INSERT INTO transactions (account_id, symbol, side, quantity, price, fees, tax, "
        "trade_date) VALUES (?,?,?,?,?,?,?,?)",
        (acc, sym, side.value, qty, "100", "0", "0", "2026-01-01"))
    conn.commit()


def _inp(acc: str, sym: str, side: Side, qty: str, price: str = "100") -> TxnInput:
    return TxnInput(account_id=acc, symbol=sym, side=side, quantity=Decimal(qty),
                    price=Decimal(price), trade_date=date(2026, 6, 1))


def test_current_shares_sums_buys_minus_sells(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    _raw_tx(conn, "tw_broker", "2330", Side.BUY, "1000")
    _raw_tx(conn, "tw_broker", "2330", Side.SELL, "300")
    assert current_shares(conn, "tw_broker", "2330") == Decimal("700")


def test_current_shares_counts_opening_and_noncash_dividends(
    conn: sqlite3.Connection,
) -> None:
    """Regression (fixed 2026-07-02): opening inventory + stock/DRIP shares count.

    The original transactions-only sum made opening-backed positions look smaller
    -> FALSE oversell warnings when selling them.
    """
    seed_accounts(conn)
    conn.execute(
        "INSERT INTO opening_inventory (account_id, symbol, shares, original_avg_cost, "
        "original_cost_total, build_date) VALUES ('tw_broker','2330','500','450',"
        "'225000','2026-01-02')")
    _raw_tx(conn, "tw_broker", "2330", Side.BUY, "1000")
    _raw_tx(conn, "tw_broker", "2330", Side.SELL, "300")
    # 配股 (stock dividend): +100 zero-cost shares; CASH dividend adds none.
    conn.execute(
        "INSERT INTO dividends (account_id, symbol, date, type, gross, withholding, net, "
        "reinvest_shares) VALUES ('tw_broker','2330','2026-03-01','STOCK','0','0','0','100')")
    conn.execute(
        "INSERT INTO dividends (account_id, symbol, date, type, gross, withholding, net) "
        "VALUES ('tw_broker','2330','2026-04-01','CASH','5000','0','5000')")
    conn.commit()
    # 500 opening + 1000 buy - 300 sell + 100 stock-dividend = 1300
    assert current_shares(conn, "tw_broker", "2330") == Decimal("1300")


def test_sell_of_opening_backed_position_no_false_oversell(
    conn: sqlite3.Connection,
) -> None:
    seed_accounts(conn)
    conn.execute(
        "INSERT INTO opening_inventory (account_id, symbol, shares, original_avg_cost, "
        "original_cost_total, build_date) VALUES ('tw_broker','2330','1000','450',"
        "'450000','2026-01-02')")
    conn.commit()
    issues = validate_transaction(conn, _inp("tw_broker", "2330", Side.SELL, "800"))
    assert "sell_exceeds_holdings" not in {i.kind for i in issues}


def test_sell_exceeds_holdings_blocks(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    _raw_tx(conn, "tw_broker", "2330", Side.BUY, "100")
    issues = validate_transaction(conn, _inp("tw_broker", "2330", Side.SELL, "500"))
    kinds = {i.kind for i in issues}
    assert "sell_exceeds_holdings" in kinds
    assert any(i.needs_confirm for i in issues if i.kind == "sell_exceeds_holdings")


def test_valid_buy_has_no_issues(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    assert validate_transaction(conn, _inp("tw_broker", "2330", Side.BUY, "100")) == []


def test_unknown_account_flagged(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    issues = validate_transaction(conn, _inp("nope", "2330", Side.BUY, "100"))
    assert any(i.kind == "unknown_account" for i in issues)


def test_non_positive_qty_price_flagged(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    issues = validate_transaction(conn, _inp("tw_broker", "2330", Side.BUY, "0", price="0"))
    kinds = {i.kind for i in issues}
    assert "non_positive_quantity" in kinds and "non_positive_price" in kinds


def test_shares_on_counts_strictly_before_date(conn: sqlite3.Connection) -> None:
    """Dividend entitlement (R4): events dated ON the cutoff do NOT count."""
    from datetime import date as _date

    from portfolio_dash.data_ingestion.holdings import shares_on

    seed_accounts(conn)
    conn.execute(
        "INSERT INTO opening_inventory (account_id, symbol, shares, original_avg_cost, "
        "original_cost_total, build_date) VALUES ('tw_broker','2330','500','450',"
        "'225000','2026-01-02')")
    _raw_tx(conn, "tw_broker", "2330", Side.BUY, "1000")      # 2026-01-01
    conn.execute(
        "INSERT INTO transactions (account_id, symbol, side, quantity, price, fees, tax, "
        "trade_date) VALUES ('tw_broker','2330','SELL','200','100','0','0','2026-03-01')")
    conn.commit()
    # cutoff 2026-01-02: only the 01-01 buy counts (opening ON the date excluded)
    assert shares_on(conn, "tw_broker", "2330", before=_date(2026, 1, 2)) == Decimal("1000")
    # cutoff 2026-02-01: opening + buy
    assert shares_on(conn, "tw_broker", "2330", before=_date(2026, 2, 1)) == Decimal("1500")
    # cutoff 2026-04-01: after the sell
    assert shares_on(conn, "tw_broker", "2330", before=_date(2026, 4, 1)) == Decimal("1300")
