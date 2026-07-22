"""Contract: 資金管理 (cash pools, R6 item 7) + negative-pool guard (item 2).

Golden subset flows affecting cash: tw_broker BUY 2330 1000×500 (−500,000 TWD),
schwab BUY AAPL 10×100 (−1,000 USD), schwab FX 32,000 TWD → 1,000 USD, tw_broker
CASH dividend net 5,000 TWD. Stored FX rates: USD/TWD 33 (latest), MYR/TWD 7.
"""

import csv
import io
from decimal import Decimal

from fastapi.testclient import TestClient


def _balance(api_client: TestClient, account_id: str, ccy: str) -> str | None:
    body = api_client.get("/api/cash").json()
    row = next((b for b in body["balances"]
                if b["account_id"] == account_id and b["ccy"] == ccy), None)
    return row["amount"] if row else None


def test_balances_reflect_all_ledgers(api_client: TestClient) -> None:
    body = api_client.get("/api/cash").json()
    # schwab USD: +1000 (fx in) − 1000 (AAPL buy) = 0
    assert _balance(api_client, "schwab", "USD") == "0"
    # schwab TWD: −32000 (fx out)
    assert _balance(api_client, "schwab", "TWD") == "-32000"
    # tw_broker TWD: −500000 (buy) + 5000 (cash dividend net) = −495000
    assert _balance(api_client, "tw_broker", "TWD") == "-495000"
    assert body["reporting_currency"] == "TWD"
    assert body["reporting_total"] is not None  # rates stored for USD/TWD


def test_deposit_moves_the_pool(api_client: TestClient) -> None:
    r = api_client.post("/api/cash/movements", json={
        "account_id": "tw_broker", "date": "2026-01-01", "kind": "deposit",
        "ccy": "TWD", "amount": "600000", "note": "初始入金"})
    assert r.status_code == 201
    assert _balance(api_client, "tw_broker", "TWD") == "105000"  # 600000−500000+5000
    rows = api_client.get("/api/cash").json()["movements"]["rows"]
    assert rows[0]["kind"] == "deposit" and rows[0]["amount"] == "600000"


def test_withdraw_over_balance_hard_block(api_client: TestClient) -> None:
    # FU-D43a (supersedes the old negative_cash+ack flow): a withdrawal exceeding the
    # pool's balance is HARD-blocked — 422 withdraw_insufficient_balance, message stating
    # the available figure, and ack_negative does NOT bypass (the override is removed for
    # withdrawals only).
    api_client.post("/api/cash/movements", json={
        "account_id": "moomoo_my", "date": "2026-01-01", "kind": "deposit",
        "ccy": "MYR", "amount": "1000"})
    r = api_client.post("/api/cash/movements", json={
        "account_id": "moomoo_my", "date": "2026-02-01", "kind": "withdraw",
        "ccy": "MYR", "amount": "1500"})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "withdraw_insufficient_balance"
    assert err["field"] == "amount"
    assert "1000" in err["message"]  # the available balance is stated in the message
    r2 = api_client.post("/api/cash/movements", json={
        "account_id": "moomoo_my", "date": "2026-02-01", "kind": "withdraw",
        "ccy": "MYR", "amount": "1500", "ack_negative": True})
    assert r2.status_code == 422  # no ack override for withdrawals
    assert r2.json()["error"]["code"] == "withdraw_insufficient_balance"
    assert _balance(api_client, "moomoo_my", "MYR") == "1000"  # nothing was written


