"""Rule ②: SMA(50) vs SMA(200) golden/death cross with volume confirmation + decay.

Detects the most recent golden (fast crosses above slow) / death (below) cross within a
bounded lookback, then scores it by:

* **base** — golden ``+1`` / death ``-1``;
* **confidence modifier** — the empirical volume-confirmation edge (high-volume
  confirmed ~72% vs unconfirmed ~54%; ``0.75 = 54/72``) encoded directly:
  ``×1.00`` confirmed · ``×0.75`` unconfirmed · ``×0.85`` unknown (volume absent / gap
  at the cross — never faked). If volume confirmation is disabled the modifier is
  ``1.00``. Caveat: the 72/54 figure is INDEX-level evidence (S&P, 33 signals over
  66y); applied per-symbol it mildly overstates confidence — individual stocks are
  noisier than indices (blueprint §1.2);
* **age decay** — a cross fades: a documented LINEAR decay to 0 over
  ``decay_sessions`` sessions (default 60 — calibrated to the death-cross evidence
  that the signal is ~random after ~30 days: half-weight at day 30, fully handed back
  to the standing relationship by ~3 months).

When no cross is found within the lookback the state reports the *standing* fast-vs-slow
relationship at a reduced magnitude (``fast_above`` ``+0.4`` / ``fast_below`` ``-0.4`` /
``aligned`` ``0``). ``cross_lookback`` equals ``decay_sessions`` by default, and the
detection window EXCLUDES the fully-decayed boundary (``days_ago < decay_sessions``),
so a detected cross always carries a strictly non-zero contribution and anything older
falls through to the relationship read — no "detected but score 0" state, no score jump
at the boundary. Pure Decimal; fewer than ``slow`` closes → ``None``.
"""

from collections.abc import Sequence
from decimal import Decimal

from portfolio_dash.strategy.rules.params import MaCrossParams
from portfolio_dash.strategy.rules.types import RuleState

_ZERO = Decimal("0")
_ONE = Decimal("1")
_REL_MAG = Decimal("0.4")
_MOD_CONFIRMED = Decimal("1.00")
_MOD_UNCONFIRMED = Decimal("0.75")
_MOD_UNKNOWN = Decimal("0.85")


def _sign_series(closes: list[Decimal], fast: int, slow: int) -> tuple[list[int], Decimal, Decimal]:
    """Sign of (fastSMA - slowSMA) at every session where both MAs exist (O(n)).

    Returns ``(signs, fast_ma_last, slow_ma_last)``. ``signs[j]`` is ``+1`` when the
    fast MA is ``>=`` the slow MA at session ``slow-1+j``, else ``-1``.
    """
    slow_sum = sum(closes[:slow], _ZERO)
    fast_sum = sum(closes[slow - fast:slow], _ZERO)
    fast_ma = fast_sum / Decimal(fast)
    slow_ma = slow_sum / Decimal(slow)
    signs = [1 if fast_ma >= slow_ma else -1]
    for t in range(slow, len(closes)):
        slow_sum += closes[t] - closes[t - slow]
        fast_sum += closes[t] - closes[t - fast]
        fast_ma = fast_sum / Decimal(fast)
        slow_ma = slow_sum / Decimal(slow)
        signs.append(1 if fast_ma >= slow_ma else -1)
    return signs, fast_ma, slow_ma


def _volume_confirmed(
    volumes: list[Decimal | None] | None, cross_t: int, window: int
) -> bool | None:
    """Cross-day volume vs the ``window``-bar average BEFORE the cross.

    ``True`` when cross-day volume exceeds that average, ``False`` when it does not,
    ``None`` when it cannot be judged (no volumes, not enough pre-cross bars, or a
    ``None`` gap in the cross day or the window) — unknown is never faked as confirmed.
    """
    if volumes is None or cross_t - window < 0 or cross_t >= len(volumes):
        return None
    cross_vol = volumes[cross_t]
    before = volumes[cross_t - window:cross_t]
    if cross_vol is None or any(v is None for v in before):
        return None
    before_vals = [v for v in before if v is not None]
    avg = sum(before_vals, _ZERO) / Decimal(window)
    if avg == _ZERO:
        return None
    return cross_vol > avg


