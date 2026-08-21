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
from bisect import bisect_left, bisect_right
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from portfolio_dash.data_ingestion.holdings import current_shares, load_action_index
from portfolio_dash.data_ingestion.store import list_accounts, list_instruments
from portfolio_dash.llm_insight import alerts_bridge
from portfolio_dash.portfolio.price_basis import series_in
from portfolio_dash.pricing.store import get_price_history, price_dates
from portfolio_dash.shared.corporate_actions import ActionIndex
from portfolio_dash.shared.wire import decimal_str
from portfolio_dash.strategy import signal_history, signal_states
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
    conn: sqlite3.Connection, symbol: str, *, now: datetime, params: RulesParams,
    actions: ActionIndex,
) -> tuple[list[Decimal], list[Decimal | None] | None, date | None]:
    """Read the derived-window closes + aligned volumes for ``symbol`` from stored prices.

    Returns ``(closes, volumes, last_price_date)`` — the third element is the date of the
    last close in the window (``None`` when empty): the history row's ``as_of`` is THAT
    date (the data the evaluation describes), never the scan's wall-clock date (AI-D28).

    Volumes are fed only when at least one session carries volume (so the volume-
    confirmation signal stays honestly absent pre-backfill), mirroring ``insight_service``.

    §5.1(d) / W6c — **this is the highest-consequence series read in the app.** Every rule
    the engine runs compares closes ACROSS dates (12-1 momentum, MA50/MA200 cross, RSI,
    52-week position, the MA200 trend filter), and a stored close is as traded on its own
    date. A split inside the ~583-day window therefore puts the older points in a different
    denomination: a 7-for-1 makes the pre-split year read 7x higher, the 52-week position
    collapses to 0, and the scan WRITES an ``alert_events`` row that notifies the owner
    about a breach that never happened. Re-expressed into ``now``'s share terms first.

    ``actions`` is threaded in, never built here (trap #21): both scan loops call this once
    per registered symbol.

    ⚠ ``volumes`` is deliberately NOT re-expressed — ``volume`` has no raw column (D39b), so
    a factor on it could never be restated or reversed. The volume-confirmation signal
    therefore still compares two denominations across a split; that needs its own stored
    column, not a read-path divide.
    """
    end = now.date()
    start = end - timedelta(days=required_calendar_days(params))
    history = series_in(
        actions, symbol, get_price_history(conn, symbol, start, end), valued_on=end
    )
    closes: list[Decimal] = [p.value for p in history]
    raw_volumes: list[Decimal | None] = [p.volume for p in history]
    volumes = raw_volumes if any(v is not None for v in raw_volumes) else None
    return closes, volumes, history[-1].as_of if history else None


def evaluate_symbol(
    conn: sqlite3.Connection,
    symbol: str,
    *,
    now: datetime,
    params: RulesParams | None = None,
) -> SymbolSignals | None:
    """Evaluate one symbol's signals from stored prices (single-symbol drawer path)."""
    resolved = params if params is not None else default_params()
    closes, volumes, _ = _read_series(conn, symbol, now=now, params=resolved,
                                      actions=load_action_index(conn))
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
    actions = load_action_index(conn)  # ONE per request, outside the loop (trap #21)
    out: list[tuple[str, SymbolSignals | None, bool]] = []
    for symbol in _registered_symbols(conn):
        closes, volumes, _ = _read_series(conn, symbol, now=now, params=params,
                                          actions=actions)
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


def _computable_dates(
    conn: sqlite3.Connection, symbol: str, params: RulesParams
) -> list[date]:
    """The price dates whose evaluation window holds ≥ ``required_sessions`` sessions.

    The honest floor (AI-D28): a history row exists only where ALL FOUR rules CAN evaluate,
    so the event study's rows are uniformly full-coverage and ``tech_score`` is never a
    two-rule blend pretending to be four. A thinner series honestly starts its history where
    the window fills (``signal_states`` still tracks it daily). Derived from params, so a
    recalibration moves the floor with the window.
    """
    dates = price_dates(conn, symbol)
    if not dates:
        return []
    floor = required_sessions(params)
    span = timedelta(days=required_calendar_days(params))
    return [
        d for d in dates
        if bisect_right(dates, d) - bisect_left(dates, d - span) >= floor
    ]


