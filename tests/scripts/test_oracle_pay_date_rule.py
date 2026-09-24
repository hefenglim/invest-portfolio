"""The stress-audit oracle transcribes the valuation cut of 2026-09-24 (DEF-016, widened by
DEF-056): every row — a dividend from its pay date, a trade / opening / conversion from its
own date — counts from that day on.

The oracle keeps its OWN transcription of every replay rule (the independence rule — an
oracle that imports the implementation cannot detect an error in it), so the owner's ruling
「a dividend counts from its pay date」 has to reach it by hand, and this test holds the two
copies together BEHAVIOURALLY: the oracle's valuation cut, run on facts, gives the adjusted
cost the app's dashboard gives for the same ledger on either side of the pay date.

``oracle.py`` imports only the standard library, so it is loaded straight from its path —
nothing here adds ``scripts/`` to the app's import graph or to the mypy gate.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any
from zoneinfo import ZoneInfo

from portfolio_dash.data_ingestion.store import insert_dividend, insert_transaction
from portfolio_dash.shared.models.enums import Side
from tests.conftest import DashboardClientFactory, _seed_golden

_ORACLE = Path(__file__).resolve().parents[2] / "scripts" / "stress_audit" / "oracle.py"
D = Decimal
_PAY = date(2026, 7, 1)


def _oracle() -> ModuleType:
    spec = importlib.util.spec_from_file_location("stress_oracle_def016", _ORACLE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module          # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


def _facts(o: Any) -> Any:
    """The golden subset's 2330 position as oracle facts, plus an unpaid 18,200 dividend."""
    inst = o.Instrument(symbol="2330", market="TW", quote_ccy="TWD", is_etf=False)
    buy = o.TxFact(id=1, account_id="tw_broker", symbol="2330", side="BUY", qty=D("1000"),
                   price=D("500"), fee=D("0"), tax=D("0"), trade_date=date(2026, 1, 5))
    paid = o.DivFact(id=1, account_id="tw_broker", symbol="2330", d=date(2026, 3, 1),
                     type="CASH", gross=D("5000"), withholding=D("0"), net=D("5000"),
                     reinvest_shares=None, reinvest_price=None)
    unpaid = o.DivFact(id=2, account_id="tw_broker", symbol="2330", d=_PAY, type="CASH",
                       gross=D("18200"), withholding=D("0"), net=D("18200"),
                       reinvest_shares=None, reinvest_price=None, ex_date=date(2026, 6, 5))
    return o.Facts(txs=[buy], divs=[paid, unpaid], instruments={"2330": inst})


def _app_adjusted(client: Any) -> Decimal:
    dash = client.get("/api/dashboard").json()
    h = next(x for x in dash["holdings"]
             if x["account_id"] == "tw_broker" and x["symbol"] == "2330")
    return D(h["adjusted_cost_total"])


def _with_unpaid(conn: sqlite3.Connection) -> None:
    _seed_golden(conn)
    insert_dividend(conn, account_id="tw_broker", symbol="2330", div_date=_PAY,
                    div_type="CASH", gross=D("18200"), withholding=D("0"), net=D("18200"),
                    ex_date=date(2026, 6, 5))


def test_the_oracle_cuts_an_unpaid_dividend_exactly_where_the_app_does(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    o = _oracle()
    facts = _facts(o)
    for now in (datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei")),
                datetime(2026, 7, 1, 9, 0, tzinfo=ZoneInfo("Asia/Taipei"))):
        res = o.replay(o.facts_valued_as_of(facts, now.date()))
        oracle_adjusted = res.holdings[("tw_broker", "2330")].adjusted_total
        app = dashboard_client_factory(_with_unpaid, now=now)
        assert oracle_adjusted == _app_adjusted(app), now


def test_the_cut_is_a_date_not_a_deletion() -> None:
    o = _oracle()
    facts = _facts(o)
    before = o.facts_valued_as_of(facts, date(2026, 6, 30))
    assert [d.id for d in before.divs] == [1] and before.txs == facts.txs
    assert [d.id for d in o.facts_valued_as_of(facts, _PAY).divs] == [1, 2]


def _with_future_buy(conn: sqlite3.Connection) -> None:
    _seed_golden(conn)
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=D("1000"), price=D("600"), fees=D("855"), tax=D("0"),
                       trade_date=_PAY)


def test_the_oracle_cuts_a_future_trade_exactly_where_the_app_does(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """DEF-056: a BUY dated after the valuation day is not in the position yet — in the
    oracle's own transcription as in the app — and is from its date on."""
    o = _oracle()
    facts = _facts(o)
    future = o.TxFact(id=2, account_id="tw_broker", symbol="2330", side="BUY", qty=D("1000"),
                      price=D("600"), fee=D("855"), tax=D("0"), trade_date=_PAY)
    facts = o.Facts(txs=[*facts.txs, future], divs=facts.divs[:1],
                    instruments=facts.instruments)
    for now in (datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei")),
                datetime(2026, 7, 1, 9, 0, tzinfo=ZoneInfo("Asia/Taipei"))):
        held = o.replay(o.facts_valued_as_of(facts, now.date())).holdings[("tw_broker", "2330")]
        dash = dashboard_client_factory(_with_future_buy, now=now).get("/api/dashboard").json()
        app = next(x for x in dash["holdings"]
                   if x["account_id"] == "tw_broker" and x["symbol"] == "2330")
        assert (held.shares, held.original_total) == (D(app["shares"]),
                                                      D(app["original_cost_total"])), now