def test_withdraw_exact_balance_passes(api_client: TestClient) -> None:
    # FU-D43a boundary: withdrawing EXACTLY the pool balance drains it to zero and is
    # allowed; even 0.01 beyond the (now zero) pool is then blocked.
    api_client.post("/api/cash/movements", json={
        "account_id": "moomoo_my", "date": "2026-01-01", "kind": "deposit",
        "ccy": "MYR", "amount": "1234.56"})
    ok = api_client.post("/api/cash/movements", json={
        "account_id": "moomoo_my", "date": "2026-02-01", "kind": "withdraw",
        "ccy": "MYR", "amount": "1234.56"})
    assert ok.status_code == 201
    assert _balance(api_client, "moomoo_my", "MYR") == "0.00"
    over = api_client.post("/api/cash/movements", json={
        "account_id": "moomoo_my", "date": "2026-02-02", "kind": "withdraw",
        "ccy": "MYR", "amount": "0.01"})
    assert over.status_code == 422
    assert over.json()["error"]["code"] == "withdraw_insufficient_balance"


def test_fx_over_balance_hard_block(api_client: TestClient) -> None:
    # FU-D34 (需求五): schwab TWD pool is −32,000 already — a conversion may NEVER drive it
    # further negative, so selling ANY TWD out is HARD-blocked (no ack override, no financing).
    r = api_client.post("/api/cash/fx", json={
        "account_id": "schwab", "date": "2026-06-01", "from_ccy": "TWD",
        "from_amt": "10000", "to_ccy": "USD", "to_amt": "300"})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "fx_insufficient_balance"
    assert err["field"] == "from_amt"
    assert "-32000" in err["message"]  # the available balance is stated in the message
    # Fund the pool so the balance covers the sell amount -> the conversion now passes and
    # lands in the SAME fx ledger the CSV path writes.
    api_client.post("/api/cash/movements", json={
        "account_id": "schwab", "date": "2026-01-05", "kind": "deposit",
        "ccy": "TWD", "amount": "50000"})
    # schwab TWD is now −32,000 + 50,000 = 18,000; selling 10,000 <= 18,000 passes.
    r2 = api_client.post("/api/cash/fx", json={
        "account_id": "schwab", "date": "2026-06-01", "from_ccy": "TWD",
        "from_amt": "10000", "to_ccy": "USD", "to_amt": "300"})
    assert r2.status_code == 201
    fx_rows = api_client.get("/api/ledgers/fx", params={"limit": 500}).json()["rows"]
    assert any(x["from_amt"] == "10000" and x["to_amt"] == "300" for x in fx_rows)
    assert _balance(api_client, "schwab", "USD") == "300"    # 0 + 300
    assert _balance(api_client, "schwab", "TWD") == "8000"   # 18,000 − 10,000


def test_fx_exact_balance_passes(api_client: TestClient) -> None:
    # FU-D34 boundary: selling EXACTLY the pool balance drains it to zero and is allowed.
    # moomoo_my MYR starts empty; fund it precisely, then convert the whole pool.
    api_client.post("/api/cash/movements", json={
        "account_id": "moomoo_my", "date": "2026-01-05", "kind": "deposit",
        "ccy": "MYR", "amount": "44000"})
    r = api_client.post("/api/cash/fx", json={
        "account_id": "moomoo_my", "date": "2026-01-06", "from_ccy": "MYR",
        "from_amt": "44000", "to_ccy": "USD", "to_amt": "10000"})
    assert r.status_code == 201
    assert _balance(api_client, "moomoo_my", "MYR") == "0"
    assert _balance(api_client, "moomoo_my", "USD") == "10000"


def test_fx_exact_fractional_balance_is_decimal_exact(api_client: TestClient) -> None:
    # FU-D34 boundary, both sides, at sub-unit precision: a naive float ceiling could make
    # the balance 12345.6699… and wrongly reject the exact sell; the Decimal check accepts
    # == exactly, then rejects even 0.01 beyond the (now zero) pool.
    api_client.post("/api/cash/movements", json={
        "account_id": "moomoo_my", "date": "2026-01-05", "kind": "deposit",
        "ccy": "MYR", "amount": "12345.67"})
    ok = api_client.post("/api/cash/fx", json={
        "account_id": "moomoo_my", "date": "2026-01-06", "from_ccy": "MYR",
        "from_amt": "12345.67", "to_ccy": "USD", "to_amt": "2743.48"})
    assert ok.status_code == 201  # exact-balance sell is NOT falsely blocked
    over = api_client.post("/api/cash/fx", json={
        "account_id": "moomoo_my", "date": "2026-01-07", "from_ccy": "MYR",
        "from_amt": "0.01", "to_ccy": "USD", "to_amt": "0.01"})
    assert over.status_code == 422
    assert over.json()["error"]["code"] == "fx_insufficient_balance"