def _fill_signal_history(
    conn: sqlite3.Connection,
    symbol: str,
    *,
    params: RulesParams,
    actions: ActionIndex,
    stamped: str,
) -> tuple[int, int]:
    """Replay missing history dates for ``symbol`` + refresh the head row; returns
    ``(rows_written, head_refreshed)``.

    * **Missing-set rule** (AI-D27): fill every computable date NOT already stored — a
      later deeper price backfill (left-edge hole), a provider gap filled in (middle hole),
      or an aborted first backfill all self-heal on the next scan, where a max-as_of
      watermark would hide all three.
    * **Per-date re-assembly** (deliberate, NOT fetch-once-truncate): each date's window is
      read and re-expressed at THAT date through the same ``_read_series`` the daily scan
      uses, so a replayed row is by construction the row a scan on that date would have
      written. The scale-invariance shortcut (re-express once, slice) rests on an argument
      about today's four rules that a future rule may not satisfy; the per-date cost is
      seconds for a full backfill and milliseconds on an incremental day.
    * **Head-row refresh** — compare-then-skip: the last computable date's row is
      re-evaluated and rewritten only when content changed (a corrected close), keeping
      ``updated_at`` stable so a re-scan is a provable no-op.
    """
    dates = _computable_dates(conn, symbol, params)
    if not dates:
        return 0, 0
    stored = signal_history.as_of_set(conn, symbol)
    rows: list[signal_history.SignalHistoryRow] = []
    for d in dates:
        if d in stored:
            continue
        d_closes, d_volumes, _ = _read_series(
            conn, symbol, now=datetime.combine(d, time.min),
            params=params, actions=actions,
        )
        d_signals = engine.evaluate_symbol(d_closes, d_volumes, params)
        if d_signals is None:
            continue  # an empty window ON a price date — impossible by construction
            # (the window [d−span, d] contains d's own close); if it ever happened the
            # date simply stays missing and the next scan retries — honest, never faked.
        rows.append(signal_history.row_from_signals(
            symbol, d_signals, as_of=d, updated_at=stamped,
        ))
    written = signal_history.upsert_rows(conn, rows)
    changed = 0
    head = dates[-1]
    if head in stored:  # a just-written head is identical by construction — skip it
        h_closes, h_volumes, _ = _read_series(
            conn, symbol, now=datetime.combine(head, time.min),
            params=params, actions=actions,
        )
        h_signals = engine.evaluate_symbol(h_closes, h_volumes, params)
        if h_signals is not None and signal_history.upsert_row_if_changed(
            conn,
            signal_history.row_from_signals(
                symbol, h_signals, as_of=head, updated_at=stamped,
            ),
        ):
            changed = 1
    return written, changed


def scan_signals(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    progress: Callable[[str], None] | None = None,
) -> str:
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

    W6 (AI-D27/AI-D28): the same pass maintains the ``signal_history`` per-day table —
    replaying missing dates (the first scan after upgrade IS the backfill, minutes in the
    background; the ``progress`` callback reports per-symbol) and refreshing the head row
    compare-then-skip. A stale ``params_version`` vintage is pruned first so a future
    recalibration refills under the new params on the same pass (no-op under rules-v1).
    The scan still has NO per-symbol error tolerance — one poison symbol aborts the run and
    the job reports the failure loudly; the missing-set rule makes the NEXT scan resume
    exactly where it stopped, so loud failure is also self-healing.
    """
    signal_states.ensure_table(conn)
    signal_history.ensure_table(conn)
    alerts_bridge.ensure_tables(conn)
    params = default_params()
    as_of = now.date().isoformat()
    stamped = now.isoformat()

    symbols = _registered_symbols(conn)
    actions = load_action_index(conn)  # ONE per scan, outside the loop (trap #21)
    pruned = signal_history.prune_params_version(conn, PARAMS_VERSION)
    seeded = 0
    recorded = 0
    replayed = 0
    refreshed = 0
    for pos, symbol in enumerate(symbols, start=1):
        closes, volumes, _ = _read_series(conn, symbol, now=now, params=params,
                                          actions=actions)
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
        else:
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

        written, changed = _fill_signal_history(
            conn, symbol, params=params, actions=actions, stamped=stamped,
        )
        replayed += written
        refreshed += changed
        if written and progress is not None:
            progress(f"回填訊號歷史 {symbol}（+{written} 列）（{pos}/{len(symbols)}）")

    detail = (f"{len(symbols)} symbol(s), {seeded} seeded, {recorded} transition event(s), "
              f"{replayed} history row(s) replayed, {refreshed} head refresh(es)")
    if pruned:
        detail += f", pruned {pruned} stale-vintage row(s)"
    return detail
