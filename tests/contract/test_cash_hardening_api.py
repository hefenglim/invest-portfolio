"""Contract regression tests: P3-batch3 Wave 2B cash hardening (audit findings).

Golden cash pools: schwab TWD −32,000 (fx-out), schwab USD 0, tw_broker TWD −495,000.
Each finding's confirmed probe is a named test here.
"""

from collections.abc import Callable
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import insert_cash_movement
from portfolio_dash.shared.enums import Currency


def _balance(api_client: TestClient, account_id: str, ccy: str) -> str | None:
    body = api_client.get("/api/cash").json()
    row = next((b for b in body["balances"]
                if b["account_id"] == account_id and b["ccy"] == ccy), None)
    return row["amount"] if row else None


# --- C2: currency ↔ account coherence --------------------------------------


def test_movement_ccy_not_allowed_rejected(api_client: TestClient) -> None:
    # tw_broker settles + funds in TWD only; a USD deposit is incoherent.
    r = api_client.post("/api/cash/movements", json={
        "account_id": "tw_broker", "date": "2026-01-01", "kind": "deposit",
        "ccy": "USD", "amount": "100"})
    assert r.status_code == 400 and "USD" in r.json()["error"]["message"]


def test_fx_leg_ccy_not_allowed_rejected(api_client: TestClient) -> None:
    # schwab holds USD (settle) + TWD (funding); MYR is not one of its currencies.
    r = api_client.post("/api/cash/fx", json={
        "account_id": "schwab", "date": "2026-01-01", "from_ccy": "MYR",
        "from_amt": "100", "to_ccy": "USD", "to_amt": "20"})
    assert r.status_code == 400 and "MYR" in r.json()["error"]["message"]


# --- C3: date-aware running-balance guard ----------------------------------