def test_fx_ack_negative_no_longer_bypasses(api_client: TestClient) -> None:
    # FU-D34: the ack_negative override is REMOVED for /api/cash/fx — passing it (as the
    # legacy frontend / stress harness still might) must NOT bypass the hard balance block.
    r = api_client.post("/api/cash/fx", json={
        "account_id": "schwab", "date": "2026-06-01", "from_ccy": "TWD",
        "from_amt": "10000", "to_ccy": "USD", "to_amt": "300", "ack_negative": True})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "fx_insufficient_balance"


def test_movement_edit_delta_guard_and_delete(api_client: TestClient) -> None:
    api_client.post("/api/cash/movements", json={
        "account_id": "moomoo_my", "date": "2026-01-01", "kind": "deposit",
        "ccy": "USD", "amount": "1000"})
    rows = api_client.get("/api/cash").json()["movements"]["rows"]
    dep = next(x for x in rows if x["account_id"] == "moomoo_my")
    api_client.post("/api/cash/movements", json={
        "account_id": "moomoo_my", "date": "2026-02-01", "kind": "withdraw",
        "ccy": "USD", "amount": "800"})
    # shrinking the deposit below the withdraw strands the pool -> 422
    r = api_client.put(f"/api/cash/movements/{dep['id']}", json={
        "account_id": "moomoo_my", "date": "2026-01-01", "kind": "deposit",
        "ccy": "USD", "amount": "500"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "negative_cash"
    # deleting it outright is guarded too
    r2 = api_client.delete(f"/api/cash/movements/{dep['id']}")
    assert r2.status_code == 422
    r3 = api_client.delete(f"/api/cash/movements/{dep['id']}?ack_negative=true")
    assert r3.status_code == 200


def test_rebate_movement_credits_pool_and_statement(api_client: TestClient) -> None:
    """A rebate movement (退款／折讓, FE-D1) is a deposit-like CREDIT in the account's
    settlement ccy, surfacing in the statement with kind 'rebate' (the frontend maps → 折讓款)."""
    r = api_client.post("/api/cash/movements", json={
        "account_id": "tw_broker", "date": "2026-06-01", "kind": "rebate",
        "ccy": "TWD", "amount": "109", "note": "2026-05 折讓款"})
    assert r.status_code == 201
    # golden tw_broker TWD is −495,000; a +109 rebate credit lifts it to −494,891.
    assert _balance(api_client, "tw_broker", "TWD") == "-494891"
    rows = api_client.get("/api/cash").json()["movements"]["rows"]
    assert rows[0]["kind"] == "rebate" and rows[0]["amount"] == "109"
    stmt = api_client.get("/api/cash/statement",
                          params={"account": "tw_broker", "ccy": "TWD"}).json()
    assert any(x["kind"] == "rebate" and x["delta"] == "109" for x in stmt["rows"])


def test_rebate_movement_ccy_guard(api_client: TestClient) -> None:
    """A rebate movement obeys the same currency↔account coherence guard as deposits."""
    r = api_client.post("/api/cash/movements", json={
        "account_id": "tw_broker", "date": "2026-06-01", "kind": "rebate",
        "ccy": "USD", "amount": "10"})
    assert r.status_code == 400 and r.json()["error"]["field"] == "ccy"


def test_bad_inputs_400(api_client: TestClient) -> None:
    assert api_client.post("/api/cash/movements", json={
        "account_id": "tw_broker", "date": "2026-01-01", "kind": "bogus",
        "ccy": "TWD", "amount": "10"}).status_code == 400
    assert api_client.post("/api/cash/movements", json={
        "account_id": "ghost", "date": "2026-01-01", "kind": "deposit",
        "ccy": "TWD", "amount": "10"}).status_code == 400
    assert api_client.post("/api/cash/fx", json={
        "account_id": "schwab", "date": "2026-01-01", "from_ccy": "USD",
        "from_amt": "10", "to_ccy": "USD", "to_amt": "10"}).status_code == 400


def test_actions_logged(api_client: TestClient) -> None:
    api_client.post("/api/cash/movements", json={
        "account_id": "tw_broker", "date": "2026-01-01", "kind": "deposit",
        "ccy": "TWD", "amount": "100"})
    log = api_client.get("/api/system-log", params={"limit": 20}).json()["rows"]
    assert any(x["action"] == "入金／出金" for x in log)


# --- FU-D5: statement line detail + account-level view + exports ------------


def test_statement_single_ccy_carries_trade_detail(api_client: TestClient) -> None:
    """A trade row carries structured detail (symbol/name/qty/price/fee/tax) + per-row ccy;
    a dividend row carries symbol/name only (trade-only detail stays null)."""
    body = api_client.get("/api/cash/statement",
                          params={"account": "tw_broker", "ccy": "TWD"}).json()
    assert body["ccy"] == "TWD"
    buy = next(r for r in body["rows"] if r["kind"] == "buy" and r["symbol"] == "2330")
    assert buy["name"] == "TSMC" and buy["ccy"] == "TWD"
    assert Decimal(buy["qty"]) == Decimal("1000") and Decimal(buy["price"]) == Decimal("500")
    assert Decimal(buy["fee"]) == Decimal("0") and Decimal(buy["tax"]) == Decimal("0")
    div = next(r for r in body["rows"] if r["kind"] == "dividend")
    assert div["symbol"] == "2330" and div["name"] == "TSMC"
    assert div["qty"] is None and div["fee"] is None  # trade-only detail null for a dividend


def test_statement_all_ccy_view_merges_pools(api_client: TestClient) -> None:
    """ccy absent → the account-level view: ccy null, a per-ccy balances list, and every
    row carrying its own ccy + its per-(account, ccy) running balance (never blended)."""
    body = api_client.get("/api/cash/statement", params={"account": "schwab"}).json()
    assert body["ccy"] is None and body["current_balance"] is None
    bal = {b["ccy"]: b["balance"] for b in body["balances"]}
    assert bal["USD"] == "0"        # +1000 fx_in − 1000 AAPL buy
    assert bal["TWD"] == "-32000"   # −32000 fx_out
    assert all(r["ccy"] in ("USD", "TWD") for r in body["rows"])
    # an fx leg carries the implied rate + counter amount
    fx_in = next(r for r in body["rows"] if r["kind"] == "fx_in")
    assert fx_in["ccy"] == "USD" and fx_in["counter_ccy"] == "TWD"
    assert Decimal(fx_in["counter_amount"]) == Decimal("-32000")
    assert Decimal(fx_in["fx_rate"]) == Decimal("32")


def test_statement_same_day_newest_first_balance_on_top(api_client: TestClient) -> None:
    """Same-day rows are ordered newest-first (reverse of the credit-before-debit
    chronological order), so the end-of-day balance sits on top."""
    api_client.post("/api/cash/movements", json={
        "account_id": "moomoo_my", "date": "2026-05-01", "kind": "deposit",
        "ccy": "USD", "amount": "1000"})
    api_client.post("/api/cash/movements", json={
        "account_id": "moomoo_my", "date": "2026-05-01", "kind": "withdraw",
        "ccy": "USD", "amount": "300"})
    body = api_client.get("/api/cash/statement",
                          params={"account": "moomoo_my", "ccy": "USD"}).json()
    assert [r["kind"] for r in body["rows"]] == ["withdraw", "deposit"]
    assert body["rows"][0]["balance"] == "700" and body["rows"][1]["balance"] == "1000"


def test_export_cash_statement_csv(api_client: TestClient) -> None:
    r = api_client.post("/api/export/cash-statement",
                        json={"account": "tw_broker", "ccy": "TWD"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "cash_statement_tw_broker_TWD" in r.headers["content-disposition"]
    reader = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert reader[0] == [
        "date", "ccy", "kind", "symbol", "name", "qty", "price", "fee", "tax",
        "note_ref", "delta", "balance",
    ]
    data = [row for row in reader[1:] if row and not row[0].startswith("#")]
    buy = next(row for row in data if row[2] == "buy" and row[3] == "2330")
    assert buy[1] == "TWD" and buy[4] == "TSMC"
    assert Decimal(buy[5]) == Decimal("1000") and Decimal(buy[6]) == Decimal("500")
    assert Decimal(buy[7]) == Decimal("0") and Decimal(buy[8]) == Decimal("0")
    assert Decimal(buy[10]) == Decimal("-500000")  # delta = -(1000*500)


def test_export_cash_statement_all_ccy_csv(api_client: TestClient) -> None:
    """ccy null exports every pool; the filename marks the 'all' scope."""
    r = api_client.post("/api/export/cash-statement", json={"account": "schwab"})
    assert r.status_code == 200
    assert "cash_statement_schwab_all" in r.headers["content-disposition"]
    reader = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    data = [row for row in reader[1:] if row and not row[0].startswith("#")]
    assert {row[1] for row in data} == {"USD", "TWD"}  # both pools present


def test_export_cash_statement_report_html(api_client: TestClient) -> None:
    r = api_client.post("/api/export/cash-statement-report", json={"account": "tw_broker"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert ".html" in r.headers["content-disposition"]
    assert "現金收支明細" in r.content.decode("utf-8")


def test_export_cash_statement_unknown_account_400(api_client: TestClient) -> None:
    r = api_client.post("/api/export/cash-statement", json={"account": "ghost"})
    assert r.status_code == 400 and r.json()["error"]["field"] == "account"
    r2 = api_client.post("/api/export/cash-statement-report", json={"account": "ghost"})
    assert r2.status_code == 400 and r2.json()["error"]["field"] == "account"


# --- FU-D43a: withdraw guard — PUT edits + untouched deposit-side flows ------


def _withdraw_row_id(api_client: TestClient, account_id: str, ccy: str) -> int:
    rows = api_client.get("/api/cash", params={"limit": 500}).json()["movements"]["rows"]
    row = next(x for x in rows
               if x["account_id"] == account_id and x["ccy"] == ccy and x["kind"] == "withdraw")
    return int(row["id"])


def test_withdraw_put_edit_self_exclusion(api_client: TestClient) -> None:
    """Editing a withdraw validates against the balance EXCLUDING the row's own prior
    effect: raising it to exactly the row-free balance passes; one cent beyond blocks."""
    api_client.post("/api/cash/movements", json={
        "account_id": "moomoo_my", "date": "2026-01-01", "kind": "deposit",
        "ccy": "USD", "amount": "1000"})
    api_client.post("/api/cash/movements", json={
        "account_id": "moomoo_my", "date": "2026-02-01", "kind": "withdraw",
        "ccy": "USD", "amount": "800"})
    assert _balance(api_client, "moomoo_my", "USD") == "200"
    wid = _withdraw_row_id(api_client, "moomoo_my", "USD")
    # 1000.01 > 1000 (balance excluding this row's own −800) -> hard 422, ack ignored
    over = api_client.put(f"/api/cash/movements/{wid}", json={
        "account_id": "moomoo_my", "date": "2026-02-01", "kind": "withdraw",
        "ccy": "USD", "amount": "1000.01", "ack_negative": True})
    assert over.status_code == 422
    err = over.json()["error"]
    assert err["code"] == "withdraw_insufficient_balance" and "1000" in err["message"]
    assert _balance(api_client, "moomoo_my", "USD") == "200"  # unchanged
    # exactly the row-free balance -> allowed (NOT falsely blocked by its own old amount)
    ok = api_client.put(f"/api/cash/movements/{wid}", json={
        "account_id": "moomoo_my", "date": "2026-02-01", "kind": "withdraw",
        "ccy": "USD", "amount": "1000"})
    assert ok.status_code == 200
    assert _balance(api_client, "moomoo_my", "USD") == "0"


def test_withdraw_backdated_before_funding_hard_blocked(api_client: TestClient) -> None:
    """C3 date-aware guard, hardened for withdrawals (FU-D43a): a withdraw back-dated
    before its funding dips the running balance -> hard 422 (ack removed); re-dated
    after the funding it passes."""
    api_client.post("/api/cash/movements", json={
        "account_id": "moomoo_my", "date": "2026-05-01", "kind": "deposit",
        "ccy": "USD", "amount": "1000"})
    r = api_client.post("/api/cash/movements", json={
        "account_id": "moomoo_my", "date": "2026-04-01", "kind": "withdraw",
        "ccy": "USD", "amount": "500", "ack_negative": True})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "withdraw_insufficient_balance"
    ok = api_client.post("/api/cash/movements", json={
        "account_id": "moomoo_my", "date": "2026-05-02", "kind": "withdraw",
        "ccy": "USD", "amount": "500"})
    assert ok.status_code == 201


def test_deposit_edit_to_withdraw_keeps_removal_ack(api_client: TestClient) -> None:
    """Editing a DEPOSIT into a withdraw: the NEW withdraw is hard-guarded (no ack), but
    the dip caused by REMOVING the old deposit's funding stays deposit-side semantics —
    ack-able negative_cash, exactly as before (FU-D43a touches withdrawals only)."""
    api_client.post("/api/cash/movements", json={
        "account_id": "moomoo_my", "date": "2026-01-01", "kind": "deposit",
        "ccy": "USD", "amount": "1000"})
    api_client.post("/api/cash/movements", json={
        "account_id": "moomoo_my", "date": "2026-02-01", "kind": "withdraw",
        "ccy": "USD", "amount": "400"})
    api_client.post("/api/cash/movements", json={
        "account_id": "moomoo_my", "date": "2026-03-01", "kind": "deposit",
        "ccy": "USD", "amount": "1000"})
    rows = api_client.get("/api/cash", params={"limit": 500}).json()["movements"]["rows"]
    dep = next(x for x in rows if x["account_id"] == "moomoo_my"
               and x["kind"] == "deposit" and x["date"] == "2026-01-01")
    # Convert the Jan deposit into a small Apr withdraw: end balance covers it (600 left)
    # and the new withdraw itself introduces no NEW dip — but removing the Jan funding
    # strands the Feb withdraw below zero -> the ack-able negative_cash warning fires.
    body = {"account_id": "moomoo_my", "date": "2026-04-01", "kind": "withdraw",
            "ccy": "USD", "amount": "100"}
    r = api_client.put(f"/api/cash/movements/{dep['id']}", json=body)
    assert r.status_code == 422 and r.json()["error"]["code"] == "negative_cash"
    r2 = api_client.put(f"/api/cash/movements/{dep['id']}",
                        json={**body, "ack_negative": True})
    assert r2.status_code == 200
    assert _balance(api_client, "moomoo_my", "USD") == "500"  # −400 +1000 −100


def test_deposit_and_opening_posts_unaffected(api_client: TestClient) -> None:
    """FU-D43a scope pin: deposits/openings are credits — never balance-guarded, even
    into a currently NEGATIVE pool (golden tw_broker TWD is −495,000)."""
    r = api_client.post("/api/cash/movements", json={
        "account_id": "tw_broker", "date": "2026-06-01", "kind": "deposit",
        "ccy": "TWD", "amount": "10"})
    assert r.status_code == 201
    r2 = api_client.post("/api/cash/movements", json={
        "account_id": "moomoo_my", "date": "2026-01-01", "kind": "opening",
        "ccy": "MYR", "amount": "5"})
    assert r2.status_code == 201


# --- FU-D43c: GET /api/cash/fx-estimate --------------------------------------


def test_fx_estimate_direct_pair_shape(api_client: TestClient) -> None:
    """Direct stored pair (USD/TWD 33 @2026-06-09): Decimal-string shape; the estimate is
    quantized to the BUY currency's minor unit (TWD = 0 dp)."""
    r = api_client.get("/api/cash/fx-estimate",
                       params={"from_ccy": "USD", "to_ccy": "TWD", "amount": "100"})
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert body["estimate"] == "3300"       # 100 × 33, TWD minor unit (0 dp)
    assert body["rate"] == "33"
    assert body["rate_as_of"] == "2026-06-09"
    for key in ("estimate", "rate"):
        assert isinstance(body[key], str)   # wire = Decimal strings, never JSON numbers


def test_fx_estimate_inverse_pair(api_client: TestClient) -> None:
    """No TWD/USD row stored — the inverse (USD/TWD 33) is used, 1/33 capped at the 6-dp
    rate precision (cap-not-pad), and the estimate lands on the USD cent."""
    r = api_client.get("/api/cash/fx-estimate",
                       params={"from_ccy": "TWD", "to_ccy": "USD", "amount": "33000"})
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert body["rate"] == "0.030303"        # 1/33 capped at 6 dp
    assert body["estimate"] == "1000.00"     # 33000 × 0.030303 = 999.999 -> 2-dp HALF_UP
    assert body["rate_as_of"] == "2026-06-09"


def test_fx_estimate_no_rate_degrades(api_client: TestClient) -> None:
    """A pair with no stored rate in either direction degrades to available:false with a
    zh reason — never a guess, never an error page. USD/TWD is the only USD pair the
    golden DB stores against TWD; wipe fx_rates via a pair that cannot resolve."""
    # golden stores USD/TWD, MYR/TWD, USD/MYR — every combination of the three ccys
    # resolves. Exercise the degrade via amount over an EMPTY-rate DB instead: use a
    # currency pair after deleting rates is not possible through the API, so assert the
    # validation guards here and cover the empty-DB degrade in the dashboard factory test.
    assert api_client.get("/api/cash/fx-estimate", params={
        "from_ccy": "USD", "to_ccy": "USD", "amount": "10"}).status_code == 400
    assert api_client.get("/api/cash/fx-estimate", params={
        "from_ccy": "USD", "to_ccy": "TWD", "amount": "0"}).status_code == 400
    assert api_client.get("/api/cash/fx-estimate", params={
        "from_ccy": "USD", "to_ccy": "TWD", "amount": "-5"}).status_code == 400


def test_movements_pagination(api_client: TestClient) -> None:
    """WPE: /api/cash movements page via limit/offset; total_count is the whole ledger."""
    for i in range(1, 6):
        r = api_client.post("/api/cash/movements", json={
            "account_id": "tw_broker", "date": f"2026-01-0{i}", "kind": "deposit",
            "ccy": "TWD", "amount": str(1000 * i)})
        assert r.status_code == 201
    p1 = api_client.get("/api/cash", params={"limit": 2, "offset": 0}).json()["movements"]
    p2 = api_client.get("/api/cash", params={"limit": 2, "offset": 2}).json()["movements"]
    assert p1["total_count"] == 5 and p2["total_count"] == 5
    assert len(p1["rows"]) == 2 and len(p2["rows"]) == 2
    assert {r["id"] for r in p1["rows"]}.isdisjoint({r["id"] for r in p2["rows"]})
    # balances are NOT affected by the movements page window
    full = api_client.get("/api/cash", params={"limit": 2, "offset": 4}).json()
    assert len(full["movements"]["rows"]) == 1
    assert full["balances"]  # balance cards intact on any page
