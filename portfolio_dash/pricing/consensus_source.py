"""Per-symbol analyst-consensus client (Blueprint P1 batch 2, spec 20.x).

Fetches analyst *consensus* — target-price range + rating distribution — from
yfinance's two LIGHT endpoints (never the heavy/fragile ``Ticker.info``):

* ``Ticker(sym).analyst_price_targets`` → ``{current, high, low, mean, median}``.
* ``Ticker(sym).recommendations_summary`` → a per-period distribution
  (``strongBuy / buy / hold / sell / strongSell``) with a ``0m`` (this month) and
  ``-1m`` (last month) row.

These are *decision-support signals*, not numbers of record: the LLM only interprets
them (rules/llm-insight.md #1). Every numeric value is parsed through
``Decimal(str(x))`` — no float ever reaches the stored payload. Price-like numbers
(the target prices) inherit the **4dp float-noise cap** (ROUND_HALF_UP, cap-only-never-
pad) that ``pricing/store`` applies to quotes, since yfinance emits binary-float tails.
Counts are ints; ratios (rating score, upside) use their own stated quantization.

Both external endpoints are isolated in the two private ``_fetch_*`` seams so tests
monkeypatch them (the repo bans sockets in tests). A missing/empty/exception result
for a symbol yields ``None`` (no snapshot for it) so it degrades honestly downstream;
per-symbol isolation lives in the ingest loop.
"""

from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import yfinance as yf

SOURCE = "yfinance"
DATASET = "consensus"

# Price-target float-noise cap: 4 dp, ROUND_HALF_UP, CAP-only (never pad) — the same
# discipline as pricing/store.upsert_prices (target prices are price-like numbers).
_PRICE_DP = 4

# The rating-count columns yfinance returns, mapped to the payload's snake_case keys.
_RATING_KEYS: tuple[tuple[str, str], ...] = (
    ("strongBuy", "strong_buy"),
    ("buy", "buy"),
    ("hold", "hold"),
    ("sell", "sell"),
    ("strongSell", "strong_sell"),
)

# Weights for the local rating score (1=strong buy … 5=strong sell).
_RATING_WEIGHT: dict[str, int] = {
    "strong_buy": 1, "buy": 2, "hold": 3, "sell": 4, "strong_sell": 5,
}

_TARGET_FIELDS: tuple[str, ...] = ("current", "mean", "median", "high", "low")


def _to_decimal(value: object) -> Decimal | None:
    """Decimal(str(value)) if finite, else None (filters NaN/inf/None/garbage)."""
    if value is None or value == "":
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _cap4(v: Decimal) -> Decimal:
    """Round ``v`` to at most 4 decimals; values within the cap are unchanged.

    Mirrors ``pricing.store._cap_dp`` (kept local so this leaf module gains no
    coupling to a private name): the cap removes float representation noise, it does
    not pad — a clean value stores byte-identical.
    """
    exp = v.as_tuple().exponent
    if isinstance(exp, int) and exp < -_PRICE_DP:
        return v.quantize(Decimal(1).scaleb(-_PRICE_DP), rounding=ROUND_HALF_UP)
    return v


def _int(value: object) -> int:
    """A finite count as int; missing/NaN/garbage → 0 (an absent count is zero)."""
    d = _to_decimal(value)
    return int(d) if d is not None else 0


def _record_for(records: list[dict[str, Any]] | None, period: str) -> dict[str, Any] | None:
    """The recommendations record for a ``period`` label (e.g. ``"0m"``), or None."""
    if not records:
        return None
    for rec in records:
        if str(rec.get("period")) == period:
            return rec
    return None


def _ratings_from_record(rec: dict[str, Any] | None) -> dict[str, int] | None:
    """A ``{strong_buy … strong_sell, total}`` count dict from a period record, or None.

    ``None`` when the period row is absent. A present row with all-zero counts still
    yields a dict (``total == 0``); the caller decides whether that is meaningful.
    """
    if rec is None:
        return None
    counts = {snake: _int(rec.get(raw)) for raw, snake in _RATING_KEYS}
    counts["total"] = sum(counts.values())
    return counts


