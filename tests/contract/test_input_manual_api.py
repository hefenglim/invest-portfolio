import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.api.instrument_service import QuickRegisterError, QuickRegisterOutcome
from portfolio_dash.api.routers import input_center
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_dividend,
    insert_transaction,
    upsert_instrument,
)
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import DashboardClientFactory


def test_manual_preview_buy_computes_fee_and_total(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-06-11", "shares": "1000", "price": "612.5"})
    assert r.status_code == 200
    b = r.json()
    # fee-engine v2 (FE-D3): 612,500 × 0.1425% = 872.8125 -> floor 872 (was 873 under HALF_UP).
    assert b["fee"] == "872" and b["tax"] == "0"
    # Full source precision stays on the wire (canonical decimal_str, #2c/M1): 1000 * 612.5
    # is Decimal("612500.0") -- the trailing zero is preserved (the old _money_str
    # normalize() dropped it). The frontend quantizes for display.
    assert b["gross"] == "612500.0" and b["total"] == "-613372.0"
    assert b["fee_overridden"] is False and b["issues"] == []
    # FE-D1 forecast hint (不計入成本): TW rebate = floor(fee × 0.77) = floor(872×0.77)=671.
    assert b["rebate_estimate"] == "671"


def test_manual_preview_rebate_estimate_null_for_non_tw(api_client: TestClient) -> None:
    """A US account (schwab, rebate_rate 0) returns rebate_estimate null (never applies)."""
    r = api_client.post("/api/input/manual/preview", json={
        "account_id": "schwab", "symbol": "AAPL", "side": "buy",
        "date": "2026-06-11", "shares": "10", "price": "100"})
    assert r.status_code == 200
    assert r.json()["rebate_estimate"] is None


def test_manual_preview_oversell_soft_issue(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "sell",
        "date": "2026-06-11", "shares": "5000", "price": "600"})
    b = r.json()
    codes = {i["code"]: i for i in b["issues"]}
    assert "sell_exceeds_holdings" in codes
    assert codes["sell_exceeds_holdings"]["sev"] == "warn"
    assert codes["sell_exceeds_holdings"]["field"] == "shares"


def test_manual_preview_fee_override(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-06-11", "shares": "1000", "price": "612.5",
        "fee_override": "500"})
    b = r.json()
    assert b["fee"] == "500" and b["fee_overridden"] is True


def test_manual_commit_writes(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/commit", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-06-11", "shares": "100", "price": "600"})
    assert r.status_code == 201
    b = r.json()
    assert isinstance(b["txn_id"], int) and b["total"].startswith("-")
    lg = api_client.get("/api/ledgers/transactions", params={"account_id": "tw_broker"}).json()
    assert lg["total_count"] == 2  # golden's 1 tw_broker txn + this one


def test_manual_commit_oversell_unacked_422(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/commit", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "sell",
        "date": "2026-06-11", "shares": "5000", "price": "600", "ack_oversell": False})
    assert r.status_code == 422 and r.json()["error"]["code"] == "oversell_unacknowledged"


def test_manual_commit_oversell_acked_writes(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/commit", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "sell",
        "date": "2026-06-11", "shares": "5000", "price": "600", "ack_oversell": True})
    assert r.status_code == 201


