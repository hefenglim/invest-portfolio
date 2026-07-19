"""Signals service (P2 batch 2) — the conn-bearing seam over the pure rule engine.

The ONLY place that reads ``pricing`` / ``portfolio`` to feed the rule engine, so
``strategy.rules`` stays pure (architecture.md; same precedent as ``insight_service``
feeding ``closes`` into the technicals). It:

1. derives the history window from the rule params (NOT the 400-calendar-day technicals
   constant — momentum needs ~253 sessions and the cross wants ~260, so a 400d window can
   silently starve momentum to ``None``);
2. reads closes + aligned volumes and calls ``engine.evaluate_symbol``;
3. serializes to the wire with DISPLAY quantization (the engine stays full precision);
4. runs the ``signal_states`` transition scan — the ONE place that additionally writes
   ``alert_events`` (an ``llm_insight`` table), which is why the scan lives in the api seam
   and not in ``strategy`` (strategy never imports ``llm_insight``). It is registered as the
   ``signal_scan`` scheduler runner at app startup (scheduler never imports api).
"""

import sqlite3
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from portfolio_dash.data_ingestion.holdings import current_shares
from portfolio_dash.data_ingestion.store import list_accounts, list_instruments
from portfolio_dash.llm_insight import alerts_bridge
from portfolio_dash.pricing.store import get_price_history
from portfolio_dash.shared.wire import decimal_str
from portfolio_dash.strategy import signal_states
from portfolio_dash.strategy.rules import engine
from portfolio_dash.strategy.rules.composite import RULE_ORDER
from portfolio_dash.strategy.rules.params import (
    PARAMS_VERSION,
    RulesParams,
    default_params,
)
from portfolio_dash.strategy.rules.types import Composite, RuleState, SymbolSignals

# Calendar-day window derivation (THE known trap). A trading session is ~5 per 7 calendar
# days (``_WEEKEND_STRETCH``); on top of that we pad generously (``_SAFETY_FACTOR``) for
# market holidays (~10/yr) and provider gaps so the momentum (≈253 sessions) and cross
# (≈260 sessions) windows are ALWAYS satisfiable under the 5y backfill. With a thinner
# series the engine degrades honestly (rules → ``None``) and the API passes that through —
# it never pads. Derived from params so a recalibration moves the window with it.
_WEEKEND_STRETCH = Decimal("1.4")
_SAFETY_FACTOR = Decimal("1.6")

# Evidence keys whose Decimal values are ratio-like → quantized to 4 dp on the wire; every
# other Decimal in an evidence dict is stringified at full precision (never a raw Decimal).
_RATIO_EVIDENCE_KEYS = frozenset({
    "price_vs_ma", "return_12_1", "decay_factor", "confidence_modifier",
    "pct_from_52w_high", "pct_from_52w_low",
})

_Q1 = Decimal("0.1")
_Q2 = Decimal("0.01")
_Q4 = Decimal("0.0001")


def required_sessions(params: RulesParams) -> int:
    """The max trailing SESSIONS any rule needs — the honest data floor for full coverage.

    Per-rule window semantics differ (deep-review 2026-07-10: surface per-rule, never
    aggregate) — this is only the READ window so every rule *can* be evaluated; each rule
    still reports its own ``window_days``.
    """
    return max(
        params.momentum.lookback_sessions + 1,          # 12-1 momentum base anchor
        params.cross.slow + params.cross.cross_lookback,  # slow MA + cross detection lookback
        params.rsi.week52_window + 1,                    # 52-week position
        params.trend.ma,                                 # MA(200) trend filter
    )


def required_calendar_days(params: RulesParams) -> int:
    """Calendar-day read window derived from :func:`required_sessions` (see module note).

    Defaults: ``required_sessions`` = 260 → ``ceil(260 × 1.4 × 1.6)`` = 583 calendar days.
    """
    sessions = Decimal(required_sessions(params))
    days = (sessions * _WEEKEND_STRETCH * _SAFETY_FACTOR).to_integral_value(
        rounding=ROUND_CEILING
    )
    return int(days)


def _read_series(
    conn: sqlite3.Connection, symbol: str, *, now: datetime, params: RulesParams
) -> tuple[list[Decimal], list[Decimal | None] | None]:
    """Read the derived-window closes + aligned volumes for ``symbol`` from stored prices.

    Volumes are fed only when at least one session carries volume (so the volume-
    confirmation signal stays honestly absent pre-backfill), mirroring ``insight_service``.
    """
    end = now.date()
    start = end - timedelta(days=required_calendar_days(params))
    history = get_price_history(conn, symbol, start, end)
    closes: list[Decimal] = [p.value for p in history]
    raw_volumes: list[Decimal | None] = [p.volume for p in history]
    volumes = raw_volumes if any(v is not None for v in raw_volumes) else None
    return closes, volumes


