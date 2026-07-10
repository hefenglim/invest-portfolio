"""Derived-cache of each held symbol's last rule state + pure transition detection.

``signal_states`` is a **derived cache, not a source of truth**: the truth stays in
``prices``; dropping every row and re-scanning reproduces an equivalent state (the
computed-on-read cache exemption in ``data-and-pricing.md`` / blueprint §5.1). One row
per symbol records the last-seen trend / cross / momentum states so the scan can detect a
*transition* (a state boundary crossed since the previous scan) and record it as an event.

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
from dataclasses import dataclass
from decimal import Decimal

from portfolio_dash.strategy.rules.types import RuleState, SymbolSignals

# Event rule_ids recorded on a transition (fed to alerts_bridge.record_event). New ids: no
# existing on_alert combo subscribes to them, so recording is a pure audit/queue write until
# a user subscribes — no event storm, no behavioural change to existing rules.
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
class SignalState:
    """A stored ``signal_states`` row: the derived slice + provenance stamps."""

    symbol: str
    derived: DerivedState
    params_version: str
    as_of: str
    updated_at: str


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


def _trend_direction(state: str | None) -> str:
    """Confirmed trend direction: ``up`` / ``down`` / ``neutral`` (unconfirmed → neutral)."""
    if state == _TREND_UP:
        return "up"
    if state == _TREND_DOWN:
        return "down"
    return "neutral"


def _momentum_sign(state: str | None) -> int | None:
    """Momentum sign: ``+1`` positive · ``-1`` negative · ``0`` flat · ``None`` unmeasured."""
    if state == "positive":
        return 1
    if state == "negative":
        return -1
    if state == "flat":
        return 0
    return None


def detect_transitions(stored: DerivedState, new: DerivedState) -> list[str]:
    """The transition event rule_ids fired by moving from ``stored`` to ``new`` state.

    Deterministic and pure (unit-tested). Three boundaries fire:

    * **trend** — the CONFIRMED trend direction (up / down / neutral) changed;
    * **cross** — ``new`` shows a fresh golden/death cross that is genuinely new: a
      different cross state than stored, OR the same cross type but a strictly SMALLER
      ``days_ago`` (a newer cross replaced the old one). A cross merely AGEING (larger
      ``days_ago``) is not a new event;
    * **momentum** — a strict sign FLIP (positive↔negative). The ``flat`` dead-band and an
      unmeasured (``None``) momentum never fire — labelling those as a flip would fabricate
      a direction the rule never measured.
    """
    events: list[str] = []

    if _trend_direction(new.trend_state) != _trend_direction(stored.trend_state):
        events.append(EVENT_TREND)

    if new.cross_state in _CROSS_FRESH:
        newer = (
            stored.cross_state != new.cross_state
            or new.cross_days_ago is None
            or stored.cross_days_ago is None
            or new.cross_days_ago < stored.cross_days_ago
        )
        if newer:
            events.append(EVENT_CROSS)

    new_sign = _momentum_sign(new.momentum_state)
    old_sign = _momentum_sign(stored.momentum_state)
    if (
        new_sign is not None and old_sign is not None
        and new_sign != 0 and old_sign != 0 and new_sign != old_sign
    ):
        events.append(EVENT_MOMENTUM)

    return events


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
    params_version TEXT NOT NULL,
    as_of TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


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
        params_version=row["params_version"],
        as_of=row["as_of"],
        updated_at=row["updated_at"],
    )


def get_state(conn: sqlite3.Connection, symbol: str) -> SignalState | None:
    """The stored state row for ``symbol``, or ``None`` when never seeded."""
    row = conn.execute(
        "SELECT symbol, trend_state, cross_state, cross_days_ago, momentum_state, "
        "tech_score, evaluation_context, params_version, as_of, updated_at "
        "FROM signal_states WHERE symbol = ?",
        (symbol,),
    ).fetchone()
    return _row_to_state(row) if row is not None else None


def all_states(conn: sqlite3.Connection) -> list[SignalState]:
    """Every stored state row, ordered by symbol (for rebuild verification / listing)."""
    rows = conn.execute(
        "SELECT symbol, trend_state, cross_state, cross_days_ago, momentum_state, "
        "tech_score, evaluation_context, params_version, as_of, updated_at "
        "FROM signal_states ORDER BY symbol"
    ).fetchall()
    return [_row_to_state(r) for r in rows]


def upsert_state(
    conn: sqlite3.Connection,
    symbol: str,
    derived: DerivedState,
    *,
    params_version: str,
    as_of: str,
    updated_at: str,
) -> None:
    """Upsert one symbol's derived state (keyed on ``symbol``). ``tech_score`` is stored as
    a canonical Decimal STRING (full precision — a cache value, not money of record)."""
    tech_score = None if derived.tech_score is None else format(derived.tech_score, "f")
    conn.execute(
        "INSERT INTO signal_states (symbol, trend_state, cross_state, cross_days_ago, "
        "momentum_state, tech_score, evaluation_context, params_version, as_of, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(symbol) DO UPDATE SET trend_state=excluded.trend_state, "
        "cross_state=excluded.cross_state, cross_days_ago=excluded.cross_days_ago, "
        "momentum_state=excluded.momentum_state, tech_score=excluded.tech_score, "
        "evaluation_context=excluded.evaluation_context, "
        "params_version=excluded.params_version, as_of=excluded.as_of, "
        "updated_at=excluded.updated_at",
        (symbol, derived.trend_state, derived.cross_state, derived.cross_days_ago,
         derived.momentum_state, tech_score, derived.evaluation_context,
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
    "SignalState",
    "all_states",
    "clear_all",
    "detect_transitions",
    "ensure_table",
    "extract_state",
    "get_state",
    "upsert_state",
]
