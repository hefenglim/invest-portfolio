"""DEF-010 (owner ruling 2026-09-24): a fee discounted at settlement earns no refund forecast.

``discount`` and ``rebate_rate`` record ONE broker benefit two ways (``markets-and-fees.md``).
Ten demo trades were booked under a rule set with BOTH on (snapshot ``discount 0.23`` +
``rebate_rate 0.77``), and ``GET /api/rebates`` forecast a 77 % refund on each — e.g. the
2026-02-10 3008 trade, fee 199 (already 23 % of the full fee), expected 153. Every forecaster
read ``rebate_rate`` and never asked how the fee had been charged. They were three: the inbox
(pending + accruing), the manual draft's ``rebate_estimate`` hint, and the rebalance plan's
``rebate_estimate_total`` (which the print report re-prints).

Why nothing caught it: the fee-rule API's ``conflicts`` banner flags the configuration, but
no forecaster test ever put a discounted fee in front of a forecaster — every fixture used
the seed rule (``discount 1``). The class fix makes ``discount`` a REQUIRED argument of the
one forecasting function, so a fourth forecaster cannot forget it.
"""

from __future__ import annotations

import inspect
import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.fees import booked_discount, forecast_tw_rebate
from portfolio_dash.data_ingestion.store import insert_transaction
from portfolio_dash.shared.models.enums import Side

_DOUBLE = {"engine": "v2", "brokerage": "0.001425", "discount": "0.23", "min_fee": "20",
           "rebate_rate": "0.77", "rounding": "floor"}
_CHARGE_FIRST = {**_DOUBLE, "discount": "1"}


def _trade(conn: sqlite3.Connection, d: date, fee: str, snapshot: dict[str, str]) -> None:
    insert_transaction(
        conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
        quantity=Decimal("1000"), price=Decimal("500"), fees=Decimal(fee),
        tax=Decimal("0"), trade_date=d, fee_rule_snapshot=snapshot)


def _month(body: dict[str, Any], section: str, month: str) -> dict[str, Any] | None:
    return next((r for r in body[section]
                 if r["account_id"] == "tw_broker" and r["month"] == month), None)


def test_the_inbox_forecasts_only_the_charge_first_trade(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """Pending month 2026-05: the discounted trade is neither forecast NOR counted."""
    _trade(golden_db, date(2026, 5, 4), "199", _DOUBLE)       # the demo's #45 shape
    _trade(golden_db, date(2026, 5, 20), "142", _CHARGE_FIRST)
    body = api_client.get("/api/rebates").json()
    may = _month(body, "rows", "2026-05")
    assert may is not None
    assert may["trade_count"] == 1, may
    assert may["fee_total"] == "142" and may["expected"] == "109", may
    assert [t["fee"] for t in may["trades"]] == ["142"]


def test_a_month_of_only_discounted_trades_is_not_forecast_at_all(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """Both sections: a pending month and the accruing (current) month."""
    _trade(golden_db, date(2026, 4, 10), "419", _DOUBLE)      # pending (GOLDEN_NOW is 06-11)
    _trade(golden_db, date(2026, 6, 3), "199", _DOUBLE)       # accruing
    body = api_client.get("/api/rebates").json()
    assert _month(body, "rows", "2026-04") is None, body["rows"]
    assert _month(body, "accruing", "2026-06") is None, body["accruing"]
    assert api_client.get("/api/rebates/count").json()["count"] == 0


def test_the_snapshot_is_the_authority_and_the_rule_set_only_its_fallback(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """A snapshot that records its discount keeps its answer when the rule set changes; one
    that records none (a broker statement's supplied fee) takes the CURRENT rule's."""
    _trade(golden_db, date(2026, 5, 5), "142", _CHARGE_FIRST)
    _trade(golden_db, date(2026, 5, 6), "156", {"engine": "supplied", "fee": "156", "tax": "0"})
    may = _month(api_client.get("/api/rebates").json(), "rows", "2026-05")
    assert may is not None and may["trade_count"] == 2 and may["expected"] == "229"
    api_client.put("/api/fee-rules/tw", json={"overrides": {"discount": "0.23"}})
    may = _month(api_client.get("/api/rebates").json(), "rows", "2026-05")
    assert may is not None and may["trade_count"] == 1, may
    assert [t["fee"] for t in may["trades"]] == ["142"]


def test_the_manual_draft_hint_is_null_when_the_rule_discounts_at_settlement(
    api_client: TestClient,
) -> None:
    body = {"account_id": "tw_broker", "symbol": "2330", "side": "buy",
            "date": "2026-06-10", "shares": "1000", "price": "500"}
    assert api_client.post("/api/input/manual/preview", json=body).json()[
        "rebate_estimate"] == "548"
    api_client.put("/api/fee-rules/tw", json={"overrides": {"discount": "0.23"}})
    r = api_client.post("/api/input/manual/preview", json=body).json()
    assert r["fee"] == "163"               # floor(500,000 × 0.001425 × 0.23) — charged cut
    assert r["rebate_estimate"] is None    # …and no refund on top of it


def test_the_rebalance_plan_forecasts_no_refund_on_a_discounted_leg(
    api_client: TestClient,
) -> None:
    targets = {"targets": {"2330": "0.30", "AAPL": "0.70"}}
    before = api_client.post("/api/rebalance/preview", json=targets).json()
    assert before["summary"]["rebate_estimate_total"] is not None
    api_client.put("/api/fee-rules/tw", json={"overrides": {"discount": "0.23"}})
    after = api_client.post("/api/rebalance/preview", json=targets).json()
    assert after["summary"]["rebate_estimate_total"] is None


def test_the_forecaster_cannot_be_called_without_the_settlement_discount() -> None:
    """The seam: ``discount`` is keyword-only with NO default — forgetting it is a TypeError
    (and a mypy error), never a silent ``1``."""
    param = inspect.signature(forecast_tw_rebate).parameters["discount"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        forecast_tw_rebate(Decimal("199"), Decimal("0.77"))  # type: ignore[call-arg]
    assert forecast_tw_rebate(Decimal("199"), Decimal("0.77"), discount=Decimal("0.23")) == 0
    assert forecast_tw_rebate(Decimal("199"), Decimal("0.77"), discount=Decimal("1")) == 153


@pytest.mark.parametrize(("snapshot", "expected"), [
    ({"discount": "0.23"}, Decimal("0.23")),
    ({"discount": "1"}, Decimal("1")),
    ({}, Decimal("0.5")),                              # silent snapshot → the rule set
    ({"engine": "supplied", "fee": "1"}, Decimal("0.5")),
    ({"discount": "not-a-number"}, Decimal("0.5")),    # never guessed
    ({"discount": "NaN"}, Decimal("0.5")),
])
def test_booked_discount_reads_the_snapshot_first(
    snapshot: dict[str, str], expected: Decimal,
) -> None:
    assert booked_discount(snapshot, fallback=Decimal("0.5")) == expected
