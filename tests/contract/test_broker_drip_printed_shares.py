"""A reinvested dividend imports at the broker's PRINTED share count (owner ruling B, 2026-10-11).

The converter used to write ``amount / price`` — on every real reinvest row the three printed
columns disagree, and the quantity was read as a rounded view of the other two. The rows after
a reinvestment settle which figure the broker books: a later sale closes the PRINTED sum
exactly, and the positions report equals the printed sums (18 of 18 reinvesting symbols on the
owner's real history; ``amount / price`` matched 0). Under the derived count, that sale is a
賣超 by a few hundred-thousandths of a share — sticky: the cost basis is discarded, no realized
row is written, and XIRR is withheld for good.

These cases drive the whole path a statement takes — ``POST /api/broker/convert`` → the
ordinary ``/api/import/commit`` per file, in the converter's commit order → the replay — and
assert the book the owner would be left with. Everything here is fictional.

⚠ The transactions file commits BEFORE the dividends file (a dividend needs its position
first, CR-02), so its preview cannot see the reinvested shares and names the full sale as a
賣超 row the owner must tick (DEF-025). That question is the preview being honest about what it
can see; what this ruling fixes is the ANSWER the replay gives once the dividends land.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from typing import Any, NamedTuple

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import load_ledger_bundle, upsert_instrument
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from tests.conftest import DashboardClientFactory, _seed_golden

_HEADER = '"Date","Action","Symbol","Description","Quantity","Price","Fees & Comm","Amount"\n'
_SYMBOL = "DRPX"


def _row(day: str, action: str, desc: str, qty: str = "", price: str = "",
         amount: str = "") -> str:
    return (f'"{day}","{action}","{_SYMBOL}","DRIP TEST CORP {desc}",'
            f'"{qty}","{price}","","{amount}"\n')


def _payout(day: str, gross: str, withheld: str, qty: str, price: str, spent: str) -> str:
    """A reinvested payout as the broker prints it, newest line first."""
    return (_row(day, "Reinvest Shares", "REINVESTMENT", qty, price, f"-${spent}")
            + _row(day, "NRA Tax Adj", "W-8 WITHHOLDING", amount=f"-${withheld}")
            + _row(day, "Cash Dividend", "DIVIDEND", amount=f"${gross}"))


class _Case(NamedTuple):
    buy: tuple[str, str, str]                                  # quantity, price, amount
    payouts: tuple[tuple[str, str, str, str, str, str], ...]   # day, gross, withheld, q, p, spent
    sell: tuple[str, str, str]
    printed: list[str]


#: Two reinvestments whose printed counts each round ``amount / price`` UP, so the derived
#: sum falls short of the printed sum the statement later sells. One shape per precision the
#: real history prints: 3 dp (the older broker) and 4 dp (the current one).
_CASES = {
    "3dp": _Case(
        buy=("10", "$60.00", "-$600.00"),
        payouts=(("03/16/2026", "10.37", "3.11", "0.121", "$60.04", "7.26"),
                 ("04/15/2026", "3.07", "0.92", "0.046", "$46.80", "2.15")),
        sell=("10.167", "$50.00", "$508.35"),
        printed=["0.121", "0.046"],
    ),
    "4dp": _Case(
        buy=("10", "$25.00", "-$250.00"),
        payouts=(("03/16/2026", "67.86", "20.36", "1.8137", "$26.19", "47.50"),
                 ("04/15/2026", "18.87", "5.66", "0.4816", "$27.43", "13.21")),
        sell=("12.2953", "$30.00", "$368.86"),
        printed=["1.8137", "0.4816"],
    ),
}


def _statement(case: _Case) -> str:
    qty, price, amount = case.sell
    text = _HEADER + _row("05/04/2026", "Sell", "", qty, price, amount)
    for day, gross, withheld, q, p, spent in reversed(case.payouts):
        text += _payout(day, gross, withheld, q, p, spent)
    bq, bp, ba = case.buy
    return text + _row("02/02/2026", "Buy", "", bq, bp, ba)


def _client(factory: DashboardClientFactory) -> tuple[TestClient, list[sqlite3.Connection]]:
    held: list[sqlite3.Connection] = []

    def seed(conn: sqlite3.Connection) -> None:
        _seed_golden(conn)
        upsert_instrument(conn, Instrument(symbol=_SYMBOL, market=Market.US,
                                           quote_ccy=Currency.USD, sector="Utilities",
                                           name="Drip Test Corp"))
        held.append(conn)

    return factory(seed), held


def _commit_all(
    client: TestClient, files: dict[str, str], order: list[str]
) -> dict[str, list[tuple[int, list[str]]]]:
    """Commit each file in the converter's order, answering the questions the per-row dialog
    would ask: every warned row is acknowledged and every 賣超 row ticked by name (DEF-025).
    Returns each file's warned rows with their kinds, so a caller can say which were asked."""
    asked: dict[str, list[tuple[int, list[str]]]] = {}
    for kind in order:
        pv = client.post("/api/import/preview", json={"kind": kind, "csv_text": files[kind]})
        assert pv.status_code == 200, (kind, pv.text)
        warned = [(r["n"], r["kinds"]) for r in pv.json()["rows"] if r["status"] == "warn"]
        asked[kind] = warned
        body: dict[str, Any] = {"kind": kind, "csv_text": files[kind]}
        if warned:
            body["ack_warnings"] = True
            body["ack_rows"] = [i for i, kinds in warned if "sell_exceeds_holdings" in kinds]
        out = client.post("/api/import/commit", json=body)
        assert out.status_code == 200, (kind, out.text)
    return asked


