"""Contract: 月度成績 (R6 item 8; PERF spec 2026-10-18 §11.4) + inbox count badge endpoint
(item 4).

``GET /api/snapshots`` serves the month closes — past months from the settlement store or
computed live, the current month live — never the retired 23:50 snapshot table. Here the
store is UNUSABLE (the golden database has never been settled): every row is computed live,
never stale and never missing. The store-backed cases are ``tests/settlement/
test_read_path_periods.py``.
"""

import sqlite3
from datetime import date, datetime

from fastapi.testclient import TestClient

from portfolio_dash.api.snapshots import write_snapshot
from tests.conftest import GOLDEN_NOW

#: The 23:50 snapshot's row fields — kept, with their meaning (``total_value`` is KPI 總市值).
_OLD_FIELDS = {"month", "as_of", "reporting_ccy", "total_value", "total_return",
               "total_return_rate", "xirr", "by_currency"}
#: The additive fields of §11.4.
_NEW_FIELDS = {"kind", "provisional", "stale", "notes", "xirr_reason", "xirr_window_days",
               "period_twr", "period_twr_reason", "restated", "current"}


def test_an_unusable_store_serves_every_month_live(api_client: TestClient) -> None:
    body = api_client.get("/api/snapshots").json()
    rows = body["rows"]
    # golden's first row is a buy on 2026-01-05; the clock is 2026-06-11
    assert [r["month"] for r in rows] == [
        "2026-06", "2026-05", "2026-04", "2026-03", "2026-02", "2026-01"]
    assert body["total_count"] == 6
    for r in rows:
        assert set(r) == _OLD_FIELDS | _NEW_FIELDS, r["month"]
        assert r["kind"] == "M" and r["reporting_ccy"] == "TWD"
        assert r["stale"] is False and r["provisional"] is None and r["restated"] is None
        assert isinstance(r["notes"], dict) and isinstance(r["by_currency"], dict)
    current = rows[0]
    assert current["as_of"] == GOLDEN_NOW.isoformat()      # live, as at now
    assert current["current"] is True and not any(r["current"] for r in rows[1:])
    assert current["total_value"] is not None                # golden holds priced positions
    assert "TWD" in current["by_currency"]
    assert rows[1]["as_of"] == "2026-05-31"                  # a past month, as at its end


def test_the_current_month_is_the_dashboards_kpis(api_client: TestClient) -> None:
    dash = api_client.get("/api/dashboard").json()
    row = api_client.get("/api/snapshots", params={"limit": 1}).json()["rows"][0]
    k = dash["kpis"]
    assert (row["total_value"], row["total_return"], row["total_return_rate"], row["xirr"]) == (
        k["total_market_value"], k["total_return"], k["total_return_rate"], k["xirr"])
    # the KPI card's short-window hint and the 月度成績 cell read the same window (MINOR 7b)
    assert row["xirr_window_days"] == k["xirr_window_days"]


def test_a_month_row_carries_its_xirr_window(api_client: TestClient) -> None:
    """Accountant MINOR 7b (owner ruling 2026-10-22): every month row carries its 累計 XIRR's
    observation window, so the cell shows 「觀察期 N 天・短窗參考」 under a year. Golden's first
    row is a buy on 2026-01-05: the live June row (as at 2026-06-11) is 157 days in, and every
    past month with an XIRR counts the days to its own end."""
    rows = {r["month"]: r for r in api_client.get("/api/snapshots").json()["rows"]}
    for r in rows.values():
        assert r["xirr_window_days"] is None or isinstance(r["xirr_window_days"], int)
        if r["xirr"] is not None and not r["current"]:
            end = date.fromisoformat(r["as_of"])
            assert r["xirr_window_days"] == (end - date(2026, 1, 5)).days, r["month"]
    june = rows["2026-06"]
    assert june["xirr"] is not None and june["xirr_window_days"] == 157


def test_a_blank_figure_states_its_reason(api_client: TestClient) -> None:
    """A month before golden has a USD rate: XIRR is blank, with the engine's reason."""
    rows = {r["month"]: r for r in api_client.get("/api/snapshots").json()["rows"]}
    for r in rows.values():
        if r["xirr"] is None:
            assert r["xirr_reason"], r["month"]
        if r["period_twr"] is None:
            assert r["period_twr_reason"], r["month"]


def test_limit_keeps_the_newest_months(api_client: TestClient) -> None:
    body = api_client.get("/api/snapshots", params={"limit": 2}).json()
    assert [r["month"] for r in body["rows"]] == ["2026-06", "2026-05"]
    assert body["total_count"] == 6
    assert api_client.get("/api/snapshots", params={"limit": 0}).status_code == 400


def test_the_retired_snapshot_table_is_never_served(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    write_snapshot(golden_db, now=datetime(2026, 5, 31, 23, 50, tzinfo=GOLDEN_NOW.tzinfo))
    golden_db.execute("UPDATE portfolio_snapshots SET total_value = '1', xirr = '9'")
    golden_db.commit()
    may = next(r for r in api_client.get("/api/snapshots").json()["rows"]
               if r["month"] == "2026-05")
    assert may["total_value"] != "1" and may["xirr"] != "9"


def test_inbox_count_endpoint(api_client: TestClient) -> None:
    r = api_client.get("/api/dividend-inbox/count")
    assert r.status_code == 200
    assert isinstance(r.json()["count"], int)
