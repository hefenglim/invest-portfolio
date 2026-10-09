"""The stress-audit oracle transcribes K3 (owner ruling 2026-10-05): a dividend refused because it
landed on an open declared short keeps the trend's days incomplete after the short is covered
back to exactly zero — the 待釐清 must not die with a flat carrier.

The oracle keeps its OWN copy of every replay rule (the independence rule), so the ruling has to
reach it by hand: ``OracleResult.refused_dividends`` records the skip, and the phase-1 harness's
``_day_incomplete`` reads it. This test holds the oracle and the app together BEHAVIOURALLY on
the 2609 lifecycle's shape (short → dividend while short → cover to 0): the days the oracle says
carry a refused dividend are the days the app's trend marks incomplete, before and after the
cover. ``oracle.py`` imports only the standard library and is loaded straight from its path.
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
    insert_dividend,
    insert_transaction,
    upsert_instrument,
)
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import GOLDEN_NOW, DashboardClientFactory

_ORACLE = Path(__file__).resolve().parents[2] / "scripts" / "stress_audit" / "oracle.py"
D = Decimal
_SHORT, _PAY, _COVER = date(2026, 1, 5), date(2026, 2, 2), date(2026, 3, 2)


def _oracle() -> ModuleType:
    spec = importlib.util.spec_from_file_location("stress_oracle_k3", _ORACLE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module          # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


def _facts(o: Any) -> Any:
    inst = o.Instrument(symbol="2609", market="TW", quote_ccy="TWD", is_etf=False)
    short = o.TxFact(id=1, account_id="tw_broker", symbol="2609", side="SELL", qty=D("100"),
                     price=D("100"), fee=D("0"), tax=D("0"), trade_date=_SHORT,
                     short_sale=True)
    cover = o.TxFact(id=2, account_id="tw_broker", symbol="2609", side="BUY", qty=D("100"),
                     price=D("95"), fee=D("0"), tax=D("0"), trade_date=_COVER)
    div = o.DivFact(id=1, account_id="tw_broker", symbol="2609", d=_PAY, type="CASH",
                    gross=D("3000"), withholding=D("0"), net=D("3000"),
                    reinvest_shares=None, reinvest_price=None)
    return o.Facts(txs=[short, cover], divs=[div], instruments={"2609": inst})


def _seed(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="2609", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Shipping", name="Yang Ming", board="TWSE"))
    insert_transaction(conn, account_id="tw_broker", symbol="2609", side=Side.SELL,
                       quantity=D("100"), price=D("100"), fees=D("0"), tax=D("0"),
                       trade_date=_SHORT, short_sale=True)
    insert_dividend(conn, account_id="tw_broker", symbol="2609", div_date=_PAY,
                    div_type="CASH", gross=D("3000"), withholding=D("0"), net=D("3000"))
    insert_transaction(conn, account_id="tw_broker", symbol="2609", side=Side.BUY,
                       quantity=D("100"), price=D("95"), fees=D("0"), tax=D("0"),
                       trade_date=_COVER)
    upsert_prices(conn, [PriceRow(instrument="2609", market=Market.TW, as_of=date(2026, 1, 2),
                                  close=D("100"), source="test")], fetched_at=GOLDEN_NOW)


def test_the_oracle_and_the_app_agree_on_the_days_a_refused_dividend_marks(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    o = _oracle()
    facts = _facts(o)
    points = {p["date"]: p["incomplete"] for p in
              dashboard_client_factory(_seed).get("/api/dashboard").json()["trend"]["points"]}
    days = (date(2026, 2, 1), _PAY, date(2026, 3, 1), _COVER, GOLDEN_NOW.date())
    for day in days:
        res = o.replay_through(facts, day)
        assert bool(res.refused_dividends) is (day >= _PAY), day
        assert points[day.isoformat()] is (day >= _PAY), day
    # After the cover nothing is held — the flag has no carrier, and the record still stands.
    assert o.replay_through(facts, GOLDEN_NOW.date()).holdings == {}
