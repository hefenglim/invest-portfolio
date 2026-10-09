"""M2 / QA-01 + QA-02 on the WIRE: ``GET /api/dashboard`` keeps the FX section, and labels it.

The unit-level proof lives in ``tests/forex/test_m2_fx_summary_degrades_per_account.py``;
this file pins the two things only the full stack can show:

* the whole ``fx`` block (and both FX KPIs) used to VANISH because an empty ``moomoo_my``
  had no ``MYR/TWD`` rate — with no reason string anywhere on the payload (QA-01);
* ``kpis.fx_unrealized`` used to carry a PARTIAL total labelled as complete (QA-02).

Every money figure is asserted as a Decimal **string** (the locked wire contract) and
re-derived by hand in each docstring.
"""

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_fx_conversion,
    insert_transaction,
    upsert_instrument,
)
from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.pricing.store import upsert_fx, upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import GOLDEN_NOW, DashboardClientFactory


def _base(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="AAPL", market=Market.US,
                                       quote_ccy=Currency.USD, sector="Tech", name="Apple"))


def _buy(conn: sqlite3.Connection, account_id: str, qty: str, on: date) -> None:
    insert_transaction(conn, account_id=account_id, symbol="AAPL", side=Side.BUY,
                       quantity=Decimal(qty), price=Decimal("100"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=on)


def _dashboard(client: TestClient) -> dict[str, Any]:
    response = client.get("/api/dashboard")
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


# --- QA-01: one EMPTY account must not take the section down ----------------------------


def _seed_one_funded_pool(conn: sqlite3.Connection) -> None:
    """schwab only: TWD 300,000 -> USD 10,000, then 100 sh of AAPL at 100.

    ``moomoo_my`` is seeded as an account but holds NOTHING, and neither ``MYR/TWD`` nor
    ``USD/MYR`` has a single row.
    """
    _base(conn)
    insert_fx_conversion(conn, account_id="schwab", date=date(2025, 6, 11),
                         from_ccy=Currency.TWD, from_amount=Decimal("300000"),
                         to_ccy=Currency.USD, to_amount=Decimal("10000"))
    _buy(conn, "schwab", "100", date(2025, 6, 11))
    upsert_prices(conn, [PriceRow(instrument="AAPL", market=Market.US, as_of=date(2026, 6, 9),
                                  close=Decimal("110"), source="test")],
                  fetched_at=GOLDEN_NOW)
    upsert_fx(conn, [FxRow(base=Currency.USD, quote=Currency.TWD, as_of=date(2026, 6, 9),
                           rate=Decimal("33"), source="test")], fetched_at=GOLDEN_NOW)


def test_an_empty_account_no_longer_voids_the_fx_section(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """Hand-derived: pool avg 300,000/10,000 = 30; cash 10,000 - 100x100 = 0; stock
    100 sh x 110 = 11,000 USD; spot 33 -> 11,000 x (33 - 30) = **33,000 TWD**.
    """
    body = _dashboard(dashboard_client_factory(_seed_one_funded_pool))

    fx = body["fx"]
    assert fx is not None, "the whole FX section was dropped by an EMPTY account (QA-01)"
    assert body["kpis"]["fx_unrealized"] == "33000"
    assert body["kpis"]["fx_realized"] == "0"
    assert fx["by_account"]["schwab"]["unrealized_fx_total"] == "33000"
    # Nothing was lost, so nothing is excluded and there is nothing to explain.
    assert fx["excluded_accounts"] == []
    assert fx["reporting_unavailable_reason"] is None
    assert body["freshness"]["fx_unavailable_reason"] is None


# --- QA-02: a partial rollup must say it is partial --------------------------------------


def _seed_two_funded_pools(conn: sqlite3.Connection, *, usd_myr: bool) -> None:
    """schwab (TWD-anchored, avg 32) and moomoo_my (MYR-anchored, avg 4.4), both funded.

    Each account: 10,000 USD acquired, 50 sh of AAPL at 100 bought, close 120.
    ``usd_myr`` decides whether moomoo_my's own ``USD/MYR`` spot exists at all.
    """
    _base(conn)
    insert_fx_conversion(conn, account_id="schwab", date=date(2026, 1, 5),
                         from_ccy=Currency.TWD, from_amount=Decimal("320000"),
                         to_ccy=Currency.USD, to_amount=Decimal("10000"))
    insert_fx_conversion(conn, account_id="moomoo_my", date=date(2026, 1, 5),
                         from_ccy=Currency.MYR, from_amount=Decimal("44000"),
                         to_ccy=Currency.USD, to_amount=Decimal("10000"))
    _buy(conn, "schwab", "50", date(2026, 1, 10))
    _buy(conn, "moomoo_my", "50", date(2026, 1, 10))
    upsert_prices(conn, [PriceRow(instrument="AAPL", market=Market.US, as_of=date(2026, 6, 9),
                                  close=Decimal("120"), source="test")],
                  fetched_at=GOLDEN_NOW)
    rows = [FxRow(base=Currency.USD, quote=Currency.TWD, as_of=date(2026, 6, 9),
                  rate=Decimal("34"), source="test"),
            FxRow(base=Currency.MYR, quote=Currency.TWD, as_of=date(2026, 6, 9),
                  rate=Decimal("7.1"), source="test")]
    if usd_myr:
        rows.append(FxRow(base=Currency.USD, quote=Currency.MYR, as_of=date(2026, 6, 9),
                          rate=Decimal("4.8"), source="test"))
    upsert_fx(conn, rows, fetched_at=GOLDEN_NOW)


def test_a_partial_fx_rollup_names_the_excluded_account_and_the_missing_pair(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """22,000 is schwab's half alone — hand-derived: (5,000 cash + 6,000 stock) x (34 - 32).

    moomoo_my holds the same 11,000 USD of exposure but has no USD/MYR rate, so the wire
    figure is PARTIAL and must be labelled as such rather than presented as the total.
    """
    client = dashboard_client_factory(lambda c: _seed_two_funded_pools(c, usd_myr=False))
    body = _dashboard(client)

    unrealized = body["kpis"]["fx_unrealized"]
    assert isinstance(unrealized, str)            # Decimal STRING, never a JSON number
    assert Decimal(unrealized) == Decimal("22000")
    fx = body["fx"]
    assert fx["excluded_accounts"] == ["moomoo_my"]
    reason = fx["reporting_unavailable_reason"]
    assert reason is not None
    assert "moomoo_my" in reason and "USD/MYR" in reason and "未實現" in reason
    assert fx["by_account"]["moomoo_my"]["unrealized_fx_total"] is None
    # The last-resort bail-out is NOT what produced this: the section is present.
    assert body["freshness"]["fx_unavailable_reason"] is None


def test_seeding_the_missing_pair_restores_the_complete_fx_total(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The same ledger with USD/MYR = 4.8 present.

    Hand-derived: moomoo_my 11,000 USD x (4.8 - 4.4) = 4,400 MYR; x 7.1 = 31,240 TWD;
    + schwab's 22,000 = **53,240 TWD** — the figure QA-02 measured as 22,000.
    """
    client = dashboard_client_factory(lambda c: _seed_two_funded_pools(c, usd_myr=True))
    body = _dashboard(client)

    unrealized = body["kpis"]["fx_unrealized"]
    assert isinstance(unrealized, str)
    assert Decimal(unrealized) == Decimal("53240")
    fx = body["fx"]
    assert fx["excluded_accounts"] == []
    assert fx["reporting_unavailable_reason"] is None
    assert Decimal(fx["by_account"]["moomoo_my"]["unrealized_fx_total"]) == Decimal("4400")


# --- an empty accumulator is not a measurement -------------------------------------------


def _seed_cold_start(conn: sqlite3.Connection) -> None:
    """One USD holding, NO conversion, NO fx_rates row at all — a brand-new ledger."""
    _base(conn)
    _buy(conn, "schwab", "10", date(2026, 1, 10))
    upsert_prices(conn, [PriceRow(instrument="AAPL", market=Market.US, as_of=date(2026, 6, 9),
                                  close=Decimal("120"), source="test")],
                  fetched_at=GOLDEN_NOW)


def test_a_cold_start_reports_no_fx_section_rather_than_a_fabricated_zero(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """No account has an FX cost basis, so every per-account figure is None.

    The rollup's ``0`` is then the value an empty accumulator STARTS at, not a measured
    one — printing it as 換匯損益 已實現 0 / 未實現 0 would be the same
    "initialised value rendered as a fact" defect the audit catalogues elsewhere. The
    section is absent and now carries a reason, where before it was absent (via the
    QA-01 KeyError) and silent.

    This is the counterpart to ``test_an_empty_account_no_longer_voids_the_fx_section``:
    ONE real figure anywhere keeps the whole section, so this rule can never reproduce
    QA-01's blast radius.
    """
    body = _dashboard(dashboard_client_factory(_seed_cold_start))

    assert body["fx"] is None
    assert body["kpis"]["fx_realized"] is None
    assert body["kpis"]["fx_unrealized"] is None
    reason = body["freshness"]["fx_unavailable_reason"]
    assert reason is not None and "匯兌損益" in reason


# --- the last-resort bail-out still reports itself ---------------------------------------


def test_the_last_resort_bailout_now_carries_a_reason(
    dashboard_client_factory: DashboardClientFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``fx = None`` is kept as a safety net — but it may no longer be SILENT.

    The per-account degradation makes this branch unreachable from missing rates alone, so
    it is forced here. Before this change the payload showed a null FX section with no
    explanation anywhere; ``freshness.fx_unavailable_reason`` now mirrors
    ``xirr_unavailable_reason``, which surfaces the very same resolver message.
    """
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise KeyError("尚無 MYR/TWD 匯率資料")

    monkeypatch.setattr("portfolio_dash.portfolio.dashboard.compute_fx_summary", _boom)
    body = _dashboard(dashboard_client_factory(_seed_one_funded_pool))

    assert body["fx"] is None
    assert body["kpis"]["fx_unrealized"] is None
    assert body["freshness"]["fx_unavailable_reason"] == "尚無 MYR/TWD 匯率資料"
