"""P3 batch 2 — the four new market-risk alert rules (pure engine, fed inputs only).

Each rule: trigger · just-below-threshold silent · insufficient-data silent · disabled
silent. Plus the Swedroe 5/25 semantics (absolute leg / relative leg / neither / unset) and
the consensus-change comparison basis (rating worsen / price cut / improvement silent /
missing baseline silent), all hand-computed.
"""

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from portfolio_dash.portfolio.dashboard_models import (
    DashboardData,
    DividendSummary,
    FreshnessReport,
    HoldingRow,
    KpiSummary,
    TrendSeries,
)
from portfolio_dash.portfolio.results import RealizedPnL
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.strategy.alerts import (
    Alert,
    ConsensusDelta,
    SymbolMetric,
    compute_alerts_from,
)
from portfolio_dash.strategy.rules_config import DEFAULT_RULES, AlertRules

_NOW = datetime(2026, 7, 13, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))
_QR = Decimal("5")   # quota_remaining (the quota rule is inert here)
_QT = Decimal("1")   # quota_threshold


def _holding(symbol: str, weight: Decimal) -> HoldingRow:
    """A minimal held row carrying only the fields the drift rule reads (symbol + weight)."""
    return HoldingRow(
        account_id="tw", account_name="TW broker", symbol=symbol, name=symbol,
        market=Market.TW, sector="Tech", board="TWSE", quote_ccy=Currency.TWD,
        shares=Decimal("1000"), original_avg=Decimal("100"), adjusted_avg=Decimal("100"),
        original_cost_total=Decimal("100000"), adjusted_cost_total=Decimal("100000"),
        dividend_portion=Decimal("0"), payback_ratio=Decimal("0"), weight=weight,
    )


def _data(holdings: list[HoldingRow] | None = None) -> DashboardData:
    reporting = Currency.TWD
    return DashboardData(
        as_of=_NOW, reporting_currency=reporting,
        kpis=KpiSummary(reporting_currency=reporting),
        holdings=holdings or [],
        realized=RealizedPnL(rows=[], by_currency={}), returns=None, allocation=None,
        currency_view=None, fx=None,
        dividends=DividendSummary(by_year=[], total_by_currency={}),
        ex_dividend_calendar=[],
        trend=TrendSeries(points=[], reporting_currency=reporting, available=False),
        freshness=FreshnessReport(prices=[], fx=[], any_stale=False,
                                  missing_prices=[], missing_fx=[]),
    )


def _run(
    data: DashboardData,
    rules: AlertRules = DEFAULT_RULES,
    *,
    symbol_metrics: dict[str, SymbolMetric] | None = None,
    target_weights: dict[str, Decimal] | None = None,
    consensus_deltas: dict[str, ConsensusDelta] | None = None,
) -> list[Alert]:
    """Run the pure engine with the (inert) quota args filled in — keeps call sites short."""
    return compute_alerts_from(
        data, rules, quota_remaining=_QR, quota_threshold=_QT,
        symbol_metrics=symbol_metrics, target_weights=target_weights,
        consensus_deltas=consensus_deltas,
    )


# --- ① drawdown_from_peak -----------------------------------------------------


def test_drawdown_risk_at_or_beyond_threshold() -> None:
    # −22% from the 52w high >= 20% default risk threshold -> risk.
    m = {"2330": SymbolMetric(held=True, pct_from_52w_high=Decimal("-0.22"), window_days=252)}
    dd = next(a for a in _run(_data(), symbol_metrics=m) if a.id == "drawdown_from_peak:2330")
    assert dd.sev == "risk" and dd.href == "/symbol/2330"
    assert "22.0%" in dd.detail and "252 日視窗" in dd.detail and "門檻" in dd.detail


def test_drawdown_warn_between_half_and_full_threshold() -> None:
    # −12% is >= half (10%) but < full (20%) -> warn. Watch symbols are included too.
    m = {"AAPL": SymbolMetric(held=False, pct_from_52w_high=Decimal("-0.12"), window_days=252)}
    dd = next(a for a in _run(_data(), symbol_metrics=m) if a.id == "drawdown_from_peak:AAPL")
    assert dd.sev == "warn"


def test_drawdown_silent_below_warn_threshold() -> None:
    # −9% < half (10%) -> silent.
    m = {"2330": SymbolMetric(held=True, pct_from_52w_high=Decimal("-0.09"), window_days=252)}
    assert not any(a.rule == "drawdown_from_peak" for a in _run(_data(), symbol_metrics=m))


def test_drawdown_silent_when_no_history() -> None:
    m = {"2330": SymbolMetric(held=True, pct_from_52w_high=None, window_days=0)}
    assert not any(a.rule == "drawdown_from_peak" for a in _run(_data(), symbol_metrics=m))


