"""Derived-cache of each held symbol's last rule state + pure transition detection.

``signal_states`` is a **derived cache, not a source of truth**: the truth stays in
``prices``; dropping every row and re-scanning reproduces an equivalent state (the
computed-on-read cache exemption in ``data-and-pricing.md`` / blueprint §5.1). One row
per symbol records the last-seen raw trend / cross / momentum states (for display /
rebuild) PLUS two **hold columns** — the last non-neutral trend direction and the last
non-flat momentum sign — so a reversal is detected even when it dwells in the dead-band
for one or more scans (deep review 2026-07-10; see :func:`detect_transitions`).

Layer note (conn-bearing module lives in ``strategy/``): this mirrors the established
``strategy/rules_config.py`` convention — the pure rule ENGINE lives in
``strategy/rules/`` while conn-bearing rule config/state persistence lives directly in
``strategy/`` (both take a ``conn``). This module imports only stdlib + the rule types; it
does NOT import ``llm_insight`` / ``api`` / ``web`` (architecture.md #4). The scan
ORCHESTRATION — which additionally writes ``alert_events`` (an ``llm_insight`` table) — is
therefore NOT here; it lives in the api seam (``api/signals_service.py``), which may import
both this module and ``llm_insight.alerts_bridge``. The transition LOGIC itself stays here,
pure and unit-testable.
"""

import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal

from portfolio_dash.strategy.rules.types import RuleState, SymbolSignals

# Event rule_ids recorded on a transition (fed to alerts_bridge.record_event). New ids: no
# existing on_alert combo subscribes to them via the 'all' wildcard (alerts_bridge excludes
# signal_* from 'all'), so recording is a pure audit/queue write until a user subscribes
# EXPLICITLY — no event storm, no behavioural change to existing risk rules.
EVENT_TREND = "signal_trend"
EVENT_CROSS = "signal_cross"
EVENT_MOMENTUM = "signal_momentum"

# The two CONFIRMED trend states (score ±1); everything else (in_band / *_unconfirmed /
# absent) reads as a neutral direction, so unconfirmed whipsaw never fires a transition.
_TREND_UP = "above_confirmed"
_TREND_DOWN = "below_confirmed"

# A FRESH golden/death cross state (ma_cross reports the standing relationship —
# fast_above / fast_below / aligned — when no cross is found within the lookback).
_CROSS_FRESH = frozenset({"golden", "death"})


@dataclass(frozen=True)
class DerivedState:
    """The comparable slice of a symbol's signals used for transition detection.

    ``tech_score`` / ``evaluation_context`` are carried for the cache row (and the
    monitoring surface later); they do NOT drive transition detection.
    """

    trend_state: str | None
    cross_state: str | None
    cross_days_ago: int | None
    momentum_state: str | None
    tech_score: Decimal | None
    evaluation_context: str | None


@dataclass(frozen=True)
class HoldState:
    """The persisted hold columns: last non-neutral trend direction + non-flat momentum sign.

    These survive neutral / flat / unmeasured scans (a dead-band dip does NOT reset them),
    so a genuine up↔down or positive↔negative reversal fires exactly once even when it
    dwells in the neutral zone in between (deep review 2026-07-10). Distinct from the raw
    ``DerivedState`` display slice.
    """

    trend_last_dir: str | None       # 'up' / 'down' / None (never seen a confirmed dir)
    momentum_last_sign: str | None   # 'positive' / 'negative' / None (never seen a sign)


@dataclass(frozen=True)
class SignalState:
    """A stored ``signal_states`` row: the derived slice + hold columns + provenance stamps."""

    symbol: str
    derived: DerivedState
    hold: HoldState
    params_version: str
    as_of: str
    updated_at: str


@dataclass(frozen=True)
class TransitionResult:
    """The outcome of a transition scan for one symbol: fired events + the hold to persist.

    ``hold`` is the UPDATED hold state (direction/sign carried forward on a neutral scan)
    that the caller writes back so the next scan compares against the remembered direction.
    """

    events: list[str] = field(default_factory=list)
    hold: HoldState = field(default_factory=lambda: HoldState(None, None))


_EMPTY = DerivedState(
    trend_state=None, cross_state=None, cross_days_ago=None,
    momentum_state=None, tech_score=None, evaluation_context=None,
)


