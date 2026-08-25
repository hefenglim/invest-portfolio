"""Pure quantitative scoring (spec 04.4): the LLM-free, objective verdict layer.

``score_quant`` compares a card's :class:`~llm_insight.cards.Prediction` against the actual
measurement FED IN by the api/pricing seam (``api/insight_service.py`` reads price-at-create
vs price-at-due, benchmark return, fx — never this layer). The function is total and pure:
given the same inputs it always returns the same verdict, so it is trivially unit-tested
with fixed fixtures (architecture.md).

Verdict semantics (spec 4.4 step 2):
- returns ``True``  → the prediction was objectively correct (a quant hit);
- returns ``False`` → objectively wrong (a quant miss);
- returns ``None``  → the actual value was unavailable → the caller defers as
  ``pending_data`` and NEVER force-judges a miss (spec 04.10 anti-poison).

``decide_miss`` folds the quant verdict with the master's narrative score into the combined
miss flag; ``calibration_error`` computes the confidence-vs-hit-rate gap in percentage
points. All math is :class:`~decimal.Decimal` (never float for a price/rate/ratio).
"""

from decimal import Decimal

from pydantic import BaseModel

from portfolio_dash.llm_insight.cards import Prediction

# The flat band: |move| within ±0.5% counts as "flat" for a direction=flat prediction.
_FLAT_BAND = Decimal("0.005")
# Volatility gets its own, wider band (AI-D25, owner ruling 2026-08-19): ``vol_change_pct``
# is the fractional change of a 30-day realized-vol ESTIMATOR, which jitters by several
# percent on a constant series — at ±0.5% a ``direction=flat`` volatility call would be an
# automatic miss, silently punishing one direction class. Price moves and excess returns
# keep the shared band above.
_VOL_FLAT_BAND = Decimal("0.05")


class ActualMeasurement(BaseModel):
    """The objective measurement an evaluation needs, fed by the api/pricing seam.

    Only the field(s) a given metric needs are populated; the rest stay ``None``. A ``None``
    in the field a metric requires means "unavailable" → ``score_quant`` returns ``None``
    (pending_data, not a miss).

    - ``price_change_pct`` — fractional move create→due (0.03 = +3%); for ``price_change``.
    - ``vol_change_pct``   — fractional change in realized volatility; for ``volatility``.
    - ``symbol_return_pct`` / ``benchmark_return_pct`` — fractional returns; for ``relative``.
    """

    model_config = {"arbitrary_types_allowed": True}

    price_change_pct: Decimal | None = None
    vol_change_pct: Decimal | None = None
    symbol_return_pct: Decimal | None = None
    benchmark_return_pct: Decimal | None = None


def _direction_hit(direction: str, move: Decimal, *, band: Decimal = _FLAT_BAND) -> bool:
    """Whether a signed *move* matches a directional claim (with a flat band for ``flat``)."""
    if direction == "flat":
        return abs(move) <= band
    if direction == "up":
        return move > Decimal("0")
    return move < Decimal("0")  # down


def _score_price_change(pred: Prediction, m: ActualMeasurement) -> bool | None:
    move = m.price_change_pct
    if move is None:
        return None
    if pred.target_pct is not None and pred.direction in ("up", "down"):
        # Magnitude claim: the move must reach the target in the predicted direction.
        target = abs(pred.target_pct)
        if pred.direction == "up":
            return move >= target
        return move <= -target
    return _direction_hit(pred.direction, move)


def _score_volatility(pred: Prediction, m: ActualMeasurement) -> bool | None:
    change = m.vol_change_pct
    if change is None:
        return None
    # A volatility prediction is a regime call: up = vol rose, down = vol fell, flat = stable.
    # The flat arm uses the wider vol-specific band (AI-D25) — see ``_VOL_FLAT_BAND``.
    return _direction_hit(pred.direction, change, band=_VOL_FLAT_BAND)


def _score_relative(pred: Prediction, m: ActualMeasurement) -> bool | None:
    if m.symbol_return_pct is None or m.benchmark_return_pct is None:
        return None
    excess = m.symbol_return_pct - m.benchmark_return_pct
    return _direction_hit(pred.direction, excess)


def score_quant(prediction: Prediction, actual: ActualMeasurement | None) -> bool | None:
    """Objectively verify a prediction against the fed actual measurement (spec 4.4).

    Returns ``True``/``False`` for a clear hit/miss, or ``None`` when the actual value is
    unavailable (→ caller defers as pending_data, never a miss). Pure + total.
    """
    if actual is None:
        return None
    if prediction.metric == "price_change":
        return _score_price_change(prediction, actual)
    if prediction.metric == "volatility":
        return _score_volatility(prediction, actual)
    return _score_relative(prediction, actual)