def evaluate_symbol(
    conn: sqlite3.Connection,
    symbol: str,
    *,
    now: datetime,
    params: RulesParams | None = None,
) -> SymbolSignals | None:
    """Evaluate one symbol's signals from stored prices (single-symbol drawer path)."""
    resolved = params if params is not None else default_params()
    closes, volumes = _read_series(conn, symbol, now=now, params=resolved)
    return engine.evaluate_symbol(closes, volumes, resolved)


def _registered_symbols(conn: sqlite3.Connection) -> list[str]:
    """Every REGISTERED instrument symbol (held + watchlist) — the ``/api/signals`` and
    ``signal_scan`` universe (P2 batch 3). A watched symbol is an entry candidate: its
    signals / TechScore / transition events matter exactly as a held one's do (a golden
    cross on a watchlist name IS the build-a-position moment). Technical signals are
    symbol-level, so this enumerates instruments directly (no ``build_dashboard``).

    Archived symbols (FU-D13) are excluded: a stopped-tracking name is no longer an entry
    candidate, so it drops out of the scan + evaluate_all universe (its money still counts
    everywhere else — archiving never touches the dashboard)."""
    return sorted({i.symbol for i in list_instruments(conn) if not i.archived})


def _account_ids(conn: sqlite3.Connection) -> list[str]:
    return [a.account_id for a in list_accounts(conn)]


def _is_held(conn: sqlite3.Connection, symbol: str, *, account_ids: list[str]) -> bool:
    """Whether *symbol* carries a live position in any account (cheap holdings check —
    same precedent as ``instruments._held``: net current_shares > 0, no dashboard build)."""
    return any(current_shares(conn, aid, symbol) > 0 for aid in account_ids)


def is_held(conn: sqlite3.Connection, symbol: str) -> bool:
    """Public single-symbol ``held`` check (drawer + rule_signals_json variable feed)."""
    return _is_held(conn, symbol, account_ids=_account_ids(conn))


def evaluate_all(
    conn: sqlite3.Connection, *, now: datetime
) -> list[tuple[str, SymbolSignals | None, bool]]:
    """Evaluate every REGISTERED symbol (held + watchlist); returns
    ``(symbol, signals-or-None, held)`` triples, sorted. Watch symbols get the same honest
    evaluation as held ones — the API tags each with its ``held`` flag (P2 batch 3)."""
    params = default_params()
    account_ids = _account_ids(conn)
    out: list[tuple[str, SymbolSignals | None, bool]] = []
    for symbol in _registered_symbols(conn):
        closes, volumes = _read_series(conn, symbol, now=now, params=params)
        signals = engine.evaluate_symbol(closes, volumes, params)
        out.append((symbol, signals, _is_held(conn, symbol, account_ids=account_ids)))
    return out


# --- wire serialization (DISPLAY quantization; the engine stays full precision) ---------


def _q(value: Decimal, exp: Decimal) -> str:
    return decimal_str(value.quantize(exp, rounding=ROUND_HALF_UP))


def _evidence_wire(evidence: dict[str, object]) -> dict[str, object]:
    """Serialize a rule's evidence: ratio-like Decimals → 4 dp, other Decimals → full-
    precision string, ints/bools/strings/None pass through. Never a raw Decimal on the wire.
    """
    out: dict[str, object] = {}
    for key, value in evidence.items():
        if value is None or isinstance(value, bool | str):
            out[key] = value
        elif isinstance(value, Decimal):
            out[key] = _q(value, _Q4) if key in _RATIO_EVIDENCE_KEYS else decimal_str(value)
        elif isinstance(value, int):
            out[key] = value
        else:  # defensive: never leak a non-JSON type
            out[key] = str(value)
    return out


def _rule_wire(rule: RuleState | None) -> dict[str, object] | None:
    if rule is None:
        return None
    return {
        "state": rule.state,
        "score": _q(rule.score, _Q2),            # signed contribution, 2 dp
        "window_days": rule.window_days,
        "evidence": _evidence_wire(rule.evidence),
    }