def evaluate(
    closes: list[Decimal],
    volumes: Sequence[Decimal | None] | None,
    params: MaCrossParams,
) -> RuleState | None:
    """Most-recent 50/200 cross (volume-confirmed, age-decayed) or standing relationship.

    ``volumes`` is aligned index-for-index with ``closes`` (``None`` marks a gap
    session). Needs at least ``params.slow`` closes (for one MA pair); ``slow + 1`` to
    detect a cross.
    """
    n = len(closes)
    fast, slow = params.fast, params.slow
    if fast <= 0 or slow <= fast or n < slow:
        return None

    vols = list(volumes) if volumes is not None else None
    signs, fast_ma_last, slow_ma_last = _sign_series(closes, fast, slow)
    window_days = min(n, slow + params.cross_lookback)

    # Standing relationship at the latest session (used when no fresh cross is found).
    if fast_ma_last > slow_ma_last:
        rel_state, rel_score = "fast_above", _REL_MAG
    elif fast_ma_last < slow_ma_last:
        rel_state, rel_score = "fast_below", -_REL_MAG
    else:
        rel_state, rel_score = "aligned", _ZERO

    # Scan newest→oldest for the most recent sign flip within cross_lookback. The
    # fully-decayed boundary (days_ago == decay_sessions) is EXCLUDED so a detected
    # cross is always strictly non-zero (deep review 2026-07-10).
    latest = len(signs) - 1  # days_ago == latest - j
    lo = max(1, latest - params.cross_lookback + 1)
    cross: str | None = None
    days_ago: int | None = None
    cross_j: int | None = None
    for j in range(latest, lo - 1, -1):
        if signs[j] != signs[j - 1]:
            cross = "golden" if signs[j] > 0 else "death"
            days_ago = latest - j
            cross_j = j
            break

    if cross is None or days_ago is None or cross_j is None:
        # No fresh cross → report the standing relationship (no decay / no modifier).
        evidence: dict[str, object] = {
            "cross": None,
            "days_ago": None,
            "relationship": rel_state,
            "fast_ma": fast_ma_last,
            "slow_ma": slow_ma_last,
            "fast_window": fast,
            "slow_window": slow,
            "volume_confirmed": None,
            "volume_confirm_enabled": params.volume_confirm,
            "confidence_modifier": _ONE,
            "decay_factor": _ONE,
        }
        return RuleState(rel_state, rel_score, evidence, window_days)

    cross_t = slow - 1 + cross_j  # absolute index of the cross session

    if params.volume_confirm:
        vconf = _volume_confirmed(vols, cross_t, params.volume_window)
        if vconf is True:
            modifier = _MOD_CONFIRMED
        elif vconf is False:
            modifier = _MOD_UNCONFIRMED
        else:
            modifier = _MOD_UNKNOWN
    else:
        vconf = None
        modifier = _ONE

    decay = _ONE - Decimal(days_ago) / Decimal(params.decay_sessions)
    if decay < _ZERO:
        decay = _ZERO

    base = _ONE if cross == "golden" else -_ONE
    score = base * modifier * decay

    evidence = {
        "cross": cross,
        "days_ago": days_ago,
        "relationship": rel_state,
        "fast_ma": fast_ma_last,
        "slow_ma": slow_ma_last,
        "fast_window": fast,
        "slow_window": slow,
        "volume_confirmed": vconf,
        "volume_confirm_enabled": params.volume_confirm,
        "confidence_modifier": modifier,
        "decay_factor": decay,
    }
    return RuleState(cross, score, evidence, window_days)
