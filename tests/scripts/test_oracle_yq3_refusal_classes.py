"""YQ3 (owner ruling 2026-10-05): the stress-audit oracle refuses all three dividend classes the
app refuses, from its OWN reading of ``domain-ledger.md``.

Until YQ3 the oracle modelled only the open-short class: a dividend with no position made it
RAISE (``dividend for unknown position``), and a dividend on a ticker an EXCHANGE vacated (E24)
was booked — as post-close income for a cash row, or a zero-cost resurrection for a 配股. The
oracle now keeps ``vacated_to`` (set by an applied EXCHANGE, cleared by whatever puts shares
back on the position — CR-06) and refuses the two classes as the rules define them.

The oracle never imports the app (the independence rule), so these tests hold the two copies
together BEHAVIOURALLY on the same ledger: which rows each refuses (the ledger listing's
``booked``), the cash pool, the carrier's flag, and the XIRR withholding for an orphan.
``oracle.py`` imports only the standard library and is loaded straight from its path.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_corporate_action,
    insert_dividend,
    insert_transaction,
    upsert_instrument,
)
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import DashboardClientFactory

_ORACLE = Path(__file__).resolve().parents[2] / "scripts" / "stress_audit" / "oracle.py"
D = Decimal
_SYMS = ("VCO", "VCN", "ORP", "REO")


def _oracle() -> ModuleType:
    spec = importlib.util.spec_from_file_location("stress_oracle_yq3", _ORACLE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module          # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


def _facts(o: Any, *, reopen: bool = False) -> Any:
    """VCO bought, EXCHANGEd into VCN, then a CASH and a 配股 on VCO (E24); an orphan CASH on
    ORP; REO exchanged away and BOUGHT BACK before its payout (CR-06 — that one books)."""
    insts = {s: o.Instrument(symbol=s, market="TW", quote_ccy="TWD", is_etf=False)
             for s in _SYMS + ("REN",)}
    txs = [o.TxFact(id=1, account_id="tw_broker", symbol="VCO", side="BUY", qty=D("1000"),
                    price=D("50"), fee=D("0"), tax=D("0"), trade_date=date(2026, 2, 9)),
           o.TxFact(id=2, account_id="tw_broker", symbol="REO", side="BUY", qty=D("100"),
                    price=D("20"), fee=D("0"), tax=D("0"), trade_date=date(2026, 2, 9))]
    if reopen:
        txs.append(o.TxFact(id=3, account_id="tw_broker", symbol="REO", side="BUY",
                            qty=D("10"), price=D("20"), fee=D("0"), tax=D("0"),
                            trade_date=date(2026, 3, 20)))
    acts = [o.CorpFact(id=1, account_id="tw_broker", d=date(2026, 3, 9), kind="EXCHANGE",
                       from_symbol="VCO", to_symbol="VCN", ratio_to=D("1"), ratio_from=D("1")),
            o.CorpFact(id=2, account_id="tw_broker", d=date(2026, 3, 9), kind="EXCHANGE",
                       from_symbol="REO", to_symbol="REN", ratio_to=D("1"), ratio_from=D("1"))]

    def div(i: int, sym: str, d: date, kind: str, net: str,
            shares: str | None = None) -> Any:
        return o.DivFact(id=i, account_id="tw_broker", symbol=sym, d=d, type=kind,
                         gross=D(net), withholding=D("0"), net=D(net),
                         reinvest_shares=None if shares is None else D(shares),
                         reinvest_price=None)
    divs = [div(1, "VCO", date(2026, 4, 9), "CASH", "3000"),
            div(2, "VCO", date(2026, 4, 16), "STOCK", "0", "50"),
            div(3, "ORP", date(2026, 3, 23), "CASH", "900"),
            div(4, "REO", date(2026, 4, 9), "CASH", "200")]
    return o.Facts(txs=txs, divs=divs, instruments=insts, actions=acts)


def test_the_oracle_refuses_the_vacated_and_orphan_classes_and_books_the_reopened_one() -> None:
    o = _oracle()
    res = o.replay(_facts(o, reopen=True))           # no raise on the orphan any more
    assert res.refused_dividend_ids == frozenset({1, 2, 3})       # 4 books: REO was reopened
    assert res.orphan_dividends == [("tw_broker", "ORP", date(2026, 3, 23))]
    assert set(res.holdings) == {("tw_broker", "VCN"), ("tw_broker", "REN"),
                                 ("tw_broker", "REO")}             # VCO not resurrected
    assert res.holdings[("tw_broker", "VCN")].unbookable_dividend is True
    assert res.holdings[("tw_broker", "REO")].unbookable_dividend is False
    assert [r.kind for r in res.realized_rows] == []                # no income on a dead ticker
    # ⑪: only the reopened REO payout reaches the pool (buys 50,000 + 2,000 + 200 out).
    assert res.cash[("tw_broker", "TWD")] == D("-52200") + D("200")
    assert o.integrity_findings(_facts(o, reopen=True), res.realized_rows) == []


def test_without_the_reopen_the_exchanged_ticker_refuses_its_payout_too() -> None:
    o = _oracle()
    res = o.replay(_facts(o))
    assert res.refused_dividend_ids == frozenset({1, 2, 3, 4})
    assert res.holdings[("tw_broker", "REN")].unbookable_dividend is True


def _seed(conn: sqlite3.Connection) -> None:
    """The same ledger through the app's store (below every door, like legacy data)."""
    seed_accounts(conn)
    for sym in _SYMS + ("REN",):
        upsert_instrument(conn, Instrument(symbol=sym, market=Market.TW,
                                           quote_ccy=Currency.TWD, sector="Industrials",
                                           name=sym, board="TWSE"))
    for sym, qty, price, day in (("VCO", "1000", "50", date(2026, 2, 9)),
                                 ("REO", "100", "20", date(2026, 2, 9)),
                                 ("REO", "10", "20", date(2026, 3, 20))):
        insert_transaction(conn, account_id="tw_broker", symbol=sym, side=Side.BUY,
                           quantity=D(qty), price=D(price), fees=D("0"), tax=D("0"),
                           trade_date=day)
    for src, dst in (("VCO", "VCN"), ("REO", "REN")):
        insert_corporate_action(conn, account_id="tw_broker", action_date=date(2026, 3, 9),
                                kind=CorporateActionKind.EXCHANGE, from_symbol=src,
                                to_symbol=dst, ratio_to=D("1"), ratio_from=D("1"))
    for sym, day, kind, net, shares in (("VCO", date(2026, 4, 9), "CASH", "3000", None),
                                        ("VCO", date(2026, 4, 16), "STOCK", "0", "50"),
                                        ("ORP", date(2026, 3, 23), "CASH", "900", None),
                                        ("REO", date(2026, 4, 9), "CASH", "200", None)):
        insert_dividend(conn, account_id="tw_broker", symbol=sym, div_date=day, div_type=kind,
                        gross=D(net), withholding=D("0"), net=D(net),
                        reinvest_shares=None if shares is None else D(shares))


