"""Every money/Decimal wire field renders via the canonical encoder (format(d, "f")).

These guard the Task-2 migration: no endpoint may regress to ``str(Decimal)`` (which can
emit scientific notation) or ``normalize()`` (which strips trailing zeros). We test the
directly-callable wire mappers with Decimals that would differ under the old paths
(sci-notation, trailing zeros, tiny rates), plus endpoint-level checks where the golden
data flows through a migrated route.
"""

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from portfolio_dash.api.routers.symbol import _realized_wire
from portfolio_dash.api.wire import fee_rules_wire
from portfolio_dash.data_ingestion.config_seed import FeeRuleSet
from portfolio_dash.portfolio.results import RealizedRow
from portfolio_dash.shared.enums import Currency, Market


def _no_sci(value: str) -> bool:
    return "E" not in value and "e" not in value


def test_fee_rules_wire_renders_tiny_rate_fixed_point() -> None:
    # brokerage 0.0001425 etc. -> never scientific; trailing zeros preserved as stored.
    r = FeeRuleSet(
        market=Market.TW,
        brokerage=Decimal("1E-7"),  # would be "1E-7" under str()
        discount=Decimal("1.00"),
        min_fee=Decimal("20"),
        tax_normal=Decimal("0.0030"),
        tax_etf=Decimal("0.0010"),
    )
    w = fee_rules_wire(r)
    assert w["rate"] == "0.0000001"
    assert w["discount"] == "1.00"
    assert w["tax_sell"] == "0.0030"
    assert w["tax_sell_etf"] == "0.0010"
    for key in ("rate", "discount", "min_fee", "tax_sell", "tax_sell_etf"):
        assert _no_sci(w[key])


def test_realized_wire_preserves_trailing_zero_no_sci() -> None:
    row = RealizedRow(
        account_id="schwab", symbol="AAPL", quote_ccy=Currency.USD,
        sell_date=date(2026, 6, 1),
        shares_sold=Decimal("10.0"),
        proceeds_net=Decimal("1200.50"),
        original_cost_removed=Decimal("1E+2"),
        adjusted_cost_removed=Decimal("1000.00"),
        realized=Decimal("200.50"),
    )
    w = _realized_wire(row)
    assert w["shares_sold"] == "10.0"          # trailing zero preserved
    assert w["proceeds_net"] == "1200.50"
    assert w["original_cost_removed"] == "100"  # 1E+2 expanded, not scientific
    assert w["adjusted_cost_removed"] == "1000.00"
    for key in ("shares_sold", "proceeds_net", "original_cost_removed",
                "adjusted_cost_removed", "realized"):
        assert _no_sci(w[key])


def test_input_manual_preview_preserves_full_precision_total(api_client: TestClient) -> None:
    # 1000 * 612.5 -> Decimal("612500.0"): full precision stays on the wire (no normalize).
    r = api_client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy",
        "date": "2026-06-11", "shares": "1000", "price": "612.5"})
    b = r.json()
    assert b["gross"] == "612500.0"
    assert b["total"] == "-613372.0"  # fee-engine v2 floor: fee 872 (was 873)
    assert _no_sci(b["gross"]) and _no_sci(b["total"])


def test_ledgers_transactions_money_fields_fixed_point(api_client: TestClient) -> None:
    body = api_client.get("/api/ledgers/transactions").json()
    for row in body["rows"]:
        for key in ("shares", "price", "fee", "tax", "total"):
            assert _no_sci(row[key])


def test_dashboard_quota_and_spark_fixed_point(api_client: TestClient) -> None:
    body = api_client.get("/api/dashboard").json()
    assert _no_sci(body["llm_quota"]["remaining_usd"])
    for h in body["holdings"]:
        for pt in h["spark_30d"]:
            assert _no_sci(pt)
