"""Options W7b — the cross tests no single wave-1 package could run (plan §8 W7b).

W3's doors were built while W2's replay still refused every ``OPTION_ADJUST`` and booked no
refused close; W4's readers were built before W2 made ``DashboardData.holdings`` stock-only.
Each case here crosses two packages on the merged branch:

* an adjusted contract through the doors (manual preview / commit, the expiry rule, the
  ``/api/input/holdings`` picker) — W3's validators over W2's replay and share walker;
* a refused close on every surface that lists it (the 交易 tab, the dashboard, the manual
  preview) — W2's record read by W3's wires;
* ``option_side`` identical on the ledgers wire and the cash statement, and agreeing with the
  replay's realized rows — W1's classifier against W2's books;
* ``/api/input/holdings`` direction / 口數 equal to ``options.open``;
* ``DashboardData.holdings`` readers see no option (W4 relied on W2 for these);
* KPI 總市值 = the stock grand subtotal + ``options.market_value`` on an option ledger.

Synthetic contracts only; the golden day is 2026-06-11, so every expiry below is still ahead.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.register import register_option_contract
from portfolio_dash.data_ingestion.store import (
    insert_corporate_action,
    insert_transaction,
    list_transactions,
    upsert_instrument,
)
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.options import is_option_symbol
from tests.conftest import GOLDEN_NOW

D = Decimal
SRC = "XYZ261218P00060000"          # XYZ 2026-12-18 60 Put
DST = "XYZ261218P00020000"          # XYZ 2026-12-18 20 Put — 60 × 1 = 20 × 3
CALL = "XYZ261218C00070000"


def _trade(conn: sqlite3.Connection, symbol: str, side: Side, qty: str, day: date, *,
           price: str = "2", fees: str = "0.65", short: bool = False,
           close: str | None = None) -> int:
    return insert_transaction(
        conn, account_id="schwab", symbol=symbol, side=side, quantity=D(qty),
        price=D("0") if close else D(price), fees=D("0") if close else D(fees), tax=D("0"),
        trade_date=day, short_sale=short,
        fee_rule_snapshot={"source": "close_reason"} if close else None,
        close_reason=close)  # type: ignore[arg-type]


def _adjusted_short(conn: sqlite3.Connection) -> int:
    """STO 1 口 SRC, then a 1 → 3 adjustment onto DST: a 3-contract short in DST."""
    register_option_contract(conn, SRC)
    register_option_contract(conn, DST)
    sto = _trade(conn, SRC, Side.SELL, "100", date(2026, 3, 2), short=True)
    insert_corporate_action(conn, account_id="schwab", action_date=date(2026, 4, 1),
                            kind=CorporateActionKind.OPTION_ADJUST, from_symbol=SRC,
                            to_symbol=DST, ratio_to=D(3), ratio_from=D(1))
    return sto


def _manual(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"account_id": "schwab", "symbol": DST, "date": "2026-05-04",
                            "price": "0.40"}
    body.update(over)
    return body


def _errors(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [i for i in body["issues"] if i["sev"] == "error"]


def _ledger(client: TestClient) -> dict[int, dict[str, Any]]:
    r = client.get("/api/ledgers/transactions")
    assert r.status_code == 200, r.text
    return {row["id"]: row for row in r.json()["rows"]}


# --- an adjusted contract through W3's doors ---------------------------------------------------


def test_the_book_and_the_picker_agree_on_the_adjusted_short(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    _adjusted_short(golden_db)
    dash = api_client.get("/api/dashboard").json()
    (row,) = dash["options"]["open"]
    assert (row["symbol"], row["direction"], row["contracts"]) == (DST, "short", "3")
    held = {r["symbol"]: r for r in api_client.get(
        "/api/input/holdings", params={"account": "schwab"}).json()["held"]}
    assert (held[DST]["direction"], held[DST]["contracts"]) == ("short", "3")
    assert SRC not in held


def test_buy_to_close_the_adjusted_short_is_clean_and_books_the_whole_premium(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    _adjusted_short(golden_db)
    r = api_client.post("/api/input/manual/preview",
                        json=_manual(contracts="3", option_intent="BTC"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert not _errors(body), body["issues"]
    assert body["position_preview"]["new_shares"] == "0"
    beyond = api_client.post("/api/input/manual/commit",
                             json=_manual(contracts="4", option_intent="BTC"))
    assert beyond.status_code == 400, beyond.text
    assert beyond.json()["error"]["message"] == (
        "買進平倉口數超過空頭部位（3 口），請分成平倉與開倉兩筆")
    done = api_client.post("/api/input/manual/commit",
                           json=_manual(contracts="3", option_intent="BTC"))
    assert done.status_code == 201, done.text
    book = build_dashboard(golden_db, now=GOLDEN_NOW, reporting=Currency.TWD)
    assert book.options is not None and book.options.open == []
    (rr,) = [r for r in book.options.realized.rows if r.symbol == DST]
    assert rr.kind == "short_cover" and rr.shares_sold == D(300)


def test_the_expiry_rule_reads_the_adjusted_position(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    _adjusted_short(golden_db)
    body = _manual(date="2026-12-18", close_reason="expired", contracts="3")
    body.pop("price")
    r = api_client.post("/api/input/manual/preview", json=body)
    assert r.status_code == 200, r.text
    assert not _errors(r.json()), r.json()["issues"]
    short_of = dict(body, contracts="2")
    r = api_client.post("/api/input/manual/commit", json=short_of)
    assert r.status_code == 400, r.text           # an expiry closes the WHOLE position
    r = api_client.post("/api/input/manual/commit", json=body)
    assert r.status_code == 201, r.text
    (close,) = [t for t in list_transactions(golden_db, symbol=DST) if t.close_reason]
    assert (close.side, close.quantity, close.price) == (Side.BUY, D(300), D(0))


def test_the_source_contract_is_flat_after_the_adjustment(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    _adjusted_short(golden_db)
    r = api_client.post("/api/input/manual/commit",
                        json=_manual(symbol=SRC, contracts="1", option_intent="BTC"))
    assert r.status_code == 400, r.text
    assert r.json()["error"]["message"] == "無空頭部位可平倉，請改選買進開倉"


def test_option_side_crosses_the_adjustment_on_every_surface(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    sto = _adjusted_short(golden_db)
    btc = _trade(golden_db, DST, Side.BUY, "300", date(2026, 5, 4), price="0.40", fees="1.95")
    rows = _ledger(api_client)
    assert (rows[sto]["option_side"], rows[btc]["option_side"]) == ("賣出開倉", "買進平倉")
    assert (rows[sto]["contracts"], rows[btc]["contracts"]) == ("1", "3")
    lines = api_client.get("/api/cash/statement",
                           params={"account": "schwab", "ccy": "USD"}).json()["rows"]
    sides = sorted((ln["option_side"], ln["contracts"]) for ln in lines
                   if ln["option_side"] is not None)
    assert sides == sorted([("賣出開倉", "1"), ("買進平倉", "3")])


# --- a refused close on every surface ----------------------------------------------------------


def _refused(conn: sqlite3.Connection) -> tuple[int, int]:
    """A 2-contract short, then an expiry row of 3 — beyond the short lot: §4.4 refuses it."""
    register_option_contract(conn, SRC)
    sto = _trade(conn, SRC, Side.SELL, "200", date(2026, 3, 2), short=True)
    bad = _trade(conn, SRC, Side.BUY, "300", date(2026, 5, 4), close="expired")
    return sto, bad


def test_the_trades_tab_marks_a_refused_close(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    sto, bad = _refused(golden_db)
    rows = _ledger(api_client)
    assert (rows[bad]["refused_close"], rows[bad]["option_side"]) == (True, "未入帳（待釐清）")
    assert (rows[sto]["refused_close"], rows[sto]["option_side"]) == (False, "賣出開倉")


def test_the_dashboard_names_the_refused_close_and_withholds_xirr(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    _sto, bad = _refused(golden_db)
    dash = api_client.get("/api/dashboard").json()
    (rc,) = dash["options"]["refused_closes"]
    assert rc["txn_id"] == bad and rc["close_reason"] == "expired"
    assert rc["remedy"] == "刪除該筆到期作廢／被指派／履約列，或補回被刪除的交易"
    (row,) = dash["options"]["open"]
    assert row["unbookable_close"] is True
    assert dash["returns"]["xirr"] is None


def test_the_manual_preview_withholds_position_math_over_a_refused_close(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    _refused(golden_db)
    r = api_client.post("/api/input/manual/preview",
                        json=_manual(symbol=SRC, contracts="1", option_intent="BTC"))
    assert r.status_code == 200, r.text
    assert r.json()["position_preview"] is None


def test_the_classifier_agrees_with_the_replays_realized_rows(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """Every booked close row has a realized row with its reason; the refused one has none."""
    register_option_contract(golden_db, SRC)
    register_option_contract(golden_db, CALL)
    _trade(golden_db, SRC, Side.SELL, "200", date(2026, 3, 2), short=True)
    assigned = _trade(golden_db, SRC, Side.BUY, "100", date(2026, 4, 1), close="assigned")
    expired = _trade(golden_db, SRC, Side.BUY, "100", date(2026, 5, 4), close="expired")
    _trade(golden_db, CALL, Side.BUY, "100", date(2026, 3, 3))
    refused = _trade(golden_db, CALL, Side.SELL, "200", date(2026, 5, 5), close="exercised")
    rows = _ledger(api_client)
    assert [rows[i]["option_side"] for i in (assigned, expired, refused)] == [
        "被指派", "到期作廢", "未入帳（待釐清）"]
    book = build_dashboard(golden_db, now=GOLDEN_NOW, reporting=Currency.TWD)
    assert book.options is not None
    reasons = sorted(r.close_reason for r in book.options.realized.rows if r.close_reason)
    assert reasons == ["assigned", "expired"]
    assert [rc.txn_id for rc in book.options.refused_closes] == [refused]


# --- stock-only readers and the KPI identity --------------------------------------------------


def _options_ledger(conn: sqlite3.Connection) -> None:
    register_option_contract(conn, SRC)
    register_option_contract(conn, CALL)
    _trade(conn, SRC, Side.SELL, "100", date(2026, 2, 2), short=True, price="2.5")
    _trade(conn, CALL, Side.BUY, "200", date(2026, 2, 3), price="1.2", fees="1.3")


def test_dashboard_holdings_readers_see_no_option(golden_db: sqlite3.Connection) -> None:
    from portfolio_dash.api.insight_service import held_in_book

    _options_ledger(golden_db)
    data = build_dashboard(golden_db, now=GOLDEN_NOW, reporting=Currency.TWD)
    assert data.options is not None and len(data.options.open) == 2
    assert data.holdings and not [h for h in data.holdings if is_option_symbol(h.symbol)]
    assert not [s for s in held_in_book(data) if is_option_symbol(s)]
    assert sum(h.weight or D(0) for h in data.holdings) == D(1)


def test_kpi_market_value_is_the_stock_subtotal_plus_the_options(
    golden_db: sqlite3.Connection,
) -> None:
    upsert_instrument(golden_db, Instrument(symbol="XYZ", market=Market.US,
                                            quote_ccy=Currency.USD, sector="", name="XYZ"))
    _options_ledger(golden_db)
    data = build_dashboard(golden_db, now=GOLDEN_NOW, reporting=Currency.TWD)
    assert data.options is not None and data.options.market_value.reporting is not None
    grand = next(s for s in data.holdings_subtotals
                 if s.account_id is None and s.market is None)
    assert grand.total_market_value is not None
    assert data.kpis.total_market_value == (
        grand.total_market_value + data.options.market_value.reporting)
