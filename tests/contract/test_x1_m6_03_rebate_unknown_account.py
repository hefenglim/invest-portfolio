"""M6-03 (owner ruling 2026-09-06, option b): the rebate confirm door tells an account that
does not exist apart from one that exists but rebates nothing.

``if acct is None or rebate_rate <= 0`` answered 「帳戶 ghost 無折讓款設定」 for both — a
sentence that asserts, about an account nobody registered, that its rebate setting is empty.
The unknown-account sentence already has ONE owner in ``data_ingestion``
(``validate.unknown_account_issue``), whose docstring recorded that the routers still build
it by hand and asked for a message-only helper. That helper now exists; this door is its
first router-side caller. The other six hand-built copies are deliberately NOT swept in here.
"""

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.validate import unknown_account_issue


def test_an_unknown_account_is_told_it_does_not_exist(api_client: TestClient) -> None:
    r = api_client.post("/api/rebates/confirm",
                        json={"account_id": "ghost", "month": "2026-05", "amount": "10"})
    assert r.status_code == 400, r.json()
    err = r.json()["error"]
    assert err == {"code": "validation_error", "message": "帳戶 ghost 不存在",
                   "field": "account_id"}, err


def test_a_blank_account_is_told_it_is_blank(api_client: TestClient) -> None:
    r = api_client.post("/api/rebates/confirm",
                        json={"account_id": "  ", "month": "2026-05", "amount": "10"})
    assert r.status_code == 400, r.json()
    assert r.json()["error"]["message"] == "帳戶不可空白", r.json()


def test_a_real_account_without_a_rebate_setting_keeps_its_own_sentence(
    api_client: TestClient,
) -> None:
    r = api_client.post("/api/rebates/confirm",
                        json={"account_id": "schwab", "month": "2026-05", "amount": "10"})
    assert r.status_code == 400, r.json()
    assert r.json()["error"]["message"] == "帳戶 schwab 無折讓款設定", r.json()
    assert r.json()["error"]["field"] == "account_id"


def test_the_message_helper_and_the_issue_helper_share_one_sentence() -> None:
    from portfolio_dash.data_ingestion.validate import unknown_account_message
    for account_id in ("ghost", "", "   "):
        assert unknown_account_message(account_id) == unknown_account_issue(account_id).message
