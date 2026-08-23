"""Event-study backtest over signal history (W6 — AI-D29/AI-D30/AI-D32). Pure functions.

Given the per-day signal state vector (``signal_history`` rows, mapped to
:class:`HistoryPoint` by the caller) and the symbol's close series, answer: **what
happened after this signal fired in the past?** — forward-return distributions at
+20/+60/+120 trading days after each event, against the SAME symbol's unconditional
distribution as the baseline (AI-D30).

Honesty guards (spec §4 / AI-D30 — these are the point of the module, not decorations):

* **min sample** — a (kind, direction, window) cell with fewer than ``MIN_SAMPLE``
  eligible events reports ``stats=None`` (「不足以判斷」): the event count is shown, the
  distribution numbers are NOT. A plausible-looking mean over 5 samples is the failure
  mode this guard exists to prevent.
* **overlap annotation** — an event re-firing within one window length of the previous
  ELIGIBLE event of its group FOR THAT WINDOW is counted in ``n_overlapping`` (it stays
  in the stats; the count is the disclosure that the samples are not independent).
* **right-censoring** — events whose forward window extends past the latest close are
  EXCLUDED from that window's stats and counted in ``n_censored`` (a +120-day window
  needs 120 trading days that have not happened yet for recent events).
* **no annualization, no Sharpe** — the study reports the window distributions as they
  are; nothing is extrapolated.

Hold semantics (mirroring ``signal_states.detect_transitions`` — one behavioural
vocabulary): a rule event fires when the score's SIGN differs from the last remembered
NON-ZERO sign; ``None`` (not evaluable) and ``0`` (neutral) HOLD the remembered sign — a
dead-band dwell neither resets nor re-fires, so a genuine reversal fires exactly once and
first sightings are silent (a first sighting is a seed, not an event). Composite events
use the same machinery on the tech_score ZONES: entering the high band (≥ band_high) is
bullish, entering the low band (≤ band_low) is bearish, the mid zone HOLDS.

The band values are NOT defined here — they are the rule engine's state bands
(``strategy.rules.composite``), injected by the caller as required arguments (a second
band vocabulary on the same prompt surface is the AI-D2 two-definitions defect; AI-D32).
This module imports stdlib only — it must stay importable by ``portfolio/`` consumers
without pulling ``strategy/`` (the dependency direction is strategy → portfolio, never
the reverse).

Caller obligations: ``closes`` ascending ``(date, close)`` in ONE consistent share basis —
re-express via ``price_basis.series_in`` first (AI-D30/W6c: an un-re-expressed split reads
as a −86% "return", fabricating both the event outcomes and the baseline); local currency
(AI-D23). ``points`` are the same symbol's history rows, ascending, one rule vintage only
(the store's ``params_version`` filter).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

# The study's own vocabulary (AI-D30): trading-day forward windows and the minimum
# eligible-event count below which a cell reports 「不足以判斷」 instead of numbers
# (aligned with the design mock's confidence-cap rule — 樣本 <8 → 信心上限 0.7).
FORWARD_WINDOWS: tuple[int, ...] = (20, 60, 120)
MIN_SAMPLE = 8

COMPOSITE_KIND = "composite"
BULLISH = "bullish"
BEARISH = "bearish"


@dataclass(frozen=True)
class HistoryPoint:
    """One day's signal state vector (a ``signal_history`` row, mapped by the caller).

    ``scores`` maps rule name → the rule's signed score that day (``None`` = not
    evaluable — holds the remembered sign, exactly like a neutral ``0``). The study
    detects rule events PURELY from these scores, so they are the load-bearing fields.
    """

    as_of: date
    scores: Mapping[str, Decimal | None]
    tech_score: Decimal | None


@dataclass(frozen=True)
class SignalEvent:
    """One detected event: ``kind`` is the rule name or ``"composite"``."""

    kind: str
    direction: str  # BULLISH | BEARISH
    as_of: date


@dataclass(frozen=True)
class WindowStats:
    """A forward-return distribution summary over ``n`` samples (full precision)."""

    n: int
    mean: Decimal
    median: Decimal
    pct_positive: Decimal  # fraction in [0, 1]


@dataclass(frozen=True)
class WindowOutcome:
    """One (event group × window) cell. ``stats`` is None when ``n < min_sample``."""

    window: int
    n: int               # eligible events (placed, not right-censored, non-zero base)
    n_overlapping: int   # eligible events within one window of the previous eligible one
    # Placed events excluded here (window past the last close, or a zero/defective base):
    n_censored: int
    stats: WindowStats | None


@dataclass(frozen=True)
class EventGroup:
    kind: str
    direction: str
    events: int                          # detected and placed, before censoring
    outcomes: tuple[WindowOutcome, ...]  # one per requested window, in order


@dataclass(frozen=True)
class EventStudy:
    """The full per-symbol study. ``baselines`` carries one entry per window (None when
    the close series is too short for that window at all)."""

    events_total: int
    # Events whose date has no close (impossible by construction; counted, never silent):
    events_without_price: int
    groups: tuple[EventGroup, ...]
    baselines: tuple[WindowStats | None, ...]


def detect_events(
    points: Sequence[HistoryPoint],
    *,
    band_high: Decimal,
    band_low: Decimal,
) -> list[SignalEvent]:
    """Detect rule sign-change + composite zone-entry events over ascending ``points``.

    See the module docstring for the hold semantics. Rule iteration is sorted by name so
    same-day multi-rule events have a deterministic order.
    """
    events: list[SignalEvent] = []
    names = sorted({name for p in points for name in p.scores})
    last_sign: dict[str, int] = {}
    last_zone: str | None = None
    for p in points:
        for name in names:
            score = p.scores.get(name)
            if score is None or score == 0:
                continue  # unevaluable / neutral → HOLD the remembered sign
            sign = 1 if score > 0 else -1
            remembered = last_sign.get(name)
            if remembered is not None and sign != remembered:
                events.append(SignalEvent(
                    kind=name,
                    direction=BULLISH if sign > 0 else BEARISH,
                    as_of=p.as_of,
                ))
            last_sign[name] = sign
        ts = p.tech_score
        if ts is not None:
            zone = (
                "high" if ts >= band_high
                else ("low" if ts <= band_low else None)
            )
            if zone is not None:
                if last_zone is not None and zone != last_zone:
                    events.append(SignalEvent(
                        kind=COMPOSITE_KIND,
                        direction=BULLISH if zone == "high" else BEARISH,
                        as_of=p.as_of,
                    ))
                last_zone = zone
    return events


def _forward_return(
    closes: Sequence[tuple[date, Decimal]], i: int, window: int
) -> Decimal | None:
    """``(closes[i+window] − closes[i]) / closes[i]``; None when censored or zero base.

    Subtract-first, matching ``insight_service._window_return``: at return magnitudes
    ≈ 0 the divide-first form (``a/b − 1``) spends significant digits on the leading
    ``1.0`` and truncates the fraction's tail — the subtraction form keeps full precision
    on the figure actually reported.
    """
    if i + window >= len(closes):
        return None
    base = closes[i][1]
    if base == 0:
        return None  # a zero close is a data defect; it can never be a denominator
    return (closes[i + window][1] - base) / base


def _stats(returns: list[Decimal]) -> WindowStats | None:
    if not returns:
        return None
    n = len(returns)
    ordered = sorted(returns)
    mid = n // 2
    median = ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    positive = sum(1 for r in returns if r > 0)
    total = Decimal(0)  # explicit accumulation — sum() over Decimals types as Decimal|float
    for r in returns:
        total += r
    return WindowStats(
        n=n,
        mean=total / n,
        median=median,
        pct_positive=Decimal(positive) / n,
    )


def _group_outcome(
    kind: str,
    direction: str,
    placed: list[tuple[SignalEvent, int]],
    closes: Sequence[tuple[date, Decimal]],
    windows: tuple[int, ...],
    min_sample: int,
) -> EventGroup:
    outcomes: list[WindowOutcome] = []
    for w in windows:
        eligible = [
            (e, i) for e, i in placed if _forward_return(closes, i, w) is not None
        ]
        # Overlap: an eligible event within one window length of the PREVIOUS eligible
        # event of this group — the samples share price path and are not independent.
        n_overlapping = sum(
            1
            for prev, cur in zip(eligible, eligible[1:], strict=False)
            if cur[1] - prev[1] < w
        )
        returns = [
            r for _, i in eligible
            if (r := _forward_return(closes, i, w)) is not None
        ]
        outcomes.append(WindowOutcome(
            window=w,
            n=len(eligible),
            n_overlapping=n_overlapping,
            n_censored=len(placed) - len(eligible),
            stats=_stats(returns) if len(returns) >= min_sample else None,
        ))
    return EventGroup(
        kind=kind, direction=direction, events=len(placed),
        outcomes=tuple(outcomes),
    )


def event_study(
    points: Sequence[HistoryPoint],
    closes: Sequence[tuple[date, Decimal]],
    *,
    band_high: Decimal,
    band_low: Decimal,
    min_sample: int = MIN_SAMPLE,
    windows: tuple[int, ...] = FORWARD_WINDOWS,
) -> EventStudy:
    """Run the event study for one symbol. See the module docstring for the guards.

    ``points`` must be ascending and single-vintage; ``closes`` ascending and already
    split-re-expressed into one share basis (caller obligation — see the module docstring).
    """
    events = detect_events(points, band_high=band_high, band_low=band_low)
    index_of = {d: i for i, (d, _) in enumerate(closes)}
    placed: list[tuple[SignalEvent, int]] = []
    without_price = 0
    for event in events:
        i = index_of.get(event.as_of)
        if i is None:
            without_price += 1  # impossible when both come from `prices`; counted, never silent
        else:
            placed.append((event, i))

    grouped: dict[tuple[str, str], list[tuple[SignalEvent, int]]] = {}
    for event, i in placed:
        grouped.setdefault((event.kind, event.direction), []).append((event, i))

    groups = tuple(
        _group_outcome(kind, direction, members, closes, windows, min_sample)
        for (kind, direction), members in sorted(grouped.items())
    )
    baselines = tuple(
        _stats([
            r
            for i in range(len(closes))
            if (r := _forward_return(closes, i, w)) is not None
        ])
        for w in windows
    )
    return EventStudy(
        events_total=len(events),
        events_without_price=without_price,
        groups=groups,
        baselines=baselines,
    )


__all__ = [
    "BEARISH",
    "BULLISH",
    "COMPOSITE_KIND",
    "FORWARD_WINDOWS",
    "MIN_SAMPLE",
    "EventGroup",
    "EventStudy",
    "HistoryPoint",
    "SignalEvent",
    "WindowOutcome",
    "WindowStats",
    "detect_events",
    "event_study",
]