@pytest.mark.parametrize("name", sorted(_CASES))
def test_selling_the_printed_sum_of_two_reinvests_closes_the_position_cleanly(
    dashboard_client_factory: DashboardClientFactory, name: str,
) -> None:
    case = _CASES[name]
    client, held = _client(dashboard_client_factory)
    conv = client.post("/api/broker/convert", json={
        "account": "schwab", "broker": "schwab", "currency": "USD",
        "exports": [{"name": "drip.csv", "text": _statement(case)}],
    })
    assert conv.status_code == 200, conv.text
    body = conv.json()
    assert body["ok"] is True and body["blocking"] == []

    # The converter writes the PRINTED count, never amount / price.
    dividends = [r for r in body["rows"]["dividends"] if r["type"] == "DRIP"]
    assert [r["shares"] for r in dividends] == case.printed

    asked = _commit_all(client, body["files"], body["commit_order"])
    # The one question on the way: the trades file cannot see the reinvested shares yet.
    assert asked["transactions"] == [(1, ["sell_exceeds_holdings"])]
    assert asked["dividends"] == []
    book = build_book(load_ledger_bundle(held[0]), allow_oversell=True)

    # No 賣超, no position left over, and the sale's realized row is written.
    assert [p for p in book.oversold_positions if p.symbol == _SYMBOL] == []
    assert [h for h in book.holdings if h.symbol == _SYMBOL] == []
    sales = [r for r in book.realized.rows if r.symbol == _SYMBOL and r.kind == "sale"]
    assert [r.shares_sold for r in sales] == [Decimal(case.sell[0])]


def test_the_derived_count_is_what_made_the_sale_a_sticky_oversell(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The ledger the old rule wrote, entered directly: the same sale against the derived
    count is a 賣超, the position stays flagged after it nets out, and no realized row is
    written — the failure the ruling removes, pinned so a revert cannot pass quietly."""
    case = _CASES["3dp"]
    client, held = _client(dashboard_client_factory)
    derived = [Decimal(spent) / Decimal(price.lstrip("$"))
               for _d, _g, _w, _q, price, spent in case.payouts]
    assert sum(derived) < Decimal(case.sell[0]) - Decimal(case.buy[0])
    rows = ["account,symbol,date,type,gross,withholding,net,reinvest_shares,reinvest_price"]
    for (day, gross, withheld, _q, price, spent), shares in zip(
            case.payouts, derived, strict=True):
        m, d, y = day.split("/")
        rows.append(f"schwab,{_SYMBOL},{y}-{m}-{d},DRIP,{gross},{withheld},{spent},"
                    f"{shares:.8f},{price.lstrip('$')}")
    trades = ("account,symbol,side,date,shares,price\n"
              f"schwab,{_SYMBOL},BUY,2026-02-02,10,60.00\n"
              f"schwab,{_SYMBOL},SELL,2026-05-04,10.167,50.00\n")
    _commit_all(client, {"transactions": trades, "dividends": "\n".join(rows) + "\n"},
                ["transactions", "dividends"])
    book = build_book(load_ledger_bundle(held[0]), allow_oversell=True)
    assert [p.symbol for p in book.oversold_positions] == [_SYMBOL]
    assert [r for r in book.realized.rows if r.symbol == _SYMBOL and r.kind == "sale"] == []
