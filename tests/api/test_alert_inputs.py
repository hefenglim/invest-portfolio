"""The api/alert_inputs feeding seam: it must read stored prices + consensus snapshots +
target config and feed the PURE engine so drawdown ① / vol_spike ② / consensus ④ fire from
REAL data (strategy/ never reads pricing itself)."""

import sqlite3
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.api import alert_inputs
from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_corporate_action,
    insert_transaction,
    set_instrument_archived,
    upsert_instrument,
)
from portfolio_dash.pricing import consensus_source, snapshots_store
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.strategy.rules_config import ensure_alert_rules_seeded

NOW = datetime(2026, 7, 13, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    create_pricing_tables(c)
    snapshots_store.ensure_tables(c)
    ensure_alert_rules_seeded(c)  # the app bootstrap seeds this; do the same here
    seed_accounts(c)  # likewise — the EXCHANGE cases below need a real account id
    upsert_instrument(c, Instrument(
        symbol="TEST", market=Market.TW, quote_ccy=Currency.TWD, sector="Tech",
        name="Test Co", board="TWSE", is_etf=False))
    yield c
    c.close()


def _seed_declining_prices(
    conn: sqlite3.Connection, n: int = 260, *, symbol: str = "TEST"
) -> None:
    """A monotonic decline 100 -> ~70 so the 52-week high is 100 and current ≈ −30%."""
    end = NOW.date()
    rows = [
        PriceRow(instrument=symbol, market=Market.TW, as_of=end - timedelta(days=n - 1 - i),
                 close=Decimal("100") - Decimal(i) * Decimal("0.12"), volume=None, source="test")
        for i in range(n)
    ]
    upsert_prices(conn, rows, fetched_at=NOW)


def _seed_consensus(conn: sqlite3.Connection, *, days_ago: int, score: str, mean: str) -> None:
    snapshots_store.add_snapshot(
        conn, source=consensus_source.SOURCE, dataset=consensus_source.DATASET, symbol="TEST",
        as_of=(NOW.date() - timedelta(days=days_ago)),
        payload={"rating_score": score, "price_targets": {"mean": mean}},
        fetched_at=NOW - timedelta(days=days_ago))


def test_assemble_reads_drawdown_from_prices(conn: sqlite3.Connection) -> None:
    _seed_declining_prices(conn)
    from portfolio_dash.portfolio.dashboard import build_dashboard
    data = build_dashboard(conn, now=NOW, reporting=Currency.TWD)
    fed = alert_inputs.assemble(conn, data, now=NOW)
    m = fed.symbol_metrics["TEST"]
    assert m.held is False  # registered but no position -> watch
    assert m.pct_from_52w_high is not None and m.pct_from_52w_high < Decimal("-0.25")
    assert m.window_days > 200


def test_full_engine_fires_drawdown_from_real_prices(conn: sqlite3.Connection) -> None:
    _seed_declining_prices(conn)
    alerts = alert_inputs.compute_alerts_full(conn, now=NOW, reporting=Currency.TWD)
    assert any(a.id == "drawdown_from_peak:TEST" and a.sev == "risk" for a in alerts)


def test_consensus_delta_uses_seven_day_older_baseline(conn: sqlite3.Connection) -> None:
    # latest (today): worse rating + cut target; baseline 10 days older.
    _seed_consensus(conn, days_ago=0, score="4.0", mean="80")
    _seed_consensus(conn, days_ago=10, score="3.0", mean="100")
    deltas = alert_inputs._consensus_deltas(conn, ["TEST"], now=NOW)
    d = deltas["TEST"]
    assert d.score_now == Decimal("4.0") and d.score_then == Decimal("3.0")
    assert d.target_mean_now == Decimal("80") and d.target_mean_then == Decimal("100")
    assert d.days_apart == 10


def test_consensus_silent_without_old_enough_baseline(conn: sqlite3.Connection) -> None:
    # Two snapshots only 3 days apart -> no baseline >= 7 days older -> omitted.
    _seed_consensus(conn, days_ago=0, score="4.0", mean="80")
    _seed_consensus(conn, days_ago=3, score="3.0", mean="100")
    deltas = alert_inputs._consensus_deltas(conn, ["TEST"], now=NOW)
    assert "TEST" not in deltas


def test_full_engine_fires_consensus_change(conn: sqlite3.Connection) -> None:
    _seed_consensus(conn, days_ago=0, score="4.0", mean="80")
    _seed_consensus(conn, days_ago=10, score="3.0", mean="100")
    alerts = alert_inputs.compute_alerts_full(conn, now=NOW, reporting=Currency.TWD)
    cc = next(a for a in alerts if a.id == "consensus_change:TEST")
    assert cc.sev == "info"


# --- FU-D28 target_cross feeding seam ----------------------------------------


def test_assemble_feeds_target_levels_only_for_configured_symbols(
    conn: sqlite3.Connection,
) -> None:
    # No target set -> the symbol is omitted from target_levels (the rule reads nothing).
    from portfolio_dash.portfolio.dashboard import build_dashboard
    _seed_declining_prices(conn)
    data = build_dashboard(conn, now=NOW, reporting=Currency.TWD)
    assert "TEST" not in alert_inputs.assemble(conn, data, now=NOW).target_levels
    # Configure a floor above the latest declining price -> the level is fed with the price.
    upsert_instrument(conn, Instrument(
        symbol="TEST", market=Market.TW, quote_ccy=Currency.TWD, sector="Tech",
        name="Test Co", board="TWSE", target_low=Decimal("95")))
    lv = alert_inputs.assemble(conn, data, now=NOW).target_levels["TEST"]
    assert lv.target_low == Decimal("95") and lv.target_high is None
    assert lv.price is not None and lv.price < Decimal("95")  # latest close is well below 95


def test_full_engine_fires_target_cross_from_real_price(conn: sqlite3.Connection) -> None:
    _seed_declining_prices(conn)  # latest close ≈ 69 (100 - 259*0.12)
    upsert_instrument(conn, Instrument(
        symbol="TEST", market=Market.TW, quote_ccy=Currency.TWD, sector="Tech",
        name="Test Co", board="TWSE", target_low=Decimal("95"), target_high=Decimal("200")))
    alerts = alert_inputs.compute_alerts_full(conn, now=NOW, reporting=Currency.TWD)
    # price far below the 95 floor -> the low leg fires; the 200 ceiling is untouched.
    assert any(a.id == "target_cross:TEST:low" and a.sev == "warn" for a in alerts)
    assert not any(a.id == "target_cross:TEST:high" for a in alerts)


# --- M1-05 (2026-09-06): 封存 and a superseded EXCHANGE source are OUT of the alert universe


def _register_us(conn: sqlite3.Connection, symbol: str) -> None:
    upsert_instrument(conn, Instrument(
        symbol=symbol, market=Market.US, quote_ccy=Currency.USD, sector="Tech",
        name=symbol, is_etf=False))


def _full(conn: sqlite3.Connection) -> tuple[alert_inputs.AlertInputs, list[str]]:
    from portfolio_dash.portfolio.dashboard import build_dashboard
    data = build_dashboard(conn, now=NOW, reporting=Currency.TWD)
    fed = alert_inputs.assemble(conn, data, now=NOW)
    fired = [a.id for a in alert_inputs.compute_alerts_full(conn, now=NOW, reporting=Currency.TWD)]
    return fed, fired


def test_assemble_skips_archived_instruments(conn: sqlite3.Connection) -> None:
    """封存 must mean ONE thing. ``scheduler/jobs.py::build_worklist`` already excludes an
    archived symbol from the price refresh and the signal scan; this seam fed every registered
    row regardless, so an archived symbol — never fetched again — kept firing the drawdown it
    froze at, for as long as the 400-day window still held its last closes (~13 months).

    ``TEST`` is the control: registered, UN-HELD, NOT archived — a watch-list symbol, which by
    design (strategy/alerts.py ①, settings-alerts.js) still gets drawdown alerts."""
    _register_us(conn, "OLD")
    assert set_instrument_archived(conn, "OLD", True)
    _seed_declining_prices(conn, symbol="OLD")
    _seed_declining_prices(conn)  # TEST
    _seed_consensus(conn, days_ago=0, score="4.0", mean="80")
    fed, fired = _full(conn)
    assert "OLD" not in fed.symbol_metrics
    assert "drawdown_from_peak:OLD" not in fired
    # counter-proof: the un-held, un-archived watch symbol is still fed and still fires
    assert "TEST" in fed.symbol_metrics and fed.symbol_metrics["TEST"].held is False
    assert "drawdown_from_peak:TEST" in fired


def test_archived_symbol_gets_no_consensus_delta_either(conn: sqlite3.Connection) -> None:
    """Same flag, same meaning for rule ④: consensus is keyed off the same registered list."""
    _register_us(conn, "OLD")
    assert set_instrument_archived(conn, "OLD", True)
    for days_ago, score, mean in ((0, "4.0", "80"), (10, "2.0", "100")):
        snapshots_store.add_snapshot(
            conn, source=consensus_source.SOURCE, dataset=consensus_source.DATASET,
            symbol="OLD", as_of=NOW.date() - timedelta(days=days_ago),
            payload={"rating_score": score, "price_targets": {"mean": mean}},
            fetched_at=NOW - timedelta(days=days_ago))
    fed, fired = _full(conn)
    assert "OLD" not in fed.consensus_deltas
    assert "consensus_change:OLD" not in fired


def _exchange_mrca_into_nvsa(conn: sqlite3.Connection) -> None:
    """MRCA → NVSA 2:7 (the fixture's shape): the old ticker can never hold a position again."""
    _register_us(conn, "MRCA")
    _register_us(conn, "NVSA")
    insert_transaction(conn, account_id="schwab", symbol="MRCA", side=Side.BUY,
                       quantity=Decimal("70"), price=Decimal("10"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2025, 11, 3))
    insert_corporate_action(conn, account_id="schwab", action_date=date(2026, 1, 9),
                            kind=CorporateActionKind.EXCHANGE, from_symbol="MRCA",
                            to_symbol="NVSA", ratio_to=Decimal("2"), ratio_from=Decimal("7"))
    _seed_declining_prices(conn, symbol="MRCA")
    _seed_declining_prices(conn, symbol="NVSA")


def test_assemble_skips_the_source_of_an_exchange_that_holds_nothing(
    conn: sqlite3.Connection,
) -> None:
    """The fixture's MRCA (exchanged into NVSA 2021-12-09) still fired 「自高點回撤」 in 2026:
    its stored history is a dead series for a ticker that no longer trades under that name.
    The destination is the live position and stays fed."""
    _exchange_mrca_into_nvsa(conn)
    fed, fired = _full(conn)
    assert "MRCA" not in fed.symbol_metrics
    assert "drawdown_from_peak:MRCA" not in fired
    assert "NVSA" in fed.symbol_metrics and fed.symbol_metrics["NVSA"].held is True
    assert "drawdown_from_peak:NVSA" in fired


def test_an_exchange_source_bought_again_is_held_and_still_fed(
    conn: sqlite3.Connection,
) -> None:
    """Counter-proof: the exclusion is 「EXCHANGE source AND no position」, not 「ever an
    EXCHANGE source」. A ticker re-bought after the exchange carries a live position again."""
    _exchange_mrca_into_nvsa(conn)
    insert_transaction(conn, account_id="schwab", symbol="MRCA", side=Side.BUY,
                       quantity=Decimal("5"), price=Decimal("10"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 3, 2))
    fed, fired = _full(conn)
    assert "MRCA" in fed.symbol_metrics and fed.symbol_metrics["MRCA"].held is True
    assert "drawdown_from_peak:MRCA" in fired