def decide_miss(
    *, quant_hit: bool | None, narrative_score: int | None, threshold: int
) -> bool:
    """Combine the objective quant verdict + the master narrative score into the miss flag.

    Rules (deterministic — the LLM never decides this; spec 4.8):
    - an objective quant miss (``quant_hit is False``) is a miss regardless of narrative;
    - a quant hit (``True``) is never a miss (the verifiable claim held);
    - with no quant signal (pure-narrative card), a narrative score below *threshold* is a
      miss; at/above *threshold* it is not;
    - no signal at all (quant None + narrative None — master skipped) cannot be judged → not
      a miss (anti-poison: an unjudgeable card never counts against the combo).
    """
    if quant_hit is False:
        return True
    if quant_hit is True:
        return False
    if narrative_score is None:
        return False
    return narrative_score < threshold


def should_calibrate(
    *,
    resolved_samples: int,
    min_samples: int,
    consecutive_misses: int,
    miss_count: int,
    gap_alert_pp: Decimal,
) -> bool:
    """Whether a self_correct combo should generate a new calibration version (spec 4.5).

    Gated FIRST by ``resolved_samples >= min_samples`` (spec 04.10 — small samples never
    trigger). Then ANY of: ≥3 consecutive misses, OR a miss rate exceeding ``gap_alert_pp``
    percentage points. (Output-rule violations are recorded by the validator on generation;
    they are not a separate pre-trigger here.) Pure + deterministic — the LLM never decides
    whether to calibrate, only what the new text is (spec 4.8).
    """
    if resolved_samples < min_samples or resolved_samples == 0:
        return False
    if consecutive_misses >= 3:
        return True
    miss_rate_pp = (Decimal(miss_count) / Decimal(resolved_samples)) * Decimal("100")
    return miss_rate_pp > gap_alert_pp


def calibration_error(rows: list[tuple[int, bool]]) -> Decimal:
    """Calibration error in percentage points: |avg claimed confidence − actual hit rate|.

    *rows* is ``[(confidence_0_100, hit_bool), ...]``. Empty → ``0``. The result is an exact
    Decimal (percentage points), never float.
    """
    if not rows:
        return Decimal("0")
    n = Decimal(len(rows))
    claimed = sum((Decimal(c) for c, _ in rows), Decimal("0")) / n
    hits = sum(1 for _, hit in rows if hit)
    actual = (Decimal(hits) / n) * Decimal("100")
    return abs(claimed - actual)


# --- Trust tier (W7, AI-D36) ------------------------------------------------------
# The scoreboard's per-task stamp — 命中率 × 校準誤差 × 樣本數 in ONE vocabulary, computed
# HERE (the web layer renders the string; it never computes it). Anchors: the sample gate
# is the event study's MIN_SAMPLE (= the evolution config's min_samples default); the
# calibration bar is the calib_gap alert's gap_alert_pp default.
TIER_MIN_SAMPLE = 8
TIER_SUCCESS_MIN = Decimal("0.6")
TIER_CALIB_MAX_PP = Decimal("10")

TIER_SUFFICIENT = "可參考"
TIER_EARLY = "早期"
TIER_INSUFFICIENT = "樣本不足"


def trust_tier(
    *,
    n: int,
    quant_n: int,
    quant_hit_rate: Decimal | None,
    narrative_success_rate: Decimal | None,
    calib_error_pp: Decimal | None,
) -> str:
    """One task's trust tier from its hit rate × calibration error × sample count.

    * ``n < TIER_MIN_SAMPLE`` → 樣本不足 (regardless of the rates — a handful of hits is
      noise theatre, the same honesty gate as the event study and the rolling gap).
    * The success leg is ``quant_hit_rate`` when quant rows exist — ``combo_score`` reports
      ``"0"`` for a quant-less combo, and reading that as a real 0% would demote every
      narrative-only task on a number it never earned — else ``narrative_success_rate``.
    * 可參考 ⟺ success ≥ 0.6 AND the calibration error is KNOWN and ≤ 10pp; a combo with
      no calibration evidence (None) is 早期, not 可參考 — "no evidence of miscalibration"
      is not evidence of calibration.
    """
    if n < TIER_MIN_SAMPLE:
        return TIER_INSUFFICIENT
    success = quant_hit_rate if quant_n > 0 else narrative_success_rate
    if success is None:
        return TIER_EARLY
    if (
        success >= TIER_SUCCESS_MIN
        and calib_error_pp is not None
        and calib_error_pp <= TIER_CALIB_MAX_PP
    ):
        return TIER_SUFFICIENT
    return TIER_EARLY


