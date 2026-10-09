"""Y1 (owner ruling 2026-10-05): a refused dividend's remedy has ONE wording and ONE owner.

K1 gave the drawer the owner's remedies — 「改以現金收支登錄這筆配息，並刪除該筆股利」 for a
payout on an open short or a vacated ticker, 「補登該日（含）之前的買進或期初庫存，或刪除該筆股利」
for a dividend with no position — written into ``web/detail.js``, while the strict path's
sentences said 「請刪除該筆股利，或改以現金收支登錄」 ("or") and
「請先補登該日之前的買進或期初庫存，或刪除這筆股利」. Two copies, already apart. The ruling unifies
the cash remedy to 「並」 and asks for one owner so the two cannot drift again: the phrases live
in ``portfolio/cost_basis.py``, the strict sentences end with them, the drawer's
``ledger_issues`` rows carry them as ``remedy``, and no web file holds a copy (a copy there is
exactly how the drift began).
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_corporate_action,
    insert_dividend,
    insert_transaction,
    upsert_instrument,
)
from portfolio_dash.portfolio import cost_basis
from portfolio_dash.portfolio.cost_basis import UnbookableLedgerError, build_book
from portfolio_dash.shared.corporate_actions import CorporateAction, CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.ledger import Dividend, LedgerBundle, Transaction
from tests.conftest import DashboardClientFactory

D = Decimal
_WEB = Path(__file__).resolve().parents[2] / "web"
#: The owner's words, verbatim (K1 ruling + Y1 「並」).
_CASH = "改以現金收支登錄這筆配息，並刪除該筆股利"
_ORPHAN = "補登該日（含）之前的買進或期初庫存，或刪除該筆股利"
_INSTR = {s: Instrument(symbol=s, market=Market.US, quote_ccy=Currency.USD, sector="Tech",
                        name=s) for s in ("OLD", "NEW", "TSLA", "GHOST")}


def _tx(symbol: str, side: Side, day: date, *, short: bool = False) -> Transaction:
    return Transaction(account_id="schwab", symbol=symbol, side=side, quantity=D("100"),
                       price=D("50"), fees=D("0"), tax=D("0"), trade_date=day,
                       short_sale=short)


def _div(symbol: str) -> Dividend:
    return Dividend(account_id="schwab", symbol=symbol, date=date(2026, 4, 1),
                    type=DividendType.NET, gross=D("70"), withholding=D("0"), net=D("70"))


_LEDGERS = {
    "short": LedgerBundle([_tx("TSLA", Side.SELL, date(2026, 1, 10), short=True)],
                          dividends=[_div("TSLA")], instruments=_INSTR),
    "vacated": LedgerBundle(
        [_tx("OLD", Side.BUY, date(2026, 1, 10))], dividends=[_div("OLD")], instruments=_INSTR,
        actions=[CorporateAction(account_id="schwab", date=date(2026, 2, 1),
                                 kind=CorporateActionKind.EXCHANGE, from_symbol="OLD",
                                 to_symbol="NEW", ratio_to=D("1"), ratio_from=D("1"))]),
}


def test_the_phrases_are_the_owners_words() -> None:
    assert cost_basis.CASH_MOVEMENT_REMEDY == _CASH
    assert cost_basis.ORPHAN_DIVIDEND_REMEDY == _ORPHAN


@pytest.mark.parametrize("shape", list(_LEDGERS))
def test_the_strict_refusal_states_the_full_remedy(shape: str) -> None:
    with pytest.raises(UnbookableLedgerError) as exc:
        build_book(_LEDGERS[shape], allow_oversell=False)
    assert str(exc.value).endswith(f"請{_CASH}。"), str(exc.value)


def test_the_orphan_refusal_states_the_drawers_remedy() -> None:
    bundle = LedgerBundle([], dividends=[_div("GHOST")], instruments=_INSTR)
    with pytest.raises(UnbookableLedgerError) as exc:
        build_book(bundle, allow_oversell=False)
    assert str(exc.value).endswith(f"請{_ORPHAN}"), str(exc.value)


def test_the_drawer_rows_carry_the_same_remedy_their_reason_ends_with(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    def seed(conn: sqlite3.Connection) -> None:
        seed_accounts(conn)
        for sym in ("2454", "2317", "2603"):
            upsert_instrument(conn, Instrument(symbol=sym, market=Market.TW,
                                               quote_ccy=Currency.TWD, sector="Electronics",
                                               name=sym, board="TWSE"))
        insert_transaction(conn, account_id="tw_broker", symbol="2454", side=Side.BUY,
                           quantity=D("1000"), price=D("500"), fees=D("0"), tax=D("0"),
                           trade_date=date(2026, 1, 10))
        insert_corporate_action(conn, account_id="tw_broker", action_date=date(2026, 2, 1),
                                kind=CorporateActionKind.EXCHANGE, from_symbol="2454",
                                to_symbol="2317", ratio_to=D("1"), ratio_from=D("1"))
        for sym in ("2454", "2603"):          # vacated, and never held
            insert_dividend(conn, account_id="tw_broker", symbol=sym,
                            div_date=date(2026, 4, 1), div_type="CASH", gross=D("3000"),
                            withholding=D("0"), net=D("3000"))
    client = dashboard_client_factory(seed)
    (vacated,) = client.get("/api/symbol/2454/detail").json()["ledger_issues"][
        "unbookable_dividends"]
    assert vacated["remedy"] == _CASH and vacated["reason"].endswith(f"請{_CASH}。")
    (orphan,) = client.get("/api/symbol/2603/detail").json()["ledger_issues"][
        "orphan_dividends"]
    assert orphan["remedy"] == _ORPHAN and orphan["reason"].endswith(f"請{_ORPHAN}")


def test_no_web_file_holds_a_copy_of_a_remedy_phrase() -> None:
    """The drawer renders the server's ``remedy``; a literal here is a second owner. The
    fragments catch a paraphrase too — the old tooltips said 「請改以現金收支登錄，並刪除…」."""
    copies = [(p.name, frag) for p in sorted(_WEB.glob("*.js"))
              if p.name != "echarts.min.js"
              for frag in ("改以現金收支登錄", "補登該日")
              if frag in p.read_text(encoding="utf-8")]
    assert copies == [], copies
