"""F-16 counter-evidence: the oversell block must name the DAY that broke, not the net total.

The guard itself is right, and deliberately DATE-AWARE (domain-ledger.md, 2026-07-31: a
net-only check let a back-dated sell through whenever a LATER buy covered it). Its message
was not. It reported ``h.shares`` — the position's FINAL net quantity after the whole ledger
has replayed — under the word 賣超.

Measured 2026-08-27: editing a 2026-04-01 buy down to 0.5 shares stranded a 2026-04-20 sell
and produced 「此更正將造成賣超（TSLA 部位將為 9.5 股）」. Positive nine and a half shares,
offered as evidence of a shortfall, with no mention of the only date that matters. A reader
who trusts the number concludes the guard is broken; a reader who trusts the guard has to
find the day themselves.

The date comes FROM the replay (``Holding.oversold_on``), not from a second walk over the
transactions here: stock dividends add shares and corporate actions re-denominate them, so
any independent re-derivation would be a second, partial owner of the replay's own ordering.
"""

from fastapi.testclient import TestClient

from tests.contract.test_ledgers_mutations_api import _commit_tx


def _strand_a_later_sell(api_client: TestClient) -> dict[str, object]:
    """Buy 1,000 on 06-11, sell 1,500 on 06-20 (covered by the golden opening), then shrink
    the buy so the 06-20 sell is no longer covered ON ITS OWN DATE."""
    buy_id = _commit_tx(api_client, shares="1000", price="600", date="2026-06-11")
    _commit_tx(api_client, side="sell", shares="1500", price="610", date="2026-06-20")
    r = api_client.put(f"/api/ledgers/transactions/{buy_id}", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-06-11", "shares": "10", "price": "600",
        "fee": "20", "tax": "0", "ack_oversell": False})
    assert r.status_code == 422, r.text
    err: dict[str, object] = r.json()["error"]
    assert err["code"] == "oversell"
    return err


def test_the_message_names_the_offending_date(api_client: TestClient) -> None:
    err = _strand_a_later_sell(api_client)
    message = str(err["message"])
    assert "2026-06-20" in message, f"the day that broke is not named: {message}"


def test_the_message_names_the_symbol_and_stays_actionable(
    api_client: TestClient
) -> None:
    err = _strand_a_later_sell(api_client)
    message = str(err["message"])
    assert "2330" in message
    # The quantities that make the sentence a fact rather than a claim: what was sold that
    # day, and what was actually held that day.
    assert "1500" in message and "10" in message, message


def test_the_ack_path_is_untouched(api_client: TestClient) -> None:
    """Wording only. A message change must not quietly relax what the guard blocks."""
    buy_id = _commit_tx(api_client, shares="1000", price="600", date="2026-06-11")
    _commit_tx(api_client, side="sell", shares="1500", price="610", date="2026-06-20")
    body = {"account_id": "tw_broker", "symbol": "2330", "side": "buy",
            "date": "2026-06-11", "shares": "10", "price": "600",
            "fee": "20", "tax": "0", "ack_oversell": True}
    assert api_client.put(
        f"/api/ledgers/transactions/{buy_id}", json=body).status_code == 200
    assert api_client.get("/api/dashboard").status_code == 200   # never-500 invariant
