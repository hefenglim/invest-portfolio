"""A trade moves its SETTLED 價金 — the currency's minor unit — wherever a figure of record
reads it.

Owner ruling 2026-09-30 (the verifier's R8 note on B-06, 「美股小數股成交金額未量化到 cent，USD
池出現 3 位小數」; the method was left to the developer: 「量化方式由開發者依券商實務決定」).
Root cause (d2e5e08): every site that turned a trade into money multiplied quantity by price
and used the raw product — ``portfolio/cash.py:138/141, 231-232``, ``portfolio/
cost_basis.py:617/639/651/704``, ``portfolio/returns.py:203/205``, ``portfolio/
timeseries.py:182``, ``forex/pools.py:241/243``, ``data_ingestion/fees.py:314``, ``strategy/
whatif.py:245``, ``api/routers/symbol.py:664/673``, ``export/ledgers_report.py:191``,
``api/routers/input_center.py:529/979/1127`` and ``api/routers/ledgers.py:235`` — so 0.5 AAPL
at 191.23 took 95.615 USD from the pool, an amount no account holds. The rule
(``shared.money.settled_notional``): TWD drops everything below the dollar — TWSE computes an
odd-lot 交割價金 per order and price with 元以下捨去 (2024-04-01); USD / MYR round the cent /
sen half up. A product already in the minor unit is returned untouched, so every whole-lot
trade — the golden ledger, the demo — is byte-identical.

Why nothing caught it: every fixture trades whole lots at 2-dp prices, whose product is
already in the minor unit; the oracle transcribed the same raw product.
"""

from __future__ import annotations

import ast
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

import portfolio_dash
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import StoredTransaction, upsert_instrument
from portfolio_dash.portfolio.cash import cash_balances, pool_lines
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.models.ledger import LedgerBundle, Transaction
from portfolio_dash.shared.money import settled_notional
from tests.conftest import DashboardClientFactory

D = Decimal
_PKG = Path(portfolio_dash.__file__).resolve().parent


@pytest.mark.parametrize("qty, price, ccy, settled", [
    ("0.5", "191.23", Currency.USD, "95.62"),     # 95.615 → half up to the cent
    ("0.5", "191.21", Currency.USD, "95.61"),     # 95.605 → 95.61
    ("3", "45.25", Currency.TWD, "135"),          # 135.75 → 元以下捨去
    ("7", "12.95", Currency.TWD, "90"),           # 90.65 → 90
    ("37", "0.505", Currency.MYR, "18.69"),       # 18.685 → half up to the sen
])
def test_the_settlement_rule_per_currency(qty: str, price: str, ccy: Currency,
                                          settled: str) -> None:
    assert settled_notional(D(qty), D(price), ccy) == D(settled)


@pytest.mark.parametrize("qty, price, ccy, text", [
    ("1000", "45.2", Currency.TWD, "45200.0"),
    ("10", "150.00", Currency.USD, "1500.00"),
    ("100", "9.87", Currency.MYR, "987.00"),
])
def test_a_product_already_in_the_minor_unit_is_returned_as_is(
    qty: str, price: str, ccy: Currency, text: str
) -> None:
    """Not re-quantized: ``45200.0`` must not become ``45200`` in every figure built on it."""
    assert str(settled_notional(D(qty), D(price), ccy)) == text


_AAPL = Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD, sector="Tech",
                   name="Apple")
_TW = Instrument(symbol="2884", market=Market.TW, quote_ccy=Currency.TWD,
                 sector="Financials", name="玉山金")
_MY = Instrument(symbol="7777", market=Market.MY, quote_ccy=Currency.MYR,
                 sector="Industrials", name="Penny Bhd")


def _trade(i: Instrument, account: str, side: Side, qty: str, price: str, fees: str,
           d: date, tid: int) -> StoredTransaction:
    return StoredTransaction(id=tid, account_id=account, symbol=i.symbol, side=side,
                             quantity=D(qty), price=D(price), fees=D(fees), tax=D("0"),
                             trade_date=d)