def extract_state(signals: SymbolSignals | None) -> DerivedState:
    """Reduce a :class:`SymbolSignals` (or ``None``) to the comparable derived state.

    An absent/too-thin series (``signals is None`` or a rule ``None``) yields ``None`` for
    that field — honest, never fabricated.
    """
    if signals is None:
        return _EMPTY
    trend = signals.rules.get("trend_filter")
    cross = signals.rules.get("ma_cross")
    momentum = signals.rules.get("momentum_12_1")
    composite = signals.composite
    return DerivedState(
        trend_state=trend.state if trend is not None else None,
        cross_state=cross.state if cross is not None else None,
        cross_days_ago=_cross_days_ago(cross),
        momentum_state=momentum.state if momentum is not None else None,
        tech_score=composite.tech_score if composite is not None else None,
        evaluation_context=composite.evaluation_context if composite is not None else None,
    )


def _cross_days_ago(cross: RuleState | None) -> int | None:
    """``days_ago`` from the ma_cross evidence when a fresh cross was detected, else None."""
    if cross is None:
        return None
    value = cross.evidence.get("days_ago")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _trend_dir(state: str | None) -> str | None:
    """Confirmed trend direction label: ``up`` / ``down``, else ``None``.

    ``None`` covers in_band / *_unconfirmed / absent — the NEUTRAL zone that HOLDS (never
    resets) the remembered direction.
    """
    if state == _TREND_UP:
        return "up"
    if state == _TREND_DOWN:
        return "down"
    return None


def _momentum_sign(state: str | None) -> str | None:
    """Non-flat momentum sign label: ``positive`` / ``negative``, else ``None``.

    ``None`` covers ``flat`` and an unmeasured momentum — the dead-band that HOLDS (never
    resets) the remembered sign.
    """
    if state in ("positive", "negative"):
        return state
    return None


def seed_hold(new: DerivedState) -> HoldState:
    """The hold state to persist on a SILENT seed (first run / params_version reseed).

    Reset from the current evaluation: the confirmed direction / non-flat sign if present,
    else ``None``. A seed NEVER fires an event — a first sighting and a recalibration are
    not market events.
    """
    return HoldState(
        trend_last_dir=_trend_dir(new.trend_state),
        momentum_last_sign=_momentum_sign(new.momentum_state),
    )


def detect_transitions(
    stored: DerivedState, new: DerivedState, hold: HoldState
) -> TransitionResult:
    """Transition events fired moving to ``new`` state, plus the updated hold to persist.

    Deterministic and pure (unit-tested). HOLD semantics (deep review 2026-07-10) for the
    two direction rules: a neutral / flat / unmeasured scan does NOT reset the remembered
    direction — it HOLDS it — so a genuine reversal is caught even after dwelling in the
    dead-band, and a mere drop into neutral (or a data-availability gap) never fires.

    * **trend** — fires only when the newly CONFIRMED direction (up / down) differs from
      the last remembered non-neutral direction (``hold.trend_last_dir``). Entering or
      leaving neutral, ``None → confirmed`` (no prior direction), and re-confirming the
      SAME direction through neutral noise are all SILENT.
    * **cross** — a fresh golden / death cross that is genuinely new: a different cross
      state than ``stored``, OR the same cross type at a strictly SMALLER ``days_ago`` (a
      newer cross replaced the old one). That smaller-``days_ago`` clause requires BOTH
      ``days_ago`` values to be present, so a legacy / hand-edited NULL row never re-fires
      every scan (F5). A cross merely AGEING (larger ``days_ago``) is not a new event.
    * **momentum** — fires only when the new non-flat sign (positive / negative) differs
      from the last remembered non-flat sign (``hold.momentum_last_sign``). ``flat`` and an
      unmeasured (``None``) momentum HOLD the sign — they never reset it and never fire.
    """
    events: list[str] = []

    # --- trend: hold semantics (up↔down through any neutral noise) ---
    new_dir = _trend_dir(new.trend_state)
    if (
        new_dir is not None
        and hold.trend_last_dir is not None
        and new_dir != hold.trend_last_dir
    ):
        events.append(EVENT_TREND)
    next_trend_dir = new_dir if new_dir is not None else hold.trend_last_dir

    # --- cross: a genuinely-new fresh cross (F5: smaller-days_ago needs both present) ---
    if new.cross_state in _CROSS_FRESH:
        newer = stored.cross_state != new.cross_state or (
            new.cross_days_ago is not None
            and stored.cross_days_ago is not None
            and new.cross_days_ago < stored.cross_days_ago
        )
        if newer:
            events.append(EVENT_CROSS)

    # --- momentum: hold semantics (positive↔negative through any flat/unmeasured) ---
    new_sign = _momentum_sign(new.momentum_state)
    if (
        new_sign is not None
        and hold.momentum_last_sign is not None
        and new_sign != hold.momentum_last_sign
    ):
        events.append(EVENT_MOMENTUM)
    next_momentum_sign = new_sign if new_sign is not None else hold.momentum_last_sign

    return TransitionResult(
        events=events,
        hold=HoldState(
            trend_last_dir=next_trend_dir, momentum_last_sign=next_momentum_sign
        ),
    )


