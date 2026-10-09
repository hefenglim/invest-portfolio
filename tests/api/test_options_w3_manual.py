"""Options W3 — the manual door's option mode, the compose endpoint and the input wires
(spec §4.2, §6.2, §8, §10).

The golden ledger (``api_client``) has no option; every case seeds its own rows. Nothing here
replays an ``OPTION_ADJUST`` or a §4.4-refused close (W2's), so the cases stay inside what the
replay at the wave-1 base books with its existing arithmetic.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.register import register_option_contract
from portfolio_dash.data_ingestion.store import (
    get_instrument,
    insert_transaction,
    list_transactions,
    upsert_instrument,
)
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side

PUT = "XYZ261218P00050000"
CALL = "XYZ261218C00060000"


def _base(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"account_id": "schwab", "symbol": PUT, "date": "2026-06-01",
                            "price": "1.50"}
    body.update(over)
    return body


def _short(conn: sqlite3.Connection, qty: str = "200", symbol: str = PUT) -> None:
    register_option_contract(conn, symbol)
    insert_transaction(conn, account_id="schwab", symbol=symbol, side=Side.SELL,
                       quantity=Decimal(qty), price=Decimal("1.5"), fees=Decimal("1.31"),
                       tax=Decimal("0"), trade_date=date(2026, 5, 4), short_sale=True)


def _long(conn: sqlite3.Connection, qty: str = "100", symbol: str = CALL) -> None:
    register_option_contract(conn, symbol)
    insert_transaction(conn, account_id="schwab", symbol=symbol, side=Side.BUY,
                       quantity=Decimal(qty), price=Decimal("2"), fees=Decimal("0.65"),
                       tax=Decimal("0"), trade_date=date(2026, 5, 4))


def _xyz(conn: sqlite3.Connection) -> None:
    upsert_instrument(conn, Instrument(symbol="XYZ", market=Market.US, quote_ccy=Currency.USD,
                                       sector="Tech", name="XYZ Corp"))


# --- the compose endpoint ---------------------------------------------------------------------


def test_compose_returns_the_occ_symbol_and_name(api_client: TestClient) -> None:
    r = api_client.get("/api/input/option-symbol",
                       params={"underlying": "xyz", "expiry": "2026-12-18", "strike": "50",
                               "right": "P"})
    assert r.status_code == 200, r.text
    assert r.json() == {"symbol": PUT, "name": "XYZ 2026-12-18 50 Put"}
    r = api_client.get("/api/input/option-symbol",
                       params={"underlying": "XYZ", "expiry": "2026-12-18", "strike": "7.5",
                               "right": "買權"})
    assert r.json()["symbol"] == "XYZ261218C00007500"


@pytest.mark.parametrize(("params", "needle"), [
    ({"underlying": "2330", "expiry": "2026-12-18", "strike": "50", "right": "P"}, "美股"),
    ({"underlying": "XYZ", "expiry": "2026-12-18", "strike": "abc", "right": "P"}, "履約價"),
    ({"underlying": "XYZ", "expiry": "2026-12-18", "strike": "0.0001", "right": "P"}, "履約價"),
    ({"underlying": "XYZ", "expiry": "12/18/2026", "strike": "50", "right": "P"}, "到期日"),
    ({"underlying": "XYZ", "expiry": "2026-12-18", "strike": "50", "right": "Q"}, "買權"),
])
def test_compose_refuses_in_chinese(api_client: TestClient, params: dict[str, str],
                                    needle: str) -> None:
    r = api_client.get("/api/input/option-symbol", params=params)
    assert r.status_code == 400, r.text
    assert needle in r.json()["error"]["message"]


def test_compose_never_registers_anything(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    api_client.get("/api/input/option-symbol",
                   params={"underlying": "XYZ", "expiry": "2026-12-18", "strike": "50",
                           "right": "P"})
    assert get_instrument(golden_db, PUT) is None


# --- option-mode preview / commit -------------------------------------------------------------


def test_a_stock_preview_carries_the_new_keys_as_null(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/preview", json={
        "account_id": "schwab", "symbol": "AAPL", "side": "buy", "date": "2026-06-01",
        "shares": "1", "price": "100"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["share_equivalents"], body["contract_name"], body["legs"]) == (None, None, None)


def test_sell_to_open_previews_contracts_without_writing(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    r = api_client.post("/api/input/manual/preview",
                        json=_base(contracts="2", option_intent="STO"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["share_equivalents"] == "200"
    assert body["contract_name"] == "XYZ 2026-12-18 50 Put"
    assert body["fee"] == "1.31" and body["tax"] == "0"
    assert body["gross"] == "300.00" and body["total"] == "298.69"
    assert body["fee_rule_label"] == "每口 0.65＋SEC（賣出）"
    assert any(i["code"] == "option_contract_autoregister" and i["sev"] == "info"
               for i in body["issues"]), body["issues"]
    assert get_instrument(golden_db, PUT) is None


def test_sell_to_open_commit_registers_and_declares_the_short(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    r = api_client.post("/api/input/manual/commit",
                        json=_base(contracts="2", option_intent="STO"))
    assert r.status_code == 201, r.text
    (tx,) = list_transactions(golden_db, symbol=PUT)
    assert (tx.side, tx.quantity, tx.short_sale) == (Side.SELL, Decimal("200"), True)
    inst = get_instrument(golden_db, PUT)
    assert inst is not None and inst.is_option


@pytest.mark.parametrize(("intent", "seed", "contracts", "message"), [
    ("BTC", None, "1", "無空頭部位可平倉，請改選買進開倉"),
    ("BTC", "short", "3", "買進平倉口數超過空頭部位（2 口），請分成平倉與開倉兩筆"),
    ("STC", None, "1", "無多頭部位可平倉，請改選賣出開倉"),
    ("STC", "long", "2", "賣出平倉口數超過多頭部位（1 口），請分成平倉與開倉兩筆"),
    ("STO", "long", "1", "目前持有買方部位 1 口，請先賣出平倉"),
    ("BTO", "short", "1", "目前持有賣方部位 2 口，請先買進平倉"),
])
def test_option_mode_refusals(
    api_client: TestClient, golden_db: sqlite3.Connection, intent: str, seed: str | None,
    contracts: str, message: str,
) -> None:
    symbol = CALL if seed == "long" or intent == "STC" else PUT
    if seed == "short":
        _short(golden_db)
    elif seed == "long":
        _long(golden_db)
    else:
        register_option_contract(golden_db, symbol)
    r = api_client.post("/api/input/manual/commit",
                        json=_base(symbol=symbol, contracts=contracts, option_intent=intent))
    assert r.status_code == 400, r.text
    assert r.json()["error"]["message"] == message


def test_buy_to_close_within_the_short_is_clean(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    _short(golden_db)
    r = api_client.post("/api/input/manual/preview",
                        json=_base(contracts="2", option_intent="BTC", price="0.40"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert not [i for i in body["issues"] if i["sev"] == "error"], body["issues"]
    pp = body["position_preview"]
    assert pp is not None and pp["new_shares"] == "0"
    # (1.50 × 200 − 1.31 proceeds) − (0.40 × 200 + 1.30) = 298.69 − 81.30
    assert Decimal(pp["realized_pnl"]) == Decimal("217.39")


def test_contracts_on_a_stock_are_refused(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/preview", json={
        "account_id": "schwab", "symbol": "AAPL", "side": "buy", "date": "2026-06-01",
        "contracts": "1", "price": "100"})
    assert r.status_code == 400, r.text
    assert "選擇權合約" in r.json()["error"]["message"]


def test_an_unset_rate_needs_the_fee_override(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    r = api_client.post("/api/input/manual/commit",
                        json=_base(account_id="moomoo_my", contracts="1", option_intent="STO",
                                   market="US"))
    assert r.status_code == 400, r.text
    assert r.json()["error"]["message"].startswith("此帳戶未設定選擇權手續費")
    r = api_client.post("/api/input/manual/commit",
                        json=_base(account_id="moomoo_my", contracts="1", option_intent="STO",
                                   market="US", fee_override="1.99"))
    assert r.status_code == 201, r.text


# --- close rows -------------------------------------------------------------------------------


def test_expired_commits_a_free_close_row_of_the_whole_short(
    api_client: TestClient, golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _short(golden_db)
    body = _base(date="2026-12-18", close_reason="expired", contracts="2")
    body.pop("price")
    r = api_client.post("/api/input/manual/preview", json=body)
    assert r.status_code == 200, r.text
    pv = r.json()
    assert (pv["fee"], pv["tax"], pv["total"]) == ("0", "0", "0")
    assert pv["position_preview"] is not None and pv["position_preview"]["new_shares"] == "0"
    assert Decimal(pv["position_preview"]["realized_pnl"]) == Decimal("298.69")
    r = api_client.post("/api/input/manual/commit", json=body)
    assert r.status_code == 201, r.text
    closes = [t for t in list_transactions(golden_db, symbol=PUT) if t.close_reason]
    assert [(t.side, t.quantity, t.price, t.fees, t.fee_rule_snapshot) for t in closes] == [
        (Side.BUY, Decimal("200"), Decimal("0"), Decimal("0"), {"source": "close_reason"})]


def test_expired_with_a_fee_override_is_refused(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    _short(golden_db)
    r = api_client.post("/api/input/manual/commit", json=_base(
        date="2026-12-18", close_reason="expired", contracts="2", fee_override="0.65"))
    assert r.status_code == 400, r.text
    assert r.json()["error"]["message"] == "到期作廢／被指派／履約列不收手續費與稅"


# --- assignment / exercise: two legs, one transaction -----------------------------------------


def test_an_assigned_put_previews_both_legs(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    _xyz(golden_db)
    _short(golden_db)
    r = api_client.post("/api/input/manual/preview", json=_base(
        date="2026-12-18", close_reason="assigned", contracts="2"))
    assert r.status_code == 200, r.text
    legs = r.json()["legs"]
    assert [leg["role"] for leg in legs] == ["option", "stock"]
    assert [leg["label"] for leg in legs] == ["選擇權腿", "股票腿"]
    option, stock = legs
    assert (option["side"], option["shares"], option["contracts"], option["total"]) == (
        "buy", "200", "2", "0")
    assert (stock["symbol"], stock["side"], stock["shares"], stock["price"]) == (
        "XYZ", "buy", "200", "50")
    assert stock["text"] == "被指派：以 50.00 買入 200 股 XYZ，現金 −10,000.00 USD"
    assert list_transactions(golden_db, symbol="XYZ") == []


def test_an_assigned_put_commits_both_rows(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    _xyz(golden_db)
    _short(golden_db)
    r = api_client.post("/api/input/manual/commit", json=_base(
        date="2026-12-18", close_reason="assigned", contracts="2", stock_price="49.5"))
    assert r.status_code == 201, r.text
    body = r.json()
    ids = body["txn_ids"]
    assert len(ids) == 2 and all(isinstance(i, int) for i in ids)
    assert [leg["txn_id"] for leg in body["legs"]] == ids
    (stock,) = list_transactions(golden_db, symbol="XYZ")
    assert (stock.id, stock.side, stock.quantity, stock.price) == (
        ids[1], Side.BUY, Decimal("200"), Decimal("49.5"))
    closes = [t for t in list_transactions(golden_db, symbol=PUT) if t.close_reason]
    assert [(t.id, t.close_reason) for t in closes] == [(ids[0], "assigned")]


def test_an_exercised_call_sells_nothing_it_does_not_hold(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """A bought call exercised BUYS the stock; on the pair card that is the stock leg."""
    _xyz(golden_db)
    _long(golden_db, qty="100", symbol=CALL)
    r = api_client.post("/api/input/manual/preview", json=_base(
        symbol=CALL, date="2026-12-18", close_reason="exercised", contracts="1"))
    assert r.status_code == 200, r.text
    stock = r.json()["legs"][1]
    assert (stock["side"], stock["shares"], stock["price"]) == ("buy", "100", "60")
    assert stock["text"].startswith("履約：以 60.00 買入 100 股 XYZ")


def test_an_unregistered_underlying_refuses_the_pair(
    api_client: TestClient, golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from portfolio_dash.api.instrument_service import QuickRegisterError
    from portfolio_dash.api.routers import input_center

    def refuse(*_a: object, **_k: object) -> object:
        raise QuickRegisterError("quote_not_found", "查無報價", 422)

    monkeypatch.setattr(input_center, "quick_register", refuse)
    _short(golden_db)
    r = api_client.post("/api/input/manual/commit", json=_base(
        date="2026-12-18", close_reason="assigned", contracts="2"))
    assert r.status_code == 400, r.text
    err = r.json()["error"]
    assert (err["code"], err["message"]) == ("underlying_unregistered", "請先登錄標的 XYZ")
    assert [t for t in list_transactions(golden_db, symbol=PUT) if t.close_reason] == []


# --- the input wires ---------------------------------------------------------------------------


def test_input_holdings_lists_a_short_contract_as_held(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    _short(golden_db)
    body = api_client.get("/api/input/holdings", params={"account": "schwab"}).json()
    rows = {r["symbol"]: r for r in body["held"]}
    assert rows[PUT]["asset_type"] == "option"
    assert (rows[PUT]["direction"], rows[PUT]["contracts"]) == ("short", "2")
    assert (rows["AAPL"]["asset_type"], rows["AAPL"]["direction"],
            rows["AAPL"]["contracts"]) == ("stock", None, None)


def test_input_context_instruments_carry_asset_type(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    register_option_contract(golden_db, PUT)
    insts = {i["symbol"]: i for i in api_client.get("/api/input/context").json()["instruments"]}
    assert insts[PUT]["asset_type"] == "option"
    assert insts["AAPL"]["asset_type"] == "stock"


def test_the_csv_preview_data_carries_contracts_and_the_name(api_client: TestClient) -> None:
    from portfolio_dash.data_ingestion.csv_import import TRANSACTION_COLUMNS

    text = (",".join(TRANSACTION_COLUMNS) + "\n"
            f"schwab,{PUT},sell,2026-06-01,200,1.5,,,,1,,\n"
            "schwab,AAPL,buy,2026-06-01,1,100,,,,,,\n")
    r = api_client.post("/api/import/preview", json={"kind": "transactions", "csv_text": text})
    assert r.status_code == 200, r.text
    opt, stock = r.json()["rows"]
    assert (opt["data"]["contracts"], opt["data"]["contract_name"]) == (
        "2", "XYZ 2026-12-18 50 Put")
    assert (stock["data"]["contracts"], stock["data"]["contract_name"]) == (None, None)