def test_backdated_withdraw_before_funding_hard_blocks(api_client: TestClient) -> None:
    # C3 semantics HARDENED by FU-D43a: a withdraw dated BEFORE its funding (end
    # aggregate +500, but the pool was −500 between 2026-04-01 and the deposit) is now a
    # hard 422 withdraw_insufficient_balance — the ack override is removed for
    # withdrawals, so the missed deposit must be recorded first.
    api_client.post("/api/cash/movements", json={
        "account_id": "moomoo_my_us", "date": "2026-05-01", "kind": "deposit",
        "ccy": "USD", "amount": "1000"})
    r = api_client.post("/api/cash/movements", json={
        "account_id": "moomoo_my_us", "date": "2026-04-01", "kind": "withdraw",
        "ccy": "USD", "amount": "500"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "withdraw_insufficient_balance"
    r2 = api_client.post("/api/cash/movements", json={
        "account_id": "moomoo_my_us", "date": "2026-04-01", "kind": "withdraw",
        "ccy": "USD", "amount": "500", "ack_negative": True})
    assert r2.status_code == 422  # ack no longer bypasses a withdraw guard
    assert r2.json()["error"]["code"] == "withdraw_insufficient_balance"


# --- C4: opening (期初資金) movement kind -----------------------------------


def test_opening_movement_credits_pool(api_client: TestClient) -> None:
    r = api_client.post("/api/cash/movements", json={
        "account_id": "moomoo_my_us", "date": "2026-01-01", "kind": "opening",
        "ccy": "USD", "amount": "1000"})
    assert r.status_code == 201
    assert _balance(api_client, "moomoo_my_us", "USD") == "1000"  # credited like a deposit
    rows = api_client.get("/api/cash").json()["movements"]["rows"]
    assert any(m["kind"] == "opening" for m in rows)


# --- C1a: negative-pool visibility -----------------------------------------


def test_negative_pools_listed(api_client: TestClient) -> None:
    body = api_client.get("/api/cash").json()
    negs = {(p["account_id"], p["ccy"]) for p in body["negative_pools"]}
    assert ("schwab", "TWD") in negs  # golden fx-out with no offsetting deposit


# --- C5: cash statement ----------------------------------------------------


def test_cash_statement_shape_and_running_balance(api_client: TestClient) -> None:
    api_client.post("/api/cash/movements", json={
        "account_id": "moomoo_my_us", "date": "2026-05-01", "kind": "deposit",
        "ccy": "USD", "amount": "1000"})
    api_client.post("/api/cash/movements", json={
        "account_id": "moomoo_my_us", "date": "2026-06-01", "kind": "withdraw",
        "ccy": "USD", "amount": "300"})
    body = api_client.get("/api/cash/statement",
                          params={"account": "moomoo_my_us", "ccy": "USD"}).json()
    assert body["account_id"] == "moomoo_my_us" and body["ccy"] == "USD"
    assert body["current_balance"] == "700" and body["total_count"] == 2
    # newest-first: withdraw (balance 700) then deposit (balance 1000)
    assert body["rows"][0]["kind"] == "withdraw" and body["rows"][0]["balance"] == "700"
    assert body["rows"][1]["kind"] == "deposit" and body["rows"][1]["balance"] == "1000"
    for key in ("date", "kind", "ref", "delta", "balance"):
        assert key in body["rows"][0]


def test_cash_statement_paging(api_client: TestClient) -> None:
    for i in range(1, 4):
        api_client.post("/api/cash/movements", json={
            "account_id": "moomoo_my_us", "date": f"2026-0{i}-01", "kind": "deposit",
            "ccy": "USD", "amount": "100"})
    p1 = api_client.get("/api/cash/statement",
                        params={"account": "moomoo_my_us", "ccy": "USD", "limit": 2}).json()
    assert p1["total_count"] == 3 and len(p1["rows"]) == 2


def test_cash_statement_unknown_account_404(api_client: TestClient) -> None:
    r = api_client.get("/api/cash/statement", params={"account": "ghost", "ccy": "USD"})
    assert r.status_code == 404


def test_cash_statement_account_only_is_combined_view(api_client: TestClient) -> None:
    """FU-D5: ccy is now OPTIONAL. Account-only returns the all-currency view (ccy null +
    a per-ccy balances list); the account param itself is still required (400 when absent)."""
    r = api_client.get("/api/cash/statement", params={"account": "schwab"})
    assert r.status_code == 200
    body = r.json()
    assert body["ccy"] is None and body["current_balance"] is None
    assert any(b["ccy"] == "USD" for b in body["balances"])
    # account is still mandatory
    assert api_client.get("/api/cash/statement", params={"ccy": "USD"}).status_code == 400


# --- C6: reporting total skips a pool with a missing FX rate ----------------


def test_reporting_total_skips_missing_rate(
    dashboard_client_factory: Callable[..., TestClient],
) -> None:
    def seed(conn) -> None:  # type: ignore[no-untyped-def]
        seed_accounts(conn)
        insert_cash_movement(conn, account_id="tw_broker", move_date=date(2026, 1, 1),
                             kind="DEPOSIT", ccy=Currency.TWD, amount=Decimal("1000"))
        # MYR pool with NO MYR/TWD rate stored anywhere -> must be skipped, not fatal.
        insert_cash_movement(conn, account_id="moomoo_my_my", move_date=date(2026, 1, 1),
                             kind="DEPOSIT", ccy=Currency.MYR, amount=Decimal("500"))

    client = dashboard_client_factory(seed, reporting=Currency.TWD)
    body = client.get("/api/cash").json()
    assert body["reporting_total"] is not None  # dust MYR pool no longer nulls the total
    assert body["reporting_total"] == "1000"    # the convertible TWD pool
    excluded = {e["ccy"] for e in body["reporting_total_excluded"]}
    assert "MYR" in excluded
    assert body["reporting_total_unavailable_reason"]  # annotated


# --- FU-D43c: fx-estimate degrade when no rate is stored ---------------------


def test_fx_estimate_no_stored_rate_degrades(
    dashboard_client_factory: Callable[..., TestClient],
) -> None:
    """No rate in EITHER direction -> {available: false, zh reason} — 200, never a guess
    (the golden DB stores every pair among its three currencies, so this uses a rate-free
    seed)."""
    client = dashboard_client_factory(seed_accounts)
    r = client.get("/api/cash/fx-estimate",
                   params={"from_ccy": "USD", "to_ccy": "TWD", "amount": "100"})
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False
    assert "USD/TWD" in body["reason"] and "匯率" in body["reason"]
    assert "estimate" not in body  # nothing fabricated
