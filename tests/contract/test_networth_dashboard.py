"""FU-D29 net worth — the cross-endpoint consistency contract (deferred C8).

Drives the rich spec-17 scenario through the REAL API and pins the new trend
``net_worth`` field against the already-verified ``GET /api/cash`` reporting total:
on the last cash-complete day, ``net_worth − total_value`` must equal exactly the
reporting-currency cash total the cash endpoint serves (same seeded DB, both paths).
"""

from decimal import Decimal
from typing import Any

from tests.conftest import DashboardClientFactory
from tests.contract.test_spec17_financials import seed_full


def _dash(factory: DashboardClientFactory) -> dict[str, Any]:
    r = factory(seed_full).get("/api/dashboard")
    assert r.status_code == 200
    body: dict[str, Any] = r.json()
    return body


def test_trend_points_carry_net_worth_as_decimal_string(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    points = _dash(dashboard_client_factory)["trend"]["points"]
    assert points, "golden scenario must produce a trend"
    for p in points:
        assert "net_worth" in p  # additive wire field on every point
        assert p["net_worth"] is None or isinstance(p["net_worth"], str)


def test_last_complete_net_worth_matches_cash_endpoint(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(seed_full)
    cash_total = Decimal(client.get("/api/cash").json()["reporting_total"])
    points = client.get("/api/dashboard").json()["trend"]["points"]
    # Newest day whose net worth is known (cash complete) — seed_full's terminal day.
    last = next(p for p in reversed(points) if p["net_worth"] is not None)
    # net_worth − total_value is exactly the reporting-currency cash of that day, which
    # on the terminal day equals the verified cash_balances-derived /api/cash total.
    assert Decimal(last["net_worth"]) - Decimal(last["total_value"]) == cash_total


def test_last_point_is_the_terminal_day_and_complete(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    points = _dash(dashboard_client_factory)["trend"]["points"]
    # seed_full has current FX for every cash pool -> the terminal day is cash-complete.
    assert points[-1]["net_worth"] is not None