def test_the_oracle_and_the_app_agree_row_by_row(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    o = _oracle()
    res = o.replay(_facts(o, reopen=True))
    client = dashboard_client_factory(_seed)
    rows = client.get("/api/ledgers/dividends", params={"limit": 50}).json()["rows"]
    app_booked = {(r["symbol"], r["date"]): r["booked"] for r in rows}
    oracle_booked = {(dv.symbol, dv.d.isoformat()): dv.id not in res.refused_dividend_ids
                     for dv in _facts(o, reopen=True).divs}
    assert app_booked == oracle_booked
    pools = {(b["account_id"], b["ccy"]): D(b["amount"])
             for b in client.get("/api/cash").json()["balances"]}
    assert pools[("tw_broker", "TWD")] == res.cash[("tw_broker", "TWD")]
    dash = client.get("/api/dashboard").json()
    held = {(h["symbol"], h["unbookable_dividend"]) for h in dash["holdings"]}
    assert held == {(s, res.holdings[("tw_broker", s)].unbookable_dividend)
                    for s in ("VCN", "REN", "REO")}
    # CR-02: an orphan withholds XIRR and the reason names the row.
    assert dash["kpis"]["xirr"] is None
    reason = dash["freshness"]["xirr_unavailable_reason"]
    assert "ORP" in reason and "2026-03-23" in reason