def test_manual_commit_hard_error_400(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/commit", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-06-11", "shares": "0", "price": "600"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "validation_error"


# --- C1b: overdraft soft issue only once the account tracks cash --------------


def test_manual_preview_overdraft_issue_when_tracked(api_client: TestClient) -> None:
    """LOW-4c: with ≥1 cash movement on the account, a BUY beyond the pool surfaces the
    soft cash_overdraft issue (the golden tw_broker TWD pool is already negative from its
    500k trade settlement, so any tracked buy overdraws)."""
    dep = api_client.post("/api/cash/movements", json={
        "account_id": "tw_broker", "date": "2026-01-01", "kind": "deposit",
        "ccy": "TWD", "amount": "1000"})
    assert dep.status_code == 201, dep.text
    r = api_client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-06-11", "shares": "1000", "price": "600"})
    codes = {i["code"] for i in r.json()["issues"]}
    assert "cash_overdraft" in codes


def test_manual_preview_no_overdraft_when_untracked(api_client: TestClient) -> None:
    """No cash movement on the account -> the overdraft check never fires (even on a big
    buy) — users who do not track cash are never warned."""
    r = api_client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-06-11", "shares": "1000", "price": "600"})
    codes = {i["code"] for i in r.json()["issues"]}
    assert "cash_overdraft" not in codes


# --- unknown symbol: auto-register on commit (2026-07-02, round 2) ------------
# The data_ingestion hard block stands (a ledger row must always resolve to an
# Instrument), but the manual COMMIT path now resolves it ITSELF: it infers the
# market from the account's settlement ccy and runs the one-step quick_register
# (which requires a REAL quote — the typo guard). Preview shows an info-severity
# note (never gates the button); an unregistrable symbol still commits nothing.


def test_manual_preview_unregistered_symbol_is_info(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "GHOST", "side": "buy",
        "date": "2026-06-11", "shares": "100", "price": "10"})
    assert r.status_code == 200
    codes = {i["code"]: i for i in r.json()["issues"]}
    assert "symbol_auto_register" in codes
    assert codes["symbol_auto_register"]["sev"] == "info"  # notice, not a gate
    assert "自動查詢並註冊" in codes["symbol_auto_register"]["text"]


def _fake_quick_register_ok(symbol: str = "GHOST", name: str = "Ghost Corp") -> Any:
    def fake(conn: Any, **kw: Any) -> QuickRegisterOutcome:
        from portfolio_dash.data_ingestion.store import upsert_instrument
        from portfolio_dash.shared.enums import Currency, Market

        inst = Instrument(symbol=symbol, market=Market.TW, quote_ccy=Currency.TWD,
                          sector="", name=name, board="TWSE")
        upsert_instrument(conn, inst)
        return QuickRegisterOutcome(instrument=inst, board="TWSE", last=None,
                                    name_source="provider", history_points=True)
    return fake


