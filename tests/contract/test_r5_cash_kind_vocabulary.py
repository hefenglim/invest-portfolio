"""R5 / QA-18: the unknown-kind rejection must name EVERY registered cash kind.

``validate.py`` answered an unrecognised kind with
``未知類型 transfer（deposit / withdraw / opening / rebate）`` — a hand-written list of four
that had been stale since 2026-08-13, when ``INTEREST`` / ``INTEREST_EXPENSE`` /
``BROKER_FEE`` were registered. The message therefore told the owner that three legal kinds
were illegal, on the one screen whose job is to say which kinds exist.

The assertion is derived from ``CASH_MOVEMENT_KINDS`` rather than written out, so the list
cannot go stale a second time: registering an eighth kind and forgetting the message fails
here. ``cash_kinds.py``'s own docstring already states the rule this enforces — "registering
a new kind means adding it here AND to ``data_ingestion/validate.py``'s allowed set".
"""

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.validate import CASH_MOVEMENT_KINDS

_BAD_KIND = {"account_id": "tw_broker", "date": "2026-07-01", "kind": "transfer",
             "ccy": "TWD", "amount": "100"}


def _message(client: TestClient) -> str:
    r = client.post("/api/cash/movements", json=_BAD_KIND)
    assert r.status_code == 400, r.text
    body = r.json()["error"]
    assert body["field"] == "kind", body
    return str(body["message"])


def test_every_registered_kind_is_named(api_client: TestClient) -> None:
    msg = _message(api_client)
    missing = sorted(k for k in CASH_MOVEMENT_KINDS if k.lower() not in msg.lower())
    assert not missing, (
        f"the rejection names only a subset of the registered kinds — missing {missing} "
        f"in: {msg}")


def test_the_rejection_still_quotes_what_the_owner_typed(api_client: TestClient) -> None:
    """Whatever the list says, the message must still say WHICH value was rejected."""
    assert "transfer" in _message(api_client)


def test_the_guard_is_not_vacuous() -> None:
    """A vocabulary of four would still pass a test that asserts nothing about the count."""
    assert len(CASH_MOVEMENT_KINDS) == 7, sorted(CASH_MOVEMENT_KINDS)
