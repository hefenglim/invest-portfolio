"""The manual cash door's EXACT responses, pinned so the shared guard cannot drift from them.

``validate_cash_movement`` was extracted out of ``api/routers/cash.py`` (2026-08-12) so the
CSV import kind could run the same guard the form runs. An extraction is only a refactor if
the door it came out of answers identically afterwards — and "identically" for an error
envelope means the STATUS, the ``code``, the ``field`` and the zh ``message`` text, not just
"it still 400s".

Every expectation below was RECORDED from the pre-extraction implementation by replaying this
exact sequence against it and dumping the responses; the post-extraction run reproduced all 35
cases plus the resulting ledger byte-for-byte. What is pinned here is therefore evidence, not
a restatement of the new code's behaviour.

⚠ ONE pin was deliberately re-recorded (QA-18, 2026-08-29): the ``unknown_movement_kind``
message listed 「deposit / withdraw / opening / rebate」 — four of the SEVEN registered kinds.
It never grew when the broker-statement importer added INTEREST, INTEREST_EXPENSE and
BROKER_FEE (2026-08-13), so the one message whose job is to say what IS accepted told the
owner that three legal kinds were not. The message is now derived from ``CashKind`` +
``CASH_KIND_ZH``; the expectation here stays a LITERAL on purpose, because a pin that derives
from the code it guards has stopped being a pin. Re-record it if the vocabulary grows again —
that is the intended maintenance, and it is one line.

The cases run as ONE ordered sequence against one ledger, because several of them only mean
something in sequence: the withdraw messages quote a balance that earlier rows created, the
self-exclusion edit needs a row to edit, and the REBATE lock needs a booked rebate. The final
ledger state is asserted at the end for the same reason — an identical set of responses over a
different set of written rows would not be the same behaviour.

Golden pools before any of this: schwab USD 0 / schwab TWD −32,000 / tw_broker TWD −495,000.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

# (name, method, body-or-target, expected status, expected error dict or None)
# ``target`` names an earlier case whose created id to act on; "__missing__" is an absent id.
#: The accepted-kind tail of the ``unknown_movement_kind`` message. Kept as ONE literal
#: (not derived from ``CashKind``): a pin that derives from the code it guards has
#: stopped being a pin. Extracted only because the line does not otherwise fit.
_KINDS = (
    "（可用類型：入金 deposit／出金 withdraw／期初資金 opening／折讓款 rebate／"
    "利息 interest／融資利息 interest_expense／券商費用 broker_fee）"
)


_POST = "POST"
_PUT = "PUT"
_DELETE = "DELETE"

_SEQUENCE: list[dict[str, Any]] = [
    # --- structural rejections, in the router's original evaluation order ----------------
    {"n": "bad_kind", "m": _POST, "b": {
        "account_id": "tw_broker", "date": "2026-07-01", "kind": "transfer",
        "ccy": "TWD", "amount": "100"},
     "status": 400, "err": {
         "code": "validation_error", "field": "kind",
         "message": f"未知類型 transfer{_KINDS}"}},
    # An unknown kind AND a bad amount: the FIRST failure is the one reported. Pinned
    # because collecting both would be a nicer bulk experience and a changed manual door.
    {"n": "bad_kind_wins_over_bad_amount", "m": _POST, "b": {
        "account_id": "tw_broker", "date": "2026-07-01", "kind": "transfer",
        "ccy": "TWD", "amount": "0"},
     "status": 400, "err": {
         "code": "validation_error", "field": "kind",
         "message": f"未知類型 transfer{_KINDS}"}},
    {"n": "zero_amount", "m": _POST, "b": {
        "account_id": "tw_broker", "date": "2026-07-01", "kind": "deposit",
        "ccy": "TWD", "amount": "0"},
     "status": 400, "err": {"code": "validation_error", "field": "amount",
                            "message": "金額必須大於 0"}},
    {"n": "negative_amount", "m": _POST, "b": {
        "account_id": "tw_broker", "date": "2026-07-01", "kind": "deposit",
        "ccy": "TWD", "amount": "-5"},
     "status": 400, "err": {"code": "validation_error", "field": "amount",
                            "message": "金額必須大於 0"}},
    {"n": "unknown_account", "m": _POST, "b": {
        "account_id": "ghost", "date": "2026-07-01", "kind": "deposit",
        "ccy": "TWD", "amount": "100"},
     "status": 400, "err": {"code": "validation_error", "field": "account_id",
                            "message": "帳戶 ghost 不存在"}},
    {"n": "ccy_not_allowed", "m": _POST, "b": {
        "account_id": "tw_broker", "date": "2026-07-01", "kind": "deposit",
        "ccy": "USD", "amount": "100"},
     "status": 400, "err": {"code": "validation_error", "field": "ccy",
                            "message": "USD 非此帳戶可用幣別（交割幣 TWD／資金幣 TWD）"}},
    {"n": "ccy_not_allowed_merged_account", "m": _POST, "b": {
        "account_id": "moomoo_my", "date": "2026-07-01", "kind": "deposit",
        "ccy": "TWD", "amount": "100"},
     "status": 400, "err": {"code": "validation_error", "field": "ccy",
                            "message": "TWD 非此帳戶可用幣別（交割幣 USD／資金幣 MYR）"}},
    # --- acquisition cost (spec 2026-07-30, F1) -----------------------------------------
    {"n": "acq_both_forms", "m": _POST, "b": {
        "account_id": "schwab", "date": "2026-07-01", "kind": "opening",
        "ccy": "USD", "amount": "1000", "acq_rate": "31", "acq_home_amount": "31000"},
     "status": 400, "err": {"code": "validation_error", "field": "acq_home_amount",
                            "message": "取得成本請擇一填寫（家幣金額 或 匯率）"}},
    {"n": "acq_on_home_ccy", "m": _POST, "b": {
        "account_id": "schwab", "date": "2026-07-01", "kind": "deposit",
        "ccy": "TWD", "amount": "1000", "acq_rate": "31"},
     "status": 400, "err": {"code": "validation_error", "field": "acq_home_amount",
                            "message": "取得成本僅適用外幣資金流（本帳戶資金幣別為 TWD）"}},
    {"n": "acq_on_withdraw", "m": _POST, "b": {
        "account_id": "schwab", "date": "2026-07-01", "kind": "withdraw",
        "ccy": "USD", "amount": "10", "acq_rate": "31"},
     "status": 400, "err": {"code": "validation_error", "field": "acq_home_amount",
                            "message": "出金是處分，不帶取得成本"}},
    {"n": "acq_amount_zero", "m": _POST, "b": {
        "account_id": "schwab", "date": "2026-07-01", "kind": "opening",
        "ccy": "USD", "amount": "1000", "acq_home_amount": "0"},
     "status": 400, "err": {"code": "validation_error", "field": "acq_home_amount",
                            "message": "取得成本必須大於 0"}},
    {"n": "acq_amount_negative", "m": _POST, "b": {
        "account_id": "schwab", "date": "2026-07-01", "kind": "opening",
        "ccy": "USD", "amount": "1000", "acq_home_amount": "-1"},
     "status": 400, "err": {"code": "validation_error", "field": "acq_home_amount",
                            "message": "取得成本必須大於 0"}},
    {"n": "acq_rate_zero", "m": _POST, "b": {
        "account_id": "schwab", "date": "2026-07-01", "kind": "opening",
        "ccy": "USD", "amount": "1000", "acq_rate": "0"},
     "status": 400, "err": {"code": "validation_error", "field": "acq_rate",
                            "message": "取得匯率必須大於 0"}},
    {"n": "acq_rate_negative", "m": _POST, "b": {
        "account_id": "schwab", "date": "2026-07-01", "kind": "opening",
        "ccy": "USD", "amount": "1000", "acq_rate": "-2"},
     "status": 400, "err": {"code": "validation_error", "field": "acq_rate",
                            "message": "取得匯率必須大於 0"}},
    # --- FU-D43a withdraw guard: HARD 422, and the message quotes the real balance -------
    {"n": "withdraw_empty_pool", "m": _POST, "b": {
        "account_id": "moomoo_my", "date": "2026-07-01", "kind": "withdraw",
        "ccy": "MYR", "amount": "1"},
     "status": 422, "err": {
         "code": "withdraw_insufficient_balance", "field": "amount",
         "message": "出金金額 1 MYR 超過 Moomoo MY 的 MYR 賬戶現金 0 — "
                    "出金不可透支（請先補登入金或換匯）"}},
    {"n": "withdraw_negative_pool", "m": _POST, "b": {
        "account_id": "tw_broker", "date": "2026-07-01", "kind": "withdraw",
        "ccy": "TWD", "amount": "100"},
     "status": 422, "err": {
         "code": "withdraw_insufficient_balance", "field": "amount",
         "message": "出金金額 100 TWD 超過 TW Broker 的 TWD 賬戶現金 -495000 — "
                    "出金不可透支（請先補登入金或換匯）"}},
    {"n": "withdraw_ack_does_not_bypass", "m": _POST, "b": {
        "account_id": "tw_broker", "date": "2026-07-01", "kind": "withdraw",
        "ccy": "TWD", "amount": "100", "ack_negative": True},
     "status": 422, "err": {
         "code": "withdraw_insufficient_balance", "field": "amount",
         "message": "出金金額 100 TWD 超過 TW Broker 的 TWD 賬戶現金 -495000 — "
                    "出金不可透支（請先補登入金或換匯）"}},
    # --- happy paths (the ids they mint are load-bearing for the edits below) ------------
    {"n": "deposit_ok", "m": _POST, "b": {
        "account_id": "tw_broker", "date": "2026-07-01", "kind": "deposit",
        "ccy": "TWD", "amount": "600000", "note": "初始入金"}, "status": 201, "id": 1},
    {"n": "rebate_ok", "m": _POST, "b": {
        "account_id": "tw_broker", "date": "2026-07-02", "kind": "rebate",
        "ccy": "TWD", "amount": "153"}, "status": 201, "id": 2},
    {"n": "opening_foreign_by_rate", "m": _POST, "b": {
        "account_id": "schwab", "date": "2026-07-01", "kind": "opening",
        "ccy": "USD", "amount": "100000", "acq_rate": "31.3587"}, "status": 201, "id": 3},
    # A rate that does NOT divide evenly: 1000 x 4.40001 = 4400.01 after MYR 2-dp quantize.
    # The rate is thrown away and re-derived on read, so the stored authority is the amount.
    {"n": "opening_foreign_by_amount", "m": _POST, "b": {
        "account_id": "moomoo_my", "date": "2026-07-01", "kind": "opening",
        "ccy": "USD", "amount": "1000", "acq_home_amount": "4400.005"},
     "status": 201, "id": 4},
    {"n": "deposit_foreign_no_cost", "m": _POST, "b": {
        "account_id": "moomoo_my", "date": "2026-07-02", "kind": "deposit",
        "ccy": "USD", "amount": "500"}, "status": 201, "id": 5},
    {"n": "withdraw_within_balance", "m": _POST, "b": {
        "account_id": "tw_broker", "date": "2026-07-20", "kind": "withdraw",
        "ccy": "TWD", "amount": "50000"}, "status": 201, "id": 6},
    {"n": "withdraw_over_new_balance", "m": _POST, "b": {
        "account_id": "tw_broker", "date": "2026-07-21", "kind": "withdraw",
        "ccy": "TWD", "amount": "999999"},
     "status": 422, "err": {
         "code": "withdraw_insufficient_balance", "field": "amount",
         "message": "出金金額 999999 TWD 超過 TW Broker 的 TWD 賬戶現金 55153 — "
                    "出金不可透支（請先補登入金或換匯）"}},
    # audit C3: the END balance covers it, but the pool dips below zero before its funding.
    {"n": "withdraw_backdated_before_funding", "m": _POST, "b": {
        "account_id": "moomoo_my", "date": "2026-06-01", "kind": "withdraw",
        "ccy": "USD", "amount": "400"},
     "status": 422, "err": {
         "code": "withdraw_insufficient_balance", "field": "amount",
         "message": "此筆出金會使 Moomoo MY 的 USD 現金於某時點降至 -400"
                    "（出金日早於資金到位）— 出金不可透支，請先補登入金或換匯"}},
    {"n": "withdraw_exact_balance", "m": _POST, "b": {
        "account_id": "moomoo_my", "date": "2026-07-25", "kind": "withdraw",
        "ccy": "USD", "amount": "1500"}, "status": 201, "id": 7},
    # --- PUT: the same guard, plus the edit-only rules the router keeps ------------------
    {"n": "edit_bad_kind", "m": _PUT, "target": "deposit_ok", "b": {
        "account_id": "tw_broker", "date": "2026-07-01", "kind": "nope",
        "ccy": "TWD", "amount": "600000"},
     "status": 400, "err": {"code": "validation_error", "field": "kind",
                            "message": f"未知類型 nope{_KINDS}"}},
    {"n": "edit_rebate_kind_locked", "m": _PUT, "target": "rebate_ok", "b": {
        "account_id": "tw_broker", "date": "2026-07-02", "kind": "deposit",
        "ccy": "TWD", "amount": "153"},
     "status": 400, "err": {
         "code": "validation_error", "field": "kind",
         "message": "折讓款的類型與日期已鎖定以避免重複入帳"
                    "(可修正金額或備註;如需撤銷請刪除此筆)"}},
    {"n": "edit_rebate_date_locked", "m": _PUT, "target": "rebate_ok", "b": {
        "account_id": "tw_broker", "date": "2026-07-03", "kind": "rebate",
        "ccy": "TWD", "amount": "153"},
     "status": 400, "err": {
         "code": "validation_error", "field": "kind",
         "message": "折讓款的類型與日期已鎖定以避免重複入帳"
                    "(可修正金額或備註;如需撤銷請刪除此筆)"}},
    # Amount stays correctable — but the deposit-side ack guard still applies, and here the
    # tw_broker pool was already negative at the 2026-01-05 buy, so it answers negative_cash.
    {"n": "edit_rebate_amount", "m": _PUT, "target": "rebate_ok", "b": {
        "account_id": "tw_broker", "date": "2026-07-02", "kind": "rebate",
        "ccy": "TWD", "amount": "200", "note": "改金額"},
     "status": 422, "err": {
         "code": "negative_cash",
         "message": "此筆會使 tw_broker 的 TWD 現金於某時點降至 -500000 — "
                    "通常代表漏記入金或換匯;確認無誤可強制寫入"}},
    {"n": "edit_deposit_shrinks_pool", "m": _PUT, "target": "deposit_ok", "b": {
        "account_id": "tw_broker", "date": "2026-07-01", "kind": "deposit",
        "ccy": "TWD", "amount": "1"},
     "status": 422, "err": {
         "code": "negative_cash",
         "message": "此筆會使 tw_broker 的 TWD 現金於某時點降至 -544846 — "
                    "通常代表漏記入金或換匯;確認無誤可強制寫入"}},
    # ...and the ack DOES still bypass the deposit-side guard (only the withdraw one is hard).
    {"n": "edit_deposit_shrinks_acked", "m": _PUT, "target": "deposit_ok", "b": {
        "account_id": "tw_broker", "date": "2026-07-01", "kind": "deposit",
        "ccy": "TWD", "amount": "1", "ack_negative": True}, "status": 200},
    {"n": "edit_unknown_id", "m": _PUT, "target": "__missing__", "b": {
        "account_id": "tw_broker", "date": "2026-07-01", "kind": "deposit",
        "ccy": "TWD", "amount": "1"},
     "status": 404, "err": {"code": "not_found", "message": "紀錄 #99999 不存在"}},
    {"n": "delete_rebate", "m": _DELETE, "target": "rebate_ok",
     "status": 422, "err": {
         "code": "negative_cash",
         "message": "此筆會使 tw_broker 的 TWD 現金於某時點降至 -544999 — "
                    "通常代表漏記入金或換匯;確認無誤可強制寫入"}},
    {"n": "delete_unknown_id", "m": _DELETE, "target": "__missing__",
     "status": 404, "err": {"code": "not_found", "message": "紀錄 #99999 不存在"}},
]

# The ledger the sequence above leaves behind — asserted because an identical set of
# responses over a different set of WRITTEN rows would not be the same behaviour.
_EXPECTED_BALANCES = [
    ("moomoo_my", "USD", "0"),
    ("schwab", "TWD", "-32000"),
    ("schwab", "USD", "100000"),
    ("tw_broker", "TWD", "-544846"),
]
_EXPECTED_MOVEMENTS = [
    # id, kind, ccy, amount, acq_home_amount, acq_rate (newest-first, as /api/cash serves)
    (7, "withdraw", "USD", "1500", None, None),
    (6, "withdraw", "TWD", "50000", None, None),
    (5, "deposit", "USD", "500", None, None),
    (2, "rebate", "TWD", "153", None, None),
    (4, "opening", "USD", "1000", "4400.01", "4.40001"),
    (3, "opening", "USD", "100000", "3135870", "31.3587"),
    (1, "deposit", "TWD", "1", None, None),
]


@pytest.fixture
def replayed(api_client: TestClient) -> dict[str, Any]:
    """Run the whole sequence once; return {case name: (status, body)} plus the ledger."""
    seen: dict[str, Any] = {}
    ids: dict[str, int] = {}
    for case in _SEQUENCE:
        if case["m"] == _POST:
            response = api_client.post("/api/cash/movements", json=case["b"])
        else:
            move_id = ids.get(str(case["target"]), 99999)
            response = (api_client.put(f"/api/cash/movements/{move_id}", json=case["b"])
                        if case["m"] == _PUT
                        else api_client.delete(f"/api/cash/movements/{move_id}"))
        body = response.json()
        seen[case["n"]] = (response.status_code, body)
        if response.status_code == 201:
            ids[case["n"]] = int(body["id"])
    seen["__cash__"] = api_client.get("/api/cash", params={"limit": 500}).json()
    return seen


@pytest.mark.parametrize("case", _SEQUENCE, ids=lambda c: str(c["n"]))
def test_recorded_response_is_reproduced_exactly(
    replayed: dict[str, Any], case: dict[str, Any]
) -> None:
    status, body = replayed[case["n"]]
    assert status == case["status"], f"{case['n']}: {body}"
    if "err" in case:
        assert body["error"] == case["err"], f"{case['n']}: {body}"
    elif case["status"] == 201:
        assert body == {"id": case["id"]}, f"{case['n']}: {body}"
    else:
        assert body["ok"] is True


def test_the_sequence_writes_exactly_the_recorded_ledger(
    replayed: dict[str, Any]
) -> None:
    cash = replayed["__cash__"]
    assert [(b["account_id"], b["ccy"], b["amount"]) for b in cash["balances"]] == (
        _EXPECTED_BALANCES)
    assert [
        (m["id"], m["kind"], m["ccy"], m["amount"], m["acq_home_amount"], m["acq_rate"])
        for m in cash["movements"]["rows"]
    ] == _EXPECTED_MOVEMENTS