def test_manual_commit_auto_registers_unknown_symbol(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(input_center, "quick_register", _fake_quick_register_ok())
    r = api_client.post("/api/input/manual/commit", json={
        "account_id": "tw_broker", "symbol": "GHOST", "side": "buy",
        "date": "2026-06-11", "shares": "100", "price": "10"})
    assert r.status_code == 201
    b = r.json()
    assert b["auto_registered"]["symbol"] == "GHOST"
    assert b["auto_registered"]["name"] == "Ghost Corp"
    # The trade itself landed in the ledger.
    lg = api_client.get("/api/ledgers/transactions", params={"account_id": "tw_broker"}).json()
    assert any(t["symbol"] == "GHOST" for t in lg["rows"])


def test_manual_commit_auto_register_fails_when_no_quote(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake(conn: Any, **kw: Any) -> QuickRegisterOutcome:
        raise QuickRegisterError("quote_not_found", "查無 GHOST 的報價", 422)

    monkeypatch.setattr(input_center, "quick_register", fake)
    r = api_client.post("/api/input/manual/commit", json={
        "account_id": "tw_broker", "symbol": "GHOST", "side": "buy",
        "date": "2026-06-11", "shares": "100", "price": "10"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "symbol_auto_register_failed"
    assert "查無" in r.json()["error"]["message"]
    # Nothing was written.
    lg = api_client.get("/api/ledgers/transactions", params={"account_id": "tw_broker"}).json()
    assert all(t["symbol"] != "GHOST" for t in lg["rows"])


def test_manual_commit_registered_symbol_skips_auto_register(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(conn: Any, **kw: Any) -> QuickRegisterOutcome:
        raise AssertionError("quick_register must not run for a registered symbol")

    monkeypatch.setattr(input_center, "quick_register", boom)
    r = api_client.post("/api/input/manual/commit", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-06-11", "shares": "100", "price": "600"})
    assert r.status_code == 201
    assert r.json()["auto_registered"] is None


def test_manual_preview_etf_sell_uses_etf_tax_rate(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """Stress-audit finding (2026-07-15): the registry's is_etf flag must reach the
    fee engine on the manual path — an ETF sell is taxed 0.1%, never the 現股 0.3%."""
    upsert_instrument(
        golden_db,
        Instrument(symbol="0050", market=Market.TW, quote_ccy=Currency.TWD,
                   sector="ETF", name="元大台灣50", is_etf=True),
    )
    r = api_client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "0050", "side": "sell",
        "date": "2026-06-11", "shares": "50", "price": "140"})
    assert r.status_code == 200
    # notional 7,000 -> tax 7 (0.1%); the pre-fix bug returned 21 (0.3%).
    assert r.json()["tax"] == "7"


def test_manual_preview_daytrade_uses_daytrade_tax_rate(api_client: TestClient) -> None:
    r = api_client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "sell",
        "date": "2026-06-11", "shares": "100", "price": "600", "daytrade": True})
    assert r.status_code == 200
    # notional 60,000 -> tax 90 (0.15%); 現股 would be 180.
    assert r.json()["tax"] == "90"


# ============================================================================
# R6-E: 草稿預覽 position what-if (position_preview) + account-cash line
# ============================================================================
# The draft preview now carries the SAME information the per-holding drawer's 試算
# shows — but SERVER-computed as Decimal strings (the frontend renders only): SELL →
# 調整成本移除 / 已實現損益 / 剩餘股數; BUY → 新持股 / 新原始均價 / 新調整均價. Plus a
# DISPLAY-ONLY account-cash line. Both fields are additive + null-on-degradation; the
# realized preview is a MIRROR of the ledger's own sell math (asserted below), never a
# second source of record.


def _seed_2330_div_adjusted(conn: sqlite3.Connection) -> None:
    """The r5 holdings pattern: 2330 held 1,000 sh with a CASH dividend so the adjusted
    average is DIVIDEND-ADJUSTED — adjusted_total = 1000×500 − 2,500 = 497,500 →
    adjusted_avg 497.5 (a naive original-cost average would read 500). Pins that the
    preview reuses the REAL build_book cost basis, not original cost."""
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Semiconductors", name="TSMC", board="TWSE"))
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=Decimal("1000"), price=Decimal("500"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 5))
    insert_dividend(conn, account_id="tw_broker", symbol="2330", div_date=date(2026, 3, 5),
                    div_type="CASH", gross=Decimal("2500"), withholding=Decimal("0"),
                    net=Decimal("2500"))
    conn.commit()


def test_position_preview_sell_uses_dividend_adjusted_cost(
    dashboard_client_factory: DashboardClientFactory
) -> None:
    """SELL on a dividend-adjusted holding: cost_removed = adjusted_total × (qty/held),
    realized = (gross − fee − tax) − cost_removed, remain = held − qty — EXACT strings.
    adjusted_total = 497,500; sell 500 @ 600 → cost_removed 497,500 × 0.5 = 248,750,
    remain 500; fee 300,000 × 0.1425% floor = 427, tax 0.3% floor = 900 →
    realized = (300,000 − 427 − 900) − 248,750 = 49,923."""
    client: TestClient = dashboard_client_factory(_seed_2330_div_adjusted)
    r = client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "sell",
        "date": "2026-06-11", "shares": "500", "price": "600"})
    assert r.status_code == 200
    pp = r.json()["position_preview"]
    assert pp["kind"] == "sell"
    # Trailing .0 is the ledger's own scale (497,500 × 0.5) — kept so the preview is
    # byte-identical to the booked realized row (see the matching cross-check test).
    assert pp["cost_removed"] == "248750.0"
    assert pp["realized_pnl"] == "49923.0"
    assert pp["remain_shares"] == "500"


def test_position_preview_sell_realized_matches_booked_row(
    dashboard_client_factory: DashboardClientFactory
) -> None:
    """No double counting: the preview realized_pnl / cost_removed are a bit-for-bit MIRROR
    of what an actual booked sell produces via the realized-P&L seam (/api/symbol detail
    realized_rows). Same qty/price → identical adjusted_cost_removed + realized."""
    client: TestClient = dashboard_client_factory(_seed_2330_div_adjusted)
    body = {"account_id": "tw_broker", "symbol": "2330", "side": "sell",
            "date": "2026-06-11", "shares": "500", "price": "600"}
    pp = client.post("/api/input/manual/preview", json=body).json()["position_preview"]
    committed = client.post("/api/input/manual/commit", json=body)
    assert committed.status_code == 201, committed.text
    detail = client.get("/api/symbol/2330/detail").json()
    rows = [row for row in detail["realized_rows"] if row["account_id"] == "tw_broker"]
    assert len(rows) == 1
    assert rows[0]["adjusted_cost_removed"] == pp["cost_removed"]
    assert rows[0]["realized"] == pp["realized_pnl"]