def _rating_score(ratings: dict[str, int] | None) -> str | None:
    """Weighted-mean rating (1=strong buy … 5=strong sell), 2dp, or None when total=0."""
    if ratings is None or ratings["total"] <= 0:
        return None
    weighted = sum(_RATING_WEIGHT[k] * ratings[k] for k in _RATING_WEIGHT)
    score = Decimal(weighted) / Decimal(ratings["total"])
    return str(score.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _price_target_decimals(targets: dict[str, Any] | None) -> dict[str, Decimal]:
    """Capped Decimal per present, finite target field (absent/garbage fields dropped)."""
    out: dict[str, Decimal] = {}
    if not targets:
        return out
    for field in _TARGET_FIELDS:
        d = _to_decimal(targets.get(field))
        if d is not None:
            out[field] = _cap4(d)
    return out


def _upside_vs_mean(decimals: dict[str, Decimal]) -> str | None:
    """``(mean − current) / current`` as a 4dp Decimal string; None when no honest base.

    None when either number is absent or ``current <= 0`` (no meaningful base) —
    mirrors the ratio discipline in ``portfolio.external_signals``.
    """
    current = decimals.get("current")
    mean = decimals.get("mean")
    if current is None or mean is None or current <= 0:
        return None
    ratio = (mean - current) / current
    return str(ratio.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def build_consensus(
    *,
    targets: dict[str, Any] | None,
    rec_records: list[dict[str, Any]] | None,
    as_of: date,
    source: str = SOURCE,
) -> dict[str, Any] | None:
    """Assemble the consensus snapshot payload from raw endpoint data (pure).

    Returns the LLM-facing payload (target prices as capped Decimal strings, rating
    counts as ints, locally-computed ``rating_score`` + ``upside_vs_mean_pct``), or
    ``None`` when there is no meaningful analyst data (no targets AND no rated period)
    — so an uncovered symbol writes no snapshot and degrades honestly.
    """
    decimals = _price_target_decimals(targets)
    ratings = _ratings_from_record(_record_for(rec_records, "0m"))
    ratings_prev = _ratings_from_record(_record_for(rec_records, "-1m"))

    has_targets = bool(decimals)
    has_ratings = ratings is not None and ratings["total"] > 0
    if not has_targets and not has_ratings:
        return None

    price_targets = (
        {f: str(v) for f, v in decimals.items()} if decimals else None
    )
    return {
        "as_of": as_of.isoformat(),
        "price_targets": price_targets,
        "ratings": ratings,
        "ratings_prev_month": ratings_prev,
        "rating_score": _rating_score(ratings),
        "upside_vs_mean_pct": _upside_vs_mean(decimals),
        "source": source,
    }


# --- External I/O seams (monkeypatched in tests; the only network in this module) ----


def _fetch_price_targets(symbol: str) -> dict[str, Any] | None:
    """Raw ``analyst_price_targets`` dict for ``symbol`` (yf), or None when empty."""
    data = yf.Ticker(symbol).analyst_price_targets
    return dict(data) if isinstance(data, dict) and data else None


def _fetch_recommendations(symbol: str) -> list[dict[str, Any]] | None:
    """``recommendations_summary`` as period records for ``symbol`` (yf), or None.

    Normalizes the DataFrame to ``[{period, strongBuy, …}, …]`` whether ``period`` is
    a column or the index (recent yfinance uses the index), so the pure builder always
    sees a ``period`` key.
    """
    df = yf.Ticker(symbol).recommendations_summary
    if df is None or getattr(df, "empty", True):
        return None
    columns = list(getattr(df, "columns", []))
    if "period" not in columns:
        df = df.reset_index()
    records: list[dict[str, Any]] = df.to_dict("records")
    for rec in records:
        if "period" not in rec and "index" in rec:
            rec["period"] = rec.pop("index")
    return records or None


def fetch_consensus(symbol: str, *, as_of: date) -> dict[str, Any] | None:
    """Fetch + assemble one symbol's consensus payload (yf), or None on any failure.

    ``symbol`` is the yfinance-mapped symbol (e.g. ``2330.TW`` / ``AAPL`` / ``1155.KL``).
    Any endpoint failure degrades to None so a bad symbol never crashes ingest and never
    fabricates a value.
    """
    try:
        targets = _fetch_price_targets(symbol)
    except Exception:  # noqa: BLE001 — any source failure degrades to None
        targets = None
    try:
        rec_records = _fetch_recommendations(symbol)
    except Exception:  # noqa: BLE001 — any source failure degrades to None
        rec_records = None
    return build_consensus(targets=targets, rec_records=rec_records, as_of=as_of)