# --- Confidence ceiling (W7.1, 2026-08-23, owner ruling 「生產端＋提示詞雙邊修」) ----------
# AI-D33 put the anchoring law in the PROMPT and deliberately refused a code-side clamp: a
# validator that silently rewrites the model's own stated confidence is the same defect class
# as averaging two providers' fundamentals. The first live run (demo, 2026-08-23) exposed the
# other failure mode — 0 of 13 advice cards obeyed a law that asked the model to execute a
# three-step conditional over a bins table mid-generation.
#
# So the ARITHMETIC moves here and the RULE stays exactly where it was. The prompt still
# states the ceiling, the model still writes the number, and nothing clamps anything after
# the fact; the model is simply handed ONE integer instead of a table plus a procedure.
#
# ⚠ REVISED 2026-08-25 (AI-D39), and the revision is to the LAW, not to this implementation:
# W7.1 executed AI-D33's three steps faithfully and two of them were wrong.
#
#   (a) The gap was subtracted ON TOP of the bucket cap. A bucket's cap is `actual_pct + 5`,
#       which IS the calibration correction — deducting the global rolling gap as well charges
#       the same over-confidence twice. Measured on the demo: bins said 39, the gap took 47,
#       the answer was 0. That 0 was arithmetic, not evidence. The step is gone, and with it
#       the `gap` parameter: a parameter kept "for compatibility" after it stops being read is
#       how the next reader concludes the gap still matters.
#   (b) A bucket nobody had scored capped at a CONSTANT 70, while a bucket measured at 40%
#       capped at 45 — so ignorance outranked evidence, and a model could lift its own ceiling
#       by moving into an unscored range. Over a 20-sample rolling window that is a limit
#       cycle, not a calibration loop. An unscored bucket now caps at the OVERALL record.
#
# AI-D33's red line is untouched: nothing here clamps what the model writes. Only the integer
# handed to the prompt changes. (The third step, recording violations instead of clamping, is
# a separate surface — see `insights.ceiling_at_create`.)
CEILING_NO_DATA = 70  # LAST RESORT ONLY: nothing has ever been scored, so there is no record
CEILING_HEADROOM = 5  # the law's "actual_pct + 5"


def _bucket_bounds(label: str) -> tuple[int, int] | None:
    """``"40-60"`` -> ``(40, 60)``; anything unparseable -> ``None`` (never raise on a bin)."""
    lo, _, hi = label.partition("-")
    try:
        return int(lo), int(hi)
    except ValueError:
        return None


def confidence_ceiling(
    bins: list[dict[str, object]],
    *,
    overall_hit_pct: Decimal | None,
    min_samples: int = TIER_MIN_SAMPLE,
) -> int:
    """The largest SELF-CONSISTENT confidence (0-100) the anchoring law admits.

    The law is circular by construction — a confidence's cap depends on the bucket that same
    confidence falls into — so the answer is the largest ``c`` with ``c <= cap(bucket(c))``:

    * a bucket at or above the sample gate caps at ``actual_pct + CEILING_HEADROOM``;
    * a bucket below the gate, or absent from ``bins`` entirely, caps at
      ``overall_hit_pct + CEILING_HEADROOM`` — the record as a whole, since this particular
      range has none of its own (AI-D39 (b));
    * only when NOTHING has ever been scored (``overall_hit_pct is None``) does
      ``CEILING_NO_DATA`` apply. That is a genuinely different state from "this bucket has no
      history", and it is the only one a constant can honestly answer.

    ⚠ ``overall_hit_pct`` is a PERCENTAGE (0-100), the same unit as a bin's ``actual_pct`` —
    NOT the fraction that ``backtest_json.overall_hit_rate`` carries on the wire. The caller
    converts, visibly. Mixing the two silently is precisely the class of defect W7.1 shipped
    (one score printed four different ways), so the unit is in the name.

    ⚠ There is deliberately NO floor: on a badly calibrated history this returns a small
    number, and the prompt says so in words rather than having this layer invent a floor the
    owner never ruled.
    """
    caps: list[tuple[int, int, Decimal]] = []
    # The fallback for a range with no record of its own: the record as a whole.
    unscored_cap = (Decimal(CEILING_NO_DATA) if overall_hit_pct is None
                    else overall_hit_pct + CEILING_HEADROOM)
    for b in bins:
        bounds = _bucket_bounds(str(b.get("bucket", "")))
        if bounds is None:
            continue
        n = int(str(b.get("n", 0)))
        cap = (
            Decimal(str(b.get("actual_pct", "0"))) + CEILING_HEADROOM
            if n >= min_samples
            else unscored_cap
        )
        caps.append((bounds[0], bounds[1], cap))

    def cap_for(c: int) -> Decimal:
        for lo, hi, cap in caps:
            if lo <= c < hi or (hi == 100 and c == 100):
                return cap
        return unscored_cap  # a bucket nobody has ever scored

    for c in range(100, -1, -1):
        if Decimal(c) <= cap_for(c):
            return c
    return 0