# --- conn-bearing store (derived cache; rebuildable) --------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS signal_states (
    symbol TEXT PRIMARY KEY,
    trend_state TEXT,
    cross_state TEXT,
    cross_days_ago INTEGER,
    momentum_state TEXT,
    tech_score TEXT,
    evaluation_context TEXT,
    trend_last_dir TEXT,
    momentum_last_sign TEXT,
    params_version TEXT NOT NULL,
    as_of TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_COLUMNS = (
    "symbol, trend_state, cross_state, cross_days_ago, momentum_state, tech_score, "
    "evaluation_context, trend_last_dir, momentum_last_sign, params_version, as_of, updated_at"
)


def ensure_table(conn: sqlite3.Connection) -> None:
    """Create the ``signal_states`` derived-cache table if missing (idempotent)."""
    conn.executescript(_DDL)
    conn.commit()


def _row_to_state(row: sqlite3.Row) -> SignalState:
    days = row["cross_days_ago"]
    score = row["tech_score"]
    return SignalState(
        symbol=row["symbol"],
        derived=DerivedState(
            trend_state=row["trend_state"],
            cross_state=row["cross_state"],
            cross_days_ago=int(days) if days is not None else None,
            momentum_state=row["momentum_state"],
            tech_score=Decimal(score) if score is not None else None,
            evaluation_context=row["evaluation_context"],
        ),
        hold=HoldState(
            trend_last_dir=row["trend_last_dir"],
            momentum_last_sign=row["momentum_last_sign"],
        ),
        params_version=row["params_version"],
        as_of=row["as_of"],
        updated_at=row["updated_at"],
    )


def get_state(conn: sqlite3.Connection, symbol: str) -> SignalState | None:
    """The stored state row for ``symbol``, or ``None`` when never seeded."""
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM signal_states WHERE symbol = ?",
        (symbol,),
    ).fetchone()
    return _row_to_state(row) if row is not None else None


def all_states(conn: sqlite3.Connection) -> list[SignalState]:
    """Every stored state row, ordered by symbol (for rebuild verification / listing)."""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM signal_states ORDER BY symbol"
    ).fetchall()
    return [_row_to_state(r) for r in rows]


def upsert_state(
    conn: sqlite3.Connection,
    symbol: str,
    derived: DerivedState,
    *,
    hold: HoldState,
    params_version: str,
    as_of: str,
    updated_at: str,
) -> None:
    """Upsert one symbol's derived state + hold columns (keyed on ``symbol``). ``tech_score``
    is stored as a canonical Decimal STRING (full precision — a cache value, not money of
    record)."""
    tech_score = None if derived.tech_score is None else format(derived.tech_score, "f")
    conn.execute(
        "INSERT INTO signal_states (symbol, trend_state, cross_state, cross_days_ago, "
        "momentum_state, tech_score, evaluation_context, trend_last_dir, momentum_last_sign, "
        "params_version, as_of, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(symbol) DO UPDATE SET trend_state=excluded.trend_state, "
        "cross_state=excluded.cross_state, cross_days_ago=excluded.cross_days_ago, "
        "momentum_state=excluded.momentum_state, tech_score=excluded.tech_score, "
        "evaluation_context=excluded.evaluation_context, "
        "trend_last_dir=excluded.trend_last_dir, "
        "momentum_last_sign=excluded.momentum_last_sign, "
        "params_version=excluded.params_version, as_of=excluded.as_of, "
        "updated_at=excluded.updated_at",
        (symbol, derived.trend_state, derived.cross_state, derived.cross_days_ago,
         derived.momentum_state, tech_score, derived.evaluation_context,
         hold.trend_last_dir, hold.momentum_last_sign,
         params_version, as_of, updated_at),
    )
    conn.commit()


def clear_all(conn: sqlite3.Connection) -> None:
    """Wipe the derived cache (rebuild path: a following scan re-seeds it silently)."""
    conn.execute("DELETE FROM signal_states")
    conn.commit()


__all__ = [
    "EVENT_CROSS",
    "EVENT_MOMENTUM",
    "EVENT_TREND",
    "DerivedState",
    "HoldState",
    "SignalState",
    "TransitionResult",
    "all_states",
    "clear_all",
    "detect_transitions",
    "ensure_table",
    "extract_state",
    "get_state",
    "seed_hold",
    "upsert_state",
]