def test_drawdown_silent_when_disabled() -> None:
    rules = DEFAULT_RULES.model_copy(deep=True)
    rules.drawdown_from_peak.enabled = False
    m = {"2330": SymbolMetric(held=True, pct_from_52w_high=Decimal("-0.50"), window_days=252)}
    assert not any(a.rule == "drawdown_from_peak"
                   for a in _run(_data(), rules, symbol_metrics=m))


# --- ② vol_spike --------------------------------------------------------------


def test_vol_spike_fires_at_multiple() -> None:
    # 30d vol 0.36 = 2.0x the 90d 0.18 >= 1.8 default -> warn (held only).
    m = {"2330": SymbolMetric(held=True, vol_30d=Decimal("0.36"), vol_90d=Decimal("0.18"))}
    vs = next(a for a in _run(_data(), symbol_metrics=m) if a.id == "vol_spike:2330")
    assert vs.sev == "warn" and "2.00x" in vs.detail and "1.80x" in vs.detail


def test_vol_spike_silent_below_multiple() -> None:
    # 1.5x < 1.8 -> silent.
    m = {"2330": SymbolMetric(held=True, vol_30d=Decimal("0.27"), vol_90d=Decimal("0.18"))}
    assert not any(a.rule == "vol_spike" for a in _run(_data(), symbol_metrics=m))


def test_vol_spike_silent_for_watch_symbol() -> None:
    # Held-only: a watch symbol never fires even with a big ratio.
    m = {"AAPL": SymbolMetric(held=False, vol_30d=Decimal("0.90"), vol_90d=Decimal("0.18"))}
    assert not any(a.rule == "vol_spike" for a in _run(_data(), symbol_metrics=m))


def test_vol_spike_silent_when_window_insufficient() -> None:
    m = {"2330": SymbolMetric(held=True, vol_30d=Decimal("0.4"), vol_90d=None)}
    assert not any(a.rule == "vol_spike" for a in _run(_data(), symbol_metrics=m))


def test_vol_spike_silent_when_disabled() -> None:
    rules = DEFAULT_RULES.model_copy(deep=True)
    rules.vol_spike.enabled = False
    m = {"2330": SymbolMetric(held=True, vol_30d=Decimal("0.9"), vol_90d=Decimal("0.18"))}
    assert not any(a.rule == "vol_spike" for a in _run(_data(), rules, symbol_metrics=m))


# --- ③ rebalance_drift (Swedroe 5/25) -----------------------------------------


def test_drift_absolute_leg_triggers() -> None:
    # current 40%, target 30%: drift 10% > max(5%, 0.25*30%=7.5%) -> risk (absolute leg wins).
    data = _data([_holding("2330", Decimal("0.40"))])
    rd = next(a for a in _run(data, target_weights={"2330": Decimal("0.30")})
              if a.id == "rebalance_drift:2330")
    assert rd.sev == "risk" and "10.0%" in rd.detail and "30.0%" in rd.detail


def test_drift_relative_leg_triggers() -> None:
    # target 10% (small): band = max(5%, 0.25*10%=2.5%) = 5%. current 4% -> drift 6% > 5%.
    data = _data([_holding("2330", Decimal("0.04"))])
    assert any(a.id == "rebalance_drift:2330"
               for a in _run(data, target_weights={"2330": Decimal("0.10")}))


def test_drift_relative_leg_binds_above_absolute() -> None:
    # target 40%: relative band = 0.25*40% = 10% > absolute 5% -> band 10%. drift 8% -> silent
    # (proves the relative leg RAISES the band using the TARGET as base).
    data = _data([_holding("2330", Decimal("0.48"))])
    assert not any(a.rule == "rebalance_drift"
                   for a in _run(data, target_weights={"2330": Decimal("0.40")}))


def test_drift_neither_leg_triggers() -> None:
    # current 32%, target 30%: drift 2% <= max(5%, 7.5%) -> silent.
    data = _data([_holding("2330", Decimal("0.32"))])
    assert not any(a.rule == "rebalance_drift"
                   for a in _run(data, target_weights={"2330": Decimal("0.30")}))


def test_drift_silent_when_no_target() -> None:
    # A big current weight but NO target set -> the drift rule is silent (targets empty).
    data = _data([_holding("2330", Decimal("0.80"))])
    assert not any(a.rule == "rebalance_drift" for a in _run(data, target_weights={}))


def test_drift_silent_when_target_symbol_not_held() -> None:
    # Target set for a symbol with no holding row -> silent (cannot compute drift honestly).
    data = _data([_holding("2330", Decimal("0.40"))])
    assert not any(a.rule == "rebalance_drift"
                   for a in _run(data, target_weights={"AAPL": Decimal("0.10")}))


