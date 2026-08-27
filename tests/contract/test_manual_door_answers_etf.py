"""The manual door can now ANSWER the ETF question for a symbol it auto-registers.

AI-D40 made ``is_etf`` three-state and stopped the manual door guessing: a symbol first seen
by TRADING it registers as ``None`` (unknown), and the fee engine meeting ``None`` on a TW SELL
computes with ``False`` but raises the soft issue ``etf_flag_unknown``. That ended the silent
3× overtax — 現股 0.3% charged on an ETF that should pay 0.1% — but it left the answer to be
supplied LATER, after a sell had already surfaced the problem.

Measured on the demo ledger 2026-08-28: `0056` 元大高股息 — an ETF — sits registered as
``is_etf=False`` with 0 sells. Nothing is wrong yet; the first sell is when it becomes wrong.

So the door now carries the answer, given at the moment the user is already looking at the
symbol. Three properties, and the third is the one that matters:

* it reaches ``quick_register`` and lands in the REGISTRY row;
* omitting it is unchanged — still ``None``, still the soft issue (AI-D40 is not relaxed);
* ⚠ it is **NOT a per-trade override**. It is consumed only on the auto-register path, so an
  ALREADY-REGISTERED instrument is untouched by it. That is the 2026-07-15 stress-audit fix
  (an input flag beating the registry) and this field must never re-open it — which is why it
  is named ``new_symbol_is_etf`` rather than ``is_etf``.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.api.routers import input_center
from portfolio_dash.data_ingestion.store import upsert_instrument
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument


def _capturing_register(seen: dict[str, Any]) -> Any:
    def fake(conn: Any, **kw: Any) -> Any:
        from portfolio_dash.api.instrument_service import QuickRegisterOutcome
        seen.update(kw)
        # Mirror what the real `quick_register` does with the tri-state answer: AI-D40 encodes
        # it as the PAIR (is_etf, etf_flag_unknown), not as an Optional bool — so `None` means
        # "unset", never "False".
        answer = kw.get("is_etf")
        inst = Instrument(symbol=kw["symbol"], market=Market.TW, quote_ccy=Currency.TWD,
                          sector="", name="New ETF", board="TWSE",
                          is_etf=bool(answer), etf_flag_unknown=answer is None)
        upsert_instrument(conn, inst)
        return QuickRegisterOutcome(instrument=inst, board="TWSE", last=None,
                                    name_source="provider", history_points=True)
    return fake


def _buy(api_client: TestClient, symbol: str, **extra: Any) -> Any:
    body: dict[str, Any] = {
        "account_id": "tw_broker", "symbol": symbol, "side": "buy",
        "date": "2026-06-11", "shares": "1000", "price": "30"}
    body.update(extra)
    return api_client.post("/api/input/manual/commit", json=body)


def test_the_answer_reaches_the_registry(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(input_center, "quick_register", _capturing_register(seen))
    assert _buy(api_client, "00888", new_symbol_is_etf=True).status_code == 201
    assert seen.get("is_etf") is True, f"the door dropped the answer: {seen}"

    rows = api_client.get("/api/instruments").json()["list"]
    row = {r["symbol"]: r for r in rows}["00888"]
    assert row["is_etf"] is True
    # ...and the "nobody answered" marker is cleared, or the fee engine would still disclose
    # an unknown rate for a symbol the user has just answered for.
    assert not row.get("etf_flag_unknown")


def test_answering_NOT_an_etf_is_also_an_answer(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """False must be stored as False, not collapsed into "unknown" — that is the whole
    point of the three-state flag. A user who says 「this is an ordinary stock」 has answered."""
    seen: dict[str, Any] = {}
    monkeypatch.setattr(input_center, "quick_register", _capturing_register(seen))
    assert _buy(api_client, "2345", new_symbol_is_etf=False).status_code == 201
    assert seen.get("is_etf") is False


def test_omitting_it_still_records_unknown(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AI-D40 unchanged: no answer given → no answer recorded, and no answer invented."""
    seen: dict[str, Any] = {}
    monkeypatch.setattr(input_center, "quick_register", _capturing_register(seen))
    assert _buy(api_client, "9999").status_code == 201
    assert seen.get("is_etf") is None
    assert "is_etf" in seen, "the kwarg must still be passed explicitly, not omitted"


def test_it_never_overrides_an_already_registered_instrument(
    api_client: TestClient, golden_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 2026-07-15 invariant: the REGISTRY is authoritative at the fee seam.

    2330 is registered and is not an ETF. Sending the flag on a trade must not relabel it —
    if it could, the field would be a per-trade override wearing a registration badge, and
    the 3× tax defect returns from the other direction.
    """
    def boom(conn: Any, **kw: Any) -> Any:
        raise AssertionError("a registered symbol must not reach quick_register")

    monkeypatch.setattr(input_center, "quick_register", boom)
    assert _buy(api_client, "2330", new_symbol_is_etf=True).status_code == 201

    rows = api_client.get("/api/instruments").json()["list"]
    by_symbol = {r["symbol"]: r for r in rows}
    assert by_symbol["2330"]["is_etf"] is not True, "a trade relabelled a registered symbol"