def _composite_wire(composite: Composite) -> dict[str, object]:
    return {
        "tech_score": _q(composite.tech_score, _Q1),                       # 0-100, 1 dp
        "contributions": {k: _q(v, _Q2) for k, v in composite.contributions.items()},
        "weights_applied": {k: _q(v, _Q2) for k, v in composite.weights_applied.items()},
        "coverage": composite.coverage,
        "missing": list(composite.missing),
        "evaluation_context": composite.evaluation_context,
        "context_note": composite.context_note,
    }


def to_wire(
    symbol: str, signals: SymbolSignals | None, *, now: datetime, held: bool = False
) -> dict[str, object]:
    """The per-symbol ``/api/signals`` wire payload (honest nulls when a rule/composite is
    too thin to judge). ``held`` tags whether the symbol carries a live position (P2 batch
    3): a watchlist entry serializes identically but with ``held=false``."""
    if signals is None:
        rules_wire: dict[str, object | None] = dict.fromkeys(RULE_ORDER, None)
        composite_wire: dict[str, object] | None = None
        params_version = PARAMS_VERSION
    else:
        rules_wire = {name: _rule_wire(signals.rules.get(name)) for name in RULE_ORDER}
        composite_wire = (
            _composite_wire(signals.composite) if signals.composite is not None else None
        )
        params_version = signals.params_version
    return {
        "symbol": symbol,
        "held": held,
        "evaluated_at": now.isoformat(),
        "as_of": now.date().isoformat(),
        "params_version": params_version,
        "rules": rules_wire,
        "composite": composite_wire,
    }


# --- transition scan (registered as the signal_scan scheduler runner) -------------------


def scan_signals(conn: sqlite3.Connection, *, now: datetime) -> str:
    """Evaluate every REGISTERED symbol (held + watchlist), compare with the stored
    ``signal_states`` cache, record transition events, and refresh the cache. Returns a
    short ``job_runs.detail`` summary. Watch symbols seed silently and fire transitions
    exactly like held ones (P2 batch 3 — a watchlist golden cross is a build-a-position
    signal worth an event).

    Discipline (per the mini-spec + deep review 2026-07-10):
    * **first run seeds silently** — a symbol with no stored row is written with ZERO
      events, and its hold columns are seeded from the current evaluation (no event storm
      on first deploy);
    * **params_version change reseeds silently** — a recalibration is not a market event;
      the hold columns are reset from the new evaluation (full silent reseed);
    * **hold semantics** — the trend/momentum detectors compare against the last non-neutral
      direction/sign (``signal_states.detect_transitions``), which the scan carries forward
      every pass, so a reversal through a dead-band dwell fires exactly once;
    * **coalesced per (rule, symbol, day)** — an intra-day repeated flip records ≤1 event
      per (rule, symbol) per day (``record_event_ex`` dedups). The cron runs once daily; a
      manual same-day re-run is deliberately conservative (no double-count). The detail's
      transition count therefore counts only INSERTED events, not merely DETECTED ones.
    """
    signal_states.ensure_table(conn)
    alerts_bridge.ensure_tables(conn)
    params = default_params()
    as_of = now.date().isoformat()
    stamped = now.isoformat()

    symbols = _registered_symbols(conn)
    seeded = 0
    recorded = 0
    for symbol in symbols:
        closes, volumes = _read_series(conn, symbol, now=now, params=params)
        signals = engine.evaluate_symbol(closes, volumes, params)
        new_state = signal_states.extract_state(signals)
        stored = signal_states.get_state(conn, symbol)

        if stored is None or stored.params_version != PARAMS_VERSION:
            # First run for this symbol, or a params recalibration → reseed silently.
            signal_states.upsert_state(
                conn, symbol, new_state, hold=signal_states.seed_hold(new_state),
                params_version=PARAMS_VERSION, as_of=as_of, updated_at=stamped,
            )
            seeded += 1
            continue

        result = signal_states.detect_transitions(stored.derived, new_state, stored.hold)
        for rule_id in result.events:
            _, inserted = alerts_bridge.record_event_ex(
                conn, rule_id=rule_id, symbol=symbol, now=now
            )
            if inserted:  # coalesced same-day repeats do not inflate the count (F2)
                recorded += 1
        signal_states.upsert_state(
            conn, symbol, new_state, hold=result.hold,
            params_version=PARAMS_VERSION, as_of=as_of, updated_at=stamped,
        )

    return f"{len(symbols)} symbol(s), {seeded} seeded, {recorded} transition event(s)"