def test_drift_aggregates_weight_across_accounts() -> None:
    # The same symbol in two accounts (18% + 25% = 43%) vs a 30% target -> drift 13% > 7.5%.
    data = _data([_holding("2330", Decimal("0.18")), _holding("2330", Decimal("0.25"))])
    rd = next(a for a in _run(data, target_weights={"2330": Decimal("0.30")})
              if a.id == "rebalance_drift:2330")
    assert "43.0%" in rd.detail  # aggregated current weight


def test_drift_silent_when_disabled() -> None:
    rules = DEFAULT_RULES.model_copy(deep=True)
    rules.rebalance_drift.enabled = False
    data = _data([_holding("2330", Decimal("0.80"))])
    assert not any(a.rule == "rebalance_drift"
                   for a in _run(data, rules, target_weights={"2330": Decimal("0.10")}))


# --- ④ consensus_change -------------------------------------------------------


def test_consensus_rating_worsening_triggers() -> None:
    # score 3.0 -> 3.6 (+0.6) >= 0.5 default -> info.
    d = {"AAPL": ConsensusDelta(score_now=Decimal("3.6"), score_then=Decimal("3.0"),
                                days_apart=7)}
    cc = next(a for a in _run(_data(), consensus_deltas=d) if a.id == "consensus_change:AAPL")
    assert cc.sev == "info" and "3.00" in cc.detail and "3.60" in cc.detail
    assert "7 日前" in cc.detail


def test_consensus_price_cut_triggers() -> None:
    # mean target 200 -> 178 = −11% >= 10% cut -> info (rating leg absent).
    d = {"AAPL": ConsensusDelta(target_mean_now=Decimal("178"), target_mean_then=Decimal("200"),
                                days_apart=8)}
    cc = next(a for a in _run(_data(), consensus_deltas=d) if a.id == "consensus_change:AAPL")
    assert "目標均價下修" in cc.detail and "11.0%" in cc.detail


def test_consensus_improvement_silent() -> None:
    # score improves (3.6 -> 3.0) and target rises -> silent.
    d = {"AAPL": ConsensusDelta(score_now=Decimal("3.0"), score_then=Decimal("3.6"),
                                target_mean_now=Decimal("210"), target_mean_then=Decimal("200"),
                                days_apart=7)}
    assert not any(a.rule == "consensus_change" for a in _run(_data(), consensus_deltas=d))


def test_consensus_just_below_thresholds_silent() -> None:
    # +0.4 rating (< 0.5) and −9% price (< 10%) -> silent.
    d = {"AAPL": ConsensusDelta(score_now=Decimal("3.4"), score_then=Decimal("3.0"),
                                target_mean_now=Decimal("182"), target_mean_then=Decimal("200"),
                                days_apart=7)}
    assert not any(a.rule == "consensus_change" for a in _run(_data(), consensus_deltas=d))


def test_consensus_silent_when_baseline_missing() -> None:
    # Only a "now" leg present, no "then" -> both legs non-firing -> silent.
    d = {"AAPL": ConsensusDelta(score_now=Decimal("4.5"), score_then=None,
                                target_mean_now=Decimal("100"), target_mean_then=None)}
    assert not any(a.rule == "consensus_change" for a in _run(_data(), consensus_deltas=d))


def test_consensus_silent_when_disabled() -> None:
    rules = DEFAULT_RULES.model_copy(deep=True)
    rules.consensus_change.enabled = False
    d = {"AAPL": ConsensusDelta(score_now=Decimal("4.5"), score_then=Decimal("3.0"),
                                days_apart=7)}
    assert not any(a.rule == "consensus_change"
                   for a in _run(_data(), rules, consensus_deltas=d))


# --- messages carry no account amounts (push discipline) ----------------------


def test_new_rule_messages_are_percentages_only() -> None:
    m = {"2330": SymbolMetric(held=True, pct_from_52w_high=Decimal("-0.25"), window_days=252,
                             vol_30d=Decimal("0.4"), vol_90d=Decimal("0.18"))}
    data = _data([_holding("2330", Decimal("0.5"))])
    alerts = _run(
        data, symbol_metrics=m, target_weights={"2330": Decimal("0.2")},
        consensus_deltas={"2330": ConsensusDelta(
            score_now=Decimal("4.0"), score_then=Decimal("3.0"), days_apart=7)},
    )
    new = [a for a in alerts if a.rule in
           {"drawdown_from_peak", "vol_spike", "rebalance_drift", "consensus_change"}]
    assert len(new) == 4
    for a in new:
        # No currency symbols / amounts leak into a push-bound message.
        assert "NT$" not in a.detail and "$" not in a.detail and "USD" not in a.detail
