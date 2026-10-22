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

import copy
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from portfolio_dash.data_ingestion.holdings import load_action_index
from portfolio_dash.data_ingestion.store import list_instruments
from portfolio_dash.portfolio.dashboard import build_dashboard, build_dashboard_keyed
from portfolio_dash.portfolio.dashboard_models import DashboardData
from portfolio_dash.portfolio.price_basis import price_in, series_in
from portfolio_dash.portfolio.technicals import annualized_volatility, week52_position
from portfolio_dash.pricing import consensus_source, snapshots_store
from portfolio_dash.pricing.store import get_latest_price, get_price_history
from portfolio_dash.shared import result_cache
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.instrument_scope import tracked_symbols
from portfolio_dash.shared.llm_config import ai_active, budget_remaining, get_alert_threshold
from portfolio_dash.strategy import target_weights as tw
from portfolio_dash.strategy.alerts import (
    Alert,
    ConsensusDelta,
    SymbolMetric,
    TargetLevels,
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
    target_levels: dict[str, TargetLevels] = field(default_factory=dict)


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
    gets 52-week drawdown + consensus — minus the archived ones and the un-held source
    tickers of an EXCHANGE (M1-05, see the comment at the filter); vol is fed only for held
    symbols (vol_spike is held-only). ``target_cross`` (FU-D28) is fed the latest stored
    price + the instrument's configured target band, but ONLY for symbols that actually carry
    a target (no target set → the symbol is omitted, so the rule reads nothing to cross).
    """
    held = {h.symbol for h in data.holdings if h.shares > 0}
    end = now.date()
    start = end - timedelta(days=_HISTORY_DAYS)
    # §5.1(d) / W6c: ONE ActionIndex for the whole assemble, built before the loop and never
    # inside it (trap #21) — a per-symbol index would re-read the action ledger once per
    # registered instrument. Every metric below compares closes ACROSS dates (52-week
    # high/low, 30d/90d vol), so a split inside the 400-day window puts the pre-split points
    # in a different denomination: a 7-for-1 makes them read 7x higher and the drawdown rule
    # fires a 52-week-low breach that never happened. `series_in` short-circuits structurally
    # on "no SPLIT for this symbol", so an action-free ledger reads byte-identically.
    actions = load_action_index(conn)
    # M1-05 (owner ruling 2026-09-06): two kinds of registered symbol are OUT of the universe.
    # (1) `archived` — 封存 already means "off every fetch scope" to the price refresh and the
    #     signal scan (`scheduler/jobs.py::build_worklist`); this seam gave the same flag a
    #     second meaning by feeding every registered row. An archived symbol is never fetched
    #     again, so its drawdown froze at the last stored close and kept firing for as long as
    #     the 400-day window still held those closes (~13 months, measured on MRCA).
    # (2) the SOURCE of an EXCHANGE that holds no position — the old ticker of a rename /
    #     merger (MRCA→NVSA). Its stored history is a dead series for a name that no longer
    #     trades; the destination is the live position and is fed on its own row.
    #     「AND no position」 is load-bearing: a source ticker bought again is held again.
    # Un-held watch-list symbols are NOT excluded — drawdown ① and consensus ④ fire for them
    # by design (`strategy/alerts.py`, `web/settings-alerts.js`).
    exchange_sources = {
        a.from_symbol for a in actions.all if a.kind is CorporateActionKind.EXCHANGE
    }
    tracked = set(tracked_symbols(conn))  # (1): the ONE shared predicate (DEF-064)
    instruments = sorted(
        (
            i for i in list_instruments(conn)
            if i.symbol in tracked
            and not (i.symbol in exchange_sources and i.symbol not in held)
        ),
        key=lambda i: i.symbol,
    )
    registered = [i.symbol for i in instruments]

    metrics: dict[str, SymbolMetric] = {}
    levels: dict[str, TargetLevels] = {}
    for inst in instruments:
        sym = inst.symbol
        closes = [
            p.value
            for p in series_in(
                actions, sym, get_price_history(conn, sym, start, end), valued_on=end
            )
        ]
        w52 = week52_position(closes)
        is_held = sym in held
        metrics[sym] = SymbolMetric(
            held=is_held,
            pct_from_52w_high=_as_decimal(w52["pct_from_high"]),
            window_days=_as_int(w52["window_days"]),
            vol_30d=annualized_volatility(closes, window=_VOL_SHORT) if is_held else None,
            vol_90d=annualized_volatility(closes, window=_VOL_LONG) if is_held else None,
        )
        # target_cross: only symbols with a configured band are fed. The latest quote is the
        # "現價" the rule compares (independent of the drawdown window — a stale price still
        # crosses honestly; None only when NO price exists at all → silent).
        if inst.target_low is not None or inst.target_high is not None:
            latest = get_latest_price(conn, sym, now=now)
            # Re-expressed into the SAME day as the series above (W6c): mixing a corrected
            # series with a raw latest price is the trap this package exists to avoid — and
            # a stale pre-split close compared against a post-split band crosses it by the
            # whole ratio. ⚠ The BAND itself (`target_low`/`target_high`) is owner-entered
            # and is NOT re-expressed: it is a configured intent, not a stored quote, so
            # after a split the owner must re-enter it. Same for a stored alert threshold.
            levels[sym] = TargetLevels(
                price=(
                    price_in(actions, sym, latest.value,
                             priced_on=latest.as_of, valued_on=end)
                    if latest is not None else None
                ),
                target_low=inst.target_low,
                target_high=inst.target_high,
            )

    return AlertInputs(
        symbol_metrics=metrics,
        target_weights=tw.load_target_weights(conn),
        consensus_deltas=_consensus_deltas(conn, registered, now=now),
        target_levels=levels,
    )


def compute_alerts_full(
    conn: sqlite3.Connection, *, now: datetime, reporting: Currency,
    calib_gap: Decimal | None = None, data: DashboardData | None = None,
    data_key: result_cache.CacheKey | None = None,
) -> list[Alert]:
    """Build the dashboard, assemble the fed inputs, and run the FULL P3 rule engine.

    The single entry point for ``GET /api/alerts`` / ``PUT /api/alert-rules`` / the dashboard
    route's embed and (via :func:`scan_alert_compute`) the scheduler scan. ``calib_gap`` is
    fed by the caller (``api.insight_service.calibration_gap``) — strategy/ never imports
    llm_insight.

    **Cached** (PERF spec §12.1, ``shared/result_cache.py``): the result — including the
    400-day price reads, the consensus snapshots and the target config :func:`assemble` feeds
    in — is kept as the latest ``"alerts"`` entry until the database changes, the day turns or
    a write window opens; ``calib_gap`` is part of the key (as its exact ``str``). Every
    answer is a deep copy. Alerts depend on ``now`` only through its date
    (``compute_alerts_from`` reads ``data.as_of.date()``), so nothing is re-stamped.

    **Which dashboard.** The alerts key is taken FIRST. Without *data*, a miss reads the
    dashboard through ``build_dashboard`` (cached) — a build never older than the key, since
    its own key is taken later. A caller that already holds this request's build passes it
    as *data* with the key it was served under (*data_key*,
    ``dashboard.build_dashboard_keyed``); it feeds the cached path only when that key
    describes the SAME state as the alerts key (``result_cache.same_state``). Otherwise — a
    commit landed between the build and this call, or the build was uncached — the alerts are
    computed from *data* for this caller alone: never looked up, never stored. Filing them
    under the alerts key would serve alerts of the pre-commit data under the post-commit
    version (review 2026-10-19: 「AAPL 報價過期」 beside a freshness block that reported nothing
    stale), and recomputing them from a fresh build would make this response's alerts
    disagree with the dashboard it embeds them in.

    Everything this function reads itself runs inside ONE read transaction (PERF-S12),
    opened after the dashboard is in hand: a key taken inside a transaction is ``None``, so
    reading the dashboard inside it would rebuild it on every miss.
    """
    gap_key = None if calib_gap is None else str(calib_gap)

    def evaluate(built: DashboardData) -> list[Alert]:
        with result_cache.read_snapshot(conn):
            inputs = assemble(conn, built, now=now)
            return compute_alerts_from(
                built, get_alert_rules(conn),
                quota_remaining=budget_remaining(conn),
                quota_threshold=get_alert_threshold(conn),
                ai_active=ai_active(conn),
                calib_gap=calib_gap,
                symbol_metrics=inputs.symbol_metrics,
                target_weights=inputs.target_weights,
                consensus_deltas=inputs.consensus_deltas,
                target_levels=inputs.target_levels,
            )

    key = result_cache.key_for(conn, name="alerts", now=now, reporting=reporting,
                               extra=(gap_key,))
    if data is not None and not result_cache.same_state(key, data_key):
        result_cache.note_uncached("alerts")
        return evaluate(data)

    def compute() -> list[Alert]:
        built = data if data is not None else build_dashboard(
            conn, now=now, reporting=reporting)
        return evaluate(built)

    return result_cache.serve(conn, key, name="alerts", compute=compute,
                              copy=copy.deepcopy, snapshot=False)


def scan_alert_compute(conn: sqlite3.Connection, *, now: datetime) -> list[Alert]:
    """Registered as the scheduler's alert-compute runner (app startup; scheduler → this).

    Reporting ccy = TWD; ``calib_gap`` is omitted (None) exactly as the pre-P3 scan did — the
    calib_gap rule is a dashboard/settings surface, not a scan trigger.
    """
    return compute_alerts_full(conn, now=now, reporting=Currency.TWD, calib_gap=None)


def prewarm_today() -> None:
    """Compute today's dashboard and alerts into the result cache (PERF spec §12.1 pre-warm).

    The ``fn`` of ``result_cache.schedule_prewarm``: scheduled 1 s after a settlement run
    (``settlement_service._schedule_prewarm``), 3 s after the last write window
    (``api/app.py``'s middleware) and at once after a recorded session touch, which itself
    waited for the page load to settle (``auth_store._prewarm_after_touch``); all share one
    debounced timer. It reads exactly what a
    request would — the clock and reporting currency of ``api/deps.py``, its own
    ``shared.db.session`` — and the alerts carry the same
    ``calib_gap`` as ``GET /api/alerts`` and reuse the build only under the same state
    (``data_key``), so both entries are the ones the next request asks for. Reads only; a
    failure is logged by ``schedule_prewarm``.
    """
    from portfolio_dash.api import deps
    from portfolio_dash.api.insight_service import calibration_gap  # local: a heavy module
    from portfolio_dash.shared.db import session

    now = deps.get_now()
    reporting = deps.get_reporting()
    with session() as conn:
        data, data_key = build_dashboard_keyed(conn, now=now, reporting=reporting)
        compute_alerts_full(conn, now=now, reporting=reporting,
                            calib_gap=calibration_gap(conn), data=data, data_key=data_key)
