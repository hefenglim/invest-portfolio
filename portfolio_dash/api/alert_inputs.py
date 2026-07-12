"""Alert-input assembler (Blueprint P3 batch 2) — the conn-bearing seam that feeds the new
market-risk rules into the PURE alert engine.

``strategy/alerts`` is pure and cannot read ``pricing`` / consensus snapshots (architecture
.md; the ``calib_gap`` precedent — external inputs are FED by the api layer). This module is
the ONE place that reads stored prices + analyst-consensus snapshots + the target-weights
config, derives the per-symbol metrics, and calls ``compute_alerts_from`` with them. It is
used by all THREE alert surfaces so the single-source invariant holds:

* the dashboard embed — via :func:`assemble` over the already-built ``DashboardData`` (no
  second ``build_dashboard``);
* ``GET /api/alerts`` + ``PUT /api/alert-rules`` — via :func:`compute_alerts_full`;
* the scheduler ``alert_scan`` — via :func:`scan_alert_compute`, registered as the
  alert-compute runner at app startup (scheduler/ never imports api/).

The read window is bounded (400 calendar days covers the 252-session 52-week high and the
90d vol comfortably). Watch symbols (registered, un-held) ARE included for drawdown ① and
consensus ④ (an entry signal is the flip side of a risk signal); vol_spike ② is held-only.
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from portfolio_dash.data_ingestion.store import list_instruments
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.portfolio.dashboard_models import DashboardData
from portfolio_dash.portfolio.technicals import annualized_volatility, week52_position
from portfolio_dash.pricing import consensus_source, snapshots_store
from portfolio_dash.pricing.store import get_price_history
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.llm_config import budget_remaining, get_alert_threshold
from portfolio_dash.strategy import target_weights as tw
from portfolio_dash.strategy.alerts import (
    Alert,
    ConsensusDelta,
    SymbolMetric,
    account_display_names,
    compute_alerts_from,
)
from portfolio_dash.strategy.rules_config import get_alert_rules

# 400 calendar days ≈ 285 sessions — covers the 252-session 52-week window AND the 90d vol
# baseline (needs 91 sessions) with generous slack for weekends/holidays/provider gaps.
_HISTORY_DAYS = 400
_VOL_SHORT = 30
_VOL_LONG = 90
_CONSENSUS_LOOKBACK_DAYS = 7


@dataclass
class AlertInputs:
    """The fed kwargs bundle for ``compute_alerts_from`` (P3 market-risk rules)."""

    symbol_metrics: dict[str, SymbolMetric] = field(default_factory=dict)
    target_weights: dict[str, Decimal] = field(default_factory=dict)
    consensus_deltas: dict[str, ConsensusDelta] = field(default_factory=dict)


def _as_decimal(value: object) -> Decimal | None:
    return value if isinstance(value, Decimal) else None


def _as_int(value: object) -> int:
    return value if isinstance(value, int) else 0


def _target_mean(payload: dict[str, object]) -> Decimal | None:
    """The mean analyst target price from a consensus snapshot payload, or None."""
    targets = payload.get("price_targets")
    if not isinstance(targets, dict):
        return None
    mean = targets.get("mean")
    return None if mean is None else Decimal(str(mean))


def _rating_score(payload: dict[str, object]) -> Decimal | None:
    score = payload.get("rating_score")
    return None if score is None else Decimal(str(score))


def _consensus_deltas(
    conn: sqlite3.Connection, symbols: list[str], *, now: datetime
) -> dict[str, ConsensusDelta]:
    """Latest-vs-(≥7-day-older) analyst consensus per symbol (missing baseline → omitted)."""
    out: dict[str, ConsensusDelta] = {}
    for sym in symbols:
        latest = snapshots_store.snapshot_on_or_before(
            conn, source=consensus_source.SOURCE, dataset=consensus_source.DATASET,
            symbol=sym, as_of=now.date(),
        )
        if latest is None:
            continue
        cutoff = latest.as_of - timedelta(days=_CONSENSUS_LOOKBACK_DAYS)
        then = snapshots_store.snapshot_on_or_before(
            conn, source=consensus_source.SOURCE, dataset=consensus_source.DATASET,
            symbol=sym, as_of=cutoff,
        )
        if then is None:
            continue  # no baseline ≥ 7 days older → the rule stays silent for this symbol
        out[sym] = ConsensusDelta(
            score_now=_rating_score(latest.payload),
            score_then=_rating_score(then.payload),
            target_mean_now=_target_mean(latest.payload),
            target_mean_then=_target_mean(then.payload),
            days_apart=(latest.as_of - then.as_of).days,
        )
    return out


def assemble(
    conn: sqlite3.Connection, data: DashboardData, *, now: datetime
) -> AlertInputs:
    """Assemble the fed market-risk inputs from stored prices + consensus + target config.

    Reuses the already-built ``data`` for the held set + current weights (no second dashboard
    build). Held = symbols carrying a live position; every REGISTERED symbol (held + watch)
    gets 52-week drawdown + consensus; vol is fed only for held symbols (vol_spike is
    held-only).
    """
    registered = sorted({i.symbol for i in list_instruments(conn)})
    held = {h.symbol for h in data.holdings if h.shares > 0}
    end = now.date()
    start = end - timedelta(days=_HISTORY_DAYS)

    metrics: dict[str, SymbolMetric] = {}
    for sym in registered:
        closes = [p.value for p in get_price_history(conn, sym, start, end)]
        w52 = week52_position(closes)
        is_held = sym in held
        metrics[sym] = SymbolMetric(
            held=is_held,
            pct_from_52w_high=_as_decimal(w52["pct_from_high"]),
            window_days=_as_int(w52["window_days"]),
            vol_30d=annualized_volatility(closes, window=_VOL_SHORT) if is_held else None,
            vol_90d=annualized_volatility(closes, window=_VOL_LONG) if is_held else None,
        )

    return AlertInputs(
        symbol_metrics=metrics,
        target_weights=tw.load_target_weights(conn),
        consensus_deltas=_consensus_deltas(conn, registered, now=now),
    )


def compute_alerts_full(
    conn: sqlite3.Connection, *, now: datetime, reporting: Currency,
    calib_gap: Decimal | None = None,
) -> list[Alert]:
    """Build the dashboard, assemble the fed inputs, and run the FULL P3 rule engine.

    The single entry point for ``GET /api/alerts`` / ``PUT /api/alert-rules`` and (via
    :func:`scan_alert_compute`) the scheduler scan. ``calib_gap`` is fed by the caller
    (``api.insight_service.calibration_gap``) — strategy/ never imports llm_insight.
    """
    data = build_dashboard(conn, now=now, reporting=reporting)
    inputs = assemble(conn, data, now=now)
    return compute_alerts_from(
        data, get_alert_rules(conn),
        quota_remaining=budget_remaining(conn),
        quota_threshold=get_alert_threshold(conn),
        calib_gap=calib_gap,
        account_names=account_display_names(conn),
        symbol_metrics=inputs.symbol_metrics,
        target_weights=inputs.target_weights,
        consensus_deltas=inputs.consensus_deltas,
    )


def scan_alert_compute(conn: sqlite3.Connection, *, now: datetime) -> list[Alert]:
    """Registered as the scheduler's alert-compute runner (app startup; scheduler → this).

    Reporting ccy = TWD; ``calib_gap`` is omitted (None) exactly as the pre-P3 scan did — the
    calib_gap rule is a dashboard/settings surface, not a scan trigger.
    """
    return compute_alerts_full(conn, now=now, reporting=Currency.TWD, calib_gap=None)