@pytest.mark.parametrize("inst, account, qty, buy_px, sell_px, fee, cost, proceeds", [
    # 0.5 × 191.23 = 95.615 → 95.62; 0.5 × 191.25 = 95.625 → 95.63
    (_AAPL, "schwab", "0.5", "191.23", "191.25", "0", "95.62", "95.63"),
    # 3 × 45.25 = 135.75 → 135 (+20 fee); 3 × 46.35 = 139.05 → 139 (−20 fee)
    (_TW, "tw_broker", "3", "45.25", "46.35", "20", "155", "119"),
    # 37 × 0.505 = 18.685 → 18.69 (+3 fee); 37 × 0.515 = 19.055 → 19.06 (−3 fee)
    (_MY, "moomoo_my", "37", "0.505", "0.515", "3", "21.69", "16.06"),
])
def test_the_cost_the_proceeds_and_the_cash_pool_book_one_amount(
    inst: Instrument, account: str, qty: str, buy_px: str, sell_px: str, fee: str,
    cost: str, proceeds: str,
) -> None:
    buy = _trade(inst, account, Side.BUY, qty, buy_px, fee, date(2026, 3, 2), 1)
    sell = _trade(inst, account, Side.SELL, qty, sell_px, fee, date(2026, 3, 9), 2)
    instruments = {inst.symbol: inst}

    held = build_book(LedgerBundle([Transaction(**buy.model_dump(exclude={"id"}))],
                                   instruments=instruments))
    [h] = held.holdings
    assert h.original_cost_total == D(cost)

    closed = build_book(LedgerBundle([Transaction(**t.model_dump(exclude={"id"}))
                                      for t in (buy, sell)], instruments=instruments))
    [row] = closed.realized.rows
    assert row.proceeds_net == D(proceeds)
    assert row.realized == D(proceeds) - D(cost)

    pools = cash_balances([], [], [buy, sell], [], instruments)
    assert pools[(account, inst.quote_ccy)] == D(proceeds) - D(cost)
    lines = pool_lines(account, inst.quote_ccy, [], [], [buy, sell], [], instruments)
    assert [ln.delta for ln in lines] == [-D(cost), D(proceeds)]


# --- the class: every quantity × price in the package ---------------------------------------

_QTY = {"quantity", "shares", "qty", "shares_sold", "reinvest_shares"}
_PRICE = {"price", "close", "px", "trade_price", "reinvest_price"}

#: Products of a quantity and a price that are NOT a settlement — each with why. Every other
#: one must be ``settled_notional``.
_NOT_A_SETTLEMENT = {
    # the rule itself
    ("shared/money.py", "settled_notional"): 1,
    # VALUATIONS — price × shares HELD: a mark, not a trade; full precision until display
    ("portfolio/pnl.py", "value_holdings"): 1,
    ("portfolio/returns.py", "xirr_reporting"): 1,     # the final market-value inflow
    ("portfolio/timeseries.py", "daily_value_series"): 1,
    # a PROJECTION at today's price (a suggested order), never booked
    ("strategy/rebalance.py", "amount"): 1,
    # CHECKS with a tolerance band against a statement's own amount, never booked
    ("data_ingestion/agents.py", "_append_amount_check"): 1,
    ("data_ingestion/broker/reconcile.py", "_reinvest_cash"): 1,
    ("data_ingestion/broker/reconcile.py", "_check_priced_rows"): 1,
    # unreachable fallbacks for a symbol with no registry row (no ledger rows either)
    ("api/routers/symbol.py", "symbol_detail"): 1,
    ("export/ledgers_report.py", "_transactions_section"): 1,
    ("api/routers/ledgers.py", "transactions"): 1,
    # the manual door with neither an instrument nor a fee rule: an unknown account, which
    # the door refuses before anything is booked
    ("api/routers/input_center.py", "_settled_gross"): 1,
}


def _name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _quantity_times_price() -> dict[tuple[str, str], int]:
    found: dict[tuple[str, str], int] = {}
    for path in sorted(_PKG.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        owner: dict[ast.AST, str] = {}
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                for node in ast.walk(fn):
                    owner.setdefault(node, fn.name)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult)):
                continue
            a, b = _name(node.left), _name(node.right)
            if a and b and ((a in _QTY and b in _PRICE) or (a in _PRICE and b in _QTY)):
                key = (path.relative_to(_PKG).as_posix(), owner.get(node, "<module>"))
                found[key] = found.get(key, 0) + 1
    return found


def test_every_quantity_times_price_is_settled_or_listed() -> None:
    assert _quantity_times_price() == _NOT_A_SETTLEMENT


def test_the_manual_door_previews_writes_and_lists_the_settled_amount(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """Through the real door: the preview, the commit, the ledger list and the dashboard
    all read 95.62 for 0.5 AAPL at 191.23 (Schwab charges a buy nothing)."""
    def seed(conn: sqlite3.Connection) -> None:
        seed_accounts(conn)
        upsert_instrument(conn, _AAPL)

    client = dashboard_client_factory(seed)
    body = {"account_id": "schwab", "symbol": "AAPL", "date": "2026-03-02", "side": "BUY",
            "shares": "0.5", "price": "191.23"}
    preview = client.post("/api/input/manual/preview", json=body).json()
    assert (preview["gross"], preview["fee"], preview["total"]) == ("95.62", "0", "-95.62")
    commit = client.post("/api/input/manual/commit", json=body)
    assert commit.status_code == 201, commit.text
    assert commit.json()["total"] == "-95.62"
    [row] = client.get("/api/ledgers/transactions").json()["rows"]
    assert row["total"] == "-95.62"
    [held] = [h for h in client.get("/api/dashboard").json()["holdings"]
              if h["symbol"] == "AAPL" and h["account_id"] == "schwab"]
    assert Decimal(held["original_cost_total"]) == D("95.62")