def test_position_preview_sell_not_held_is_null(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """A registered but NOT-held symbol on the sell side → position_preview null (no basis
    to remove); the account-cash line still resolves from the quote ccy."""
    upsert_instrument(golden_db, Instrument(
        symbol="2454", market=Market.TW, quote_ccy=Currency.TWD,
        sector="Semiconductors", name="MediaTek", board="TWSE"))
    r = api_client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "2454", "side": "sell",
        "date": "2026-06-11", "shares": "100", "price": "600"})
    assert r.status_code == 200
    b = r.json()
    assert b["position_preview"] is None
    assert b["account_cash"] == {"ccy": "TWD", "balance": "-495000"}


def test_position_preview_sell_oversell_remain_floors_at_zero(
    api_client: TestClient
) -> None:
    """Oversell (qty > held) still renders honestly — remain_shares floors at 0 and the
    pre-existing soft 賣超 issue is unchanged. cost_removed = 495,000 × (5000/1000) =
    2,475,000 (the golden 2330 adjusted_total is 500,000 − 5,000 div = 495,000)."""
    r = api_client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "sell",
        "date": "2026-06-11", "shares": "5000", "price": "600"})
    assert r.status_code == 200
    b = r.json()
    assert b["position_preview"]["kind"] == "sell"
    assert b["position_preview"]["cost_removed"] == "2475000"
    assert b["position_preview"]["remain_shares"] == "0"
    assert "sell_exceeds_holdings" in {i["code"] for i in b["issues"]}


def test_position_preview_buy_new_averages_from_held_totals(
    api_client: TestClient
) -> None:
    """BUY into a held position: new averages come from held TOTALS + this trade's all-in
    cost. golden 2330 held 1,000 (original 500,000 / adjusted 495,000); buy 1,000 @ 600 →
    gross 600,000, fee 600,000×0.1425% floor 855, tax 0 → all-in 600,855; new_shares 2,000;
    new_original_avg = 1,100,855 / 2,000 = 550.4275; new_adjusted_avg = 1,095,855 / 2,000 =
    547.9275."""
    r = api_client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-06-11", "shares": "1000", "price": "600"})
    assert r.status_code == 200
    pp = r.json()["position_preview"]
    assert pp["kind"] == "buy"
    assert pp["new_shares"] == "2000"
    assert pp["new_original_avg"] == "550.4275"
    assert pp["new_adjusted_avg"] == "547.9275"


def test_position_preview_buy_fresh_position(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """BUY a registered-but-unheld symbol → fresh-position math (held = 0): both new averages
    equal the all-in cost / qty. 2454 buy 1,000 @ 100 → gross 100,000, fee 142, all-in
    100,142; new_shares 1,000; both averages 100,142 / 1,000 = 100.142."""
    upsert_instrument(golden_db, Instrument(
        symbol="2454", market=Market.TW, quote_ccy=Currency.TWD,
        sector="Semiconductors", name="MediaTek", board="TWSE"))
    r = api_client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "2454", "side": "buy",
        "date": "2026-06-11", "shares": "1000", "price": "100"})
    assert r.status_code == 200
    pp = r.json()["position_preview"]
    assert pp["kind"] == "buy"
    assert pp["new_shares"] == "1000"
    assert pp["new_original_avg"] == "100.142"
    assert pp["new_adjusted_avg"] == "100.142"


def test_position_preview_and_cash_null_for_unregistered_symbol(
    api_client: TestClient
) -> None:
    """Unregistered symbol (EXACT-only resolution) → position_preview null AND account_cash
    null; the pre-existing auto-register info issue is unchanged (base preview never fails)."""
    r = api_client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "GHOST", "side": "buy",
        "date": "2026-06-11", "shares": "100", "price": "10"})
    assert r.status_code == 200
    b = r.json()
    assert b["position_preview"] is None
    assert b["account_cash"] is None
    assert "symbol_auto_register" in {i["code"] for i in b["issues"]}


def test_position_preview_null_when_inputs_incomplete(api_client: TestClient) -> None:
    """Incomplete inputs (shares 0) → position_preview null (guards the fresh-buy zero
    divisor); the base fee/tax preview still returns 200."""
    r = api_client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-06-11", "shares": "0", "price": "600"})
    assert r.status_code == 200
    assert r.json()["position_preview"] is None


def test_position_preview_null_when_ledger_unbookable(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """Never-500 at the build_book seam: an orphan dividend (no prior position) makes the
    whole ledger un-bookable — position_preview hides entirely (not mis-read as a fresh
    portfolio), while account_cash still resolves (cash balances do not depend on the book)."""
    upsert_instrument(golden_db, Instrument(
        symbol="GLD", market=Market.US, quote_ccy=Currency.USD, sector="", name="Gold"))
    insert_dividend(golden_db, account_id="schwab", symbol="GLD", div_date=date(2025, 1, 1),
                    div_type="CASH", gross=Decimal("1"), withholding=Decimal("0"),
                    net=Decimal("1"))
    golden_db.commit()
    r = api_client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-06-11", "shares": "100", "price": "600"})
    assert r.status_code == 200
    b = r.json()
    assert b["position_preview"] is None
    assert b["account_cash"] == {"ccy": "TWD", "balance": "-495000"}


def test_account_cash_matches_cash_balances_endpoint(api_client: TestClient) -> None:
    """account_cash serves the SAME figure /api/cash reports for the (account, ccy) pool —
    the draft line and the 資金 page can never disagree."""
    prev = api_client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-06-11", "shares": "100", "price": "600"}).json()["account_cash"]
    assert prev["ccy"] == "TWD"
    cash = api_client.get("/api/cash").json()
    pool = next(row for row in cash["balances"]
                if row["account_id"] == "tw_broker" and row["ccy"] == "TWD")
    assert prev["balance"] == pool["amount"]


def test_manual_preview_additive_contract_pinned_case(api_client: TestClient) -> None:
    """The R6-E + R7 fields are ADDITIVE: every pre-existing preview field stays byte-identical
    to the pinned buy case (see test_manual_preview_buy_computes_fee_and_total), with the new
    position_preview + account_cash + cash_after keys present alongside."""
    b = api_client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-06-11", "shares": "1000", "price": "612.5"}).json()
    # pre-existing fields — unchanged.
    assert b["fee"] == "872" and b["tax"] == "0"
    assert b["gross"] == "612500.0" and b["total"] == "-613372.0"
    assert b["fee_overridden"] is False and b["tax_overridden"] is False
    assert b["rebate_estimate"] == "671" and b["issues"] == []
    # account_cash stays the byte-identical {ccy, balance} pair (cash_after is a SIBLING top-
    # level key, so this exact-dict pin holds).
    assert b["account_cash"] == {"ccy": "TWD", "balance": "-495000"}
    # new additive fields — present.
    assert b["position_preview"]["kind"] == "buy"
    assert b["cash_after"] is not None


# ============================================================================
# R7 A3: 扣款後現金 (cash_after) — projected pool after settlement
# ============================================================================
# cash_after = the account-cash balance + the ALREADY-SIGNED total (BUY negative /
# SELL positive), in the SAME quote ccy as account_cash. Emitted only when the balance
# is known (else null). A pure Decimal add over figures the response already carries — no
# new engine call, no float.


def test_cash_after_buy_is_balance_plus_signed_total(api_client: TestClient) -> None:
    """BUY: cash_after pushes the (already-negative golden) pool further negative by the
    all-in cost. balance −495,000 + total −613,372.0 = −1,108,372.0 (exact string)."""
    b = api_client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-06-11", "shares": "1000", "price": "612.5"}).json()
    assert b["cash_after"] == "-1108372.0"
    # cash_after is exactly balance + total (the additive contract) — scale-proof derivation.
    assert b["cash_after"] == format(
        Decimal(b["account_cash"]["balance"]) + Decimal(b["total"]), "f")


def test_cash_after_sell_adds_proceeds(api_client: TestClient) -> None:
    """SELL: proceeds add back. balance −495,000 + total (300,000 − 427 fee − 900 tax =
    298,673) = −196,327 (exact string)."""
    s = api_client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "sell",
        "date": "2026-06-11", "shares": "500", "price": "600"}).json()
    assert s["cash_after"] == "-196327"
    assert s["cash_after"] == format(
        Decimal(s["account_cash"]["balance"]) + Decimal(s["total"]), "f")


def test_cash_after_null_when_balance_unknown(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """account_cash present but balance null (moomoo_my_my MYR pool has no golden activity)
    → cash_after null too (nothing to project from). Same dynamic ccy label (MYR)."""
    upsert_instrument(golden_db, Instrument(
        symbol="1155.KL", market=Market.MY, quote_ccy=Currency.MYR,
        sector="Financials", name="Maybank"))
    r = api_client.post("/api/input/manual/preview", json={
        "account_id": "moomoo_my_my", "symbol": "1155.KL", "side": "buy",
        "date": "2026-06-11", "shares": "100", "price": "9"}).json()
    assert r["account_cash"] == {"ccy": "MYR", "balance": None}
    assert r["cash_after"] is None


def test_cash_after_null_for_unregistered_symbol(api_client: TestClient) -> None:
    """Unregistered symbol → account_cash null (no quote ccy) → cash_after null."""
    r = api_client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "GHOST", "side": "buy",
        "date": "2026-06-11", "shares": "100", "price": "10"}).json()
    assert r["account_cash"] is None
    assert r["cash_after"] is None


# ============================================================================
# R7 A4: OLD-vs-NEW position_preview triple (old_shares / old_original_avg / old_adjusted_avg)
# ============================================================================
# Additive to position_preview: the PRE-trade position, so the draft renders 持股/原始均價/
# 調整均價 old→new. Averages computed from totals on read (never a stored rounded average);
# null for a fresh position. Existing new_* fields stay byte-identical.


def test_position_preview_old_fields_buy_dividend_adjusted(
    dashboard_client_factory: DashboardClientFactory
) -> None:
    """BUY into a dividend-adjusted holding: the OLD triple reflects the REAL build_book basis —
    old_original_avg (500,000/1,000 = 500) ≠ old_adjusted_avg (497,500/1,000 = 497.5). The
    new_* fields are unchanged (additive)."""
    client: TestClient = dashboard_client_factory(_seed_2330_div_adjusted)
    pp = client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-06-11", "shares": "500", "price": "600"}).json()["position_preview"]
    assert pp["kind"] == "buy"
    assert Decimal(pp["old_shares"]) == Decimal("1000")
    assert Decimal(pp["old_original_avg"]) == Decimal("500")
    assert Decimal(pp["old_adjusted_avg"]) == Decimal("497.5")
    # the dividend split is visible in the OLD triple (original ≠ adjusted).
    assert Decimal(pp["old_original_avg"]) != Decimal(pp["old_adjusted_avg"])


def test_position_preview_old_fields_sell_dividend_adjusted(
    dashboard_client_factory: DashboardClientFactory
) -> None:
    """SELL also carries the OLD triple; the sell branch emits no new_*_avg (avg unchanged),
    so the frontend renders old==new for the average pair (Senior Review #10)."""
    client: TestClient = dashboard_client_factory(_seed_2330_div_adjusted)
    pp = client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "sell",
        "date": "2026-06-11", "shares": "500", "price": "600"}).json()["position_preview"]
    assert pp["kind"] == "sell"
    assert Decimal(pp["old_shares"]) == Decimal("1000")
    assert Decimal(pp["old_original_avg"]) == Decimal("500")
    assert Decimal(pp["old_adjusted_avg"]) == Decimal("497.5")


def test_position_preview_old_fields_null_for_fresh_position(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """A fresh (registered but unheld) BUY → old_* null; the new_* fields still compute the
    fresh-position math."""
    upsert_instrument(golden_db, Instrument(
        symbol="2454", market=Market.TW, quote_ccy=Currency.TWD,
        sector="Semiconductors", name="MediaTek", board="TWSE"))
    pp = api_client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "2454", "side": "buy",
        "date": "2026-06-11", "shares": "1000", "price": "100"}).json()["position_preview"]
    assert pp["kind"] == "buy"
    assert pp["old_shares"] is None
    assert pp["old_original_avg"] is None
    assert pp["old_adjusted_avg"] is None
    # new_* fresh-position math unchanged (fee 142 → all-in 100,142 / 1,000 = 100.142).
    assert pp["new_original_avg"] == "100.142"
