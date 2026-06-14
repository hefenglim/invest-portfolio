"""Pure Decimal derivations over external snapshots (spec 20.5).

The calculation core for the chips/sentiment variables: it turns a trailing window
of raw snapshot numbers (institutional net buy, margin balances, monthly revenue,
PER/PBR history, VIX) into the values the LLM reasons about. It lives in
``portfolio/`` (not ``llm_insight/``) for the same reason ``technicals.py`` does:
**the LLM never emits numbers of record** — every signal here is a pure, unit-tested
function and only *assembled* into a prompt downstream.

Discipline (``rules/data-and-pricing.md``):

* Everything is :class:`~decimal.Decimal`. Daily-net sequences are ``list[Decimal]``
  in chronological order (oldest first, newest last) — mirroring ``technicals.py``.
* Every *ratio* returns ``None`` when its denominator is ``<= 0`` (domain-ledger
  discipline), so the caller renders it as missing rather than a fabricated number.
* No I/O, no connection — inputs are already-read values.
"""

from decimal import Decimal, InvalidOperation
from typing import Any

_ZERO = Decimal("0")


def consecutive_buy_days(daily_net: list[Decimal]) -> int:
    """Length of the trailing run of strictly-positive net values (newest last).

    Counts back from the newest end while values are ``> 0``; a zero or negative
    breaks the run. ``0`` when the newest value is not positive or the list is empty.
    """
    count = 0
    for value in reversed(daily_net):
        if value > _ZERO:
            count += 1
        else:
            break
    return count


def consecutive_sell_days(daily_net: list[Decimal]) -> int:
    """Length of the trailing run of strictly-negative net values (newest last)."""
    count = 0
    for value in reversed(daily_net):
        if value < _ZERO:
            count += 1
        else:
            break
    return count


def net_buy_sum(daily_net: list[Decimal], days: int) -> Decimal:
    """Sum of the last ``days`` net values (fewer if the series is shorter)."""
    if days <= 0:
        return _ZERO
    return sum(daily_net[-days:], _ZERO)


def chg_pct(curr: Decimal, prev: Decimal) -> Decimal | None:
    """``(curr - prev) / prev``; ``None`` when ``prev <= 0`` (no honest base)."""
    if prev <= _ZERO:
        return None
    return (curr - prev) / prev


def yoy(curr: Decimal, year_ago: Decimal) -> Decimal | None:
    """Year-over-year change rate; ``None`` when ``year_ago <= 0``."""
    return chg_pct(curr, year_ago)


def mom(curr: Decimal, last_month: Decimal) -> Decimal | None:
    """Month-over-month change rate; ``None`` when ``last_month <= 0``."""
    return chg_pct(curr, last_month)


def percentile(value: Decimal, history: list[Decimal]) -> Decimal | None:
    """Fraction of historical values ``<= value`` (a rank in ``[0, 1]``).

    ``None`` for empty history. Uses ``<=`` so a value at the historical max scores
    ``1`` and below the historical min scores ``0``.
    """
    if not history:
        return None
    at_or_below = sum(1 for h in history if h <= value)
    return Decimal(at_or_below) / Decimal(len(history))


def vix_zone(vix: Decimal) -> str:
    """Classify a VIX level: ``<15`` low · ``15-25`` normal · ``25-35`` elevated · ``>=35`` high."""
    if vix < Decimal("15"):
        return "low"
    if vix < Decimal("25"):
        return "normal"
    if vix < Decimal("35"):
        return "elevated"
    return "high"


# --- Snapshot -> variable assemblers (pure; LLM-facing dicts; spec 20.2/20.5) ---
# These take already-read snapshot rows (the router does the conn-bearing read) and
# return JSON-able dicts. Numeric values are emitted as canonical strings (no float),
# matching the Decimal-string wire contract; ``None`` survives for missing ratios.

_UNAVAILABLE: dict[str, Any] = {"unavailable": True, "last_as_of": None}


def to_decimal(value: Any) -> Decimal | None:
    """Decimal(str(value)) if finite, else None (filters None/''/garbage).

    Public so the router can parse stored snapshot strings before handing values to the
    assemblers, keeping the float ban (``Decimal(str(x))``) in one place.
    """
    if value is None or value == "":
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


# Internal alias kept for the assemblers below (call sites use ``to_decimal``).
_dec = to_decimal


def _s(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _sorted_by_date(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows in ascending ``date`` order (rows without a parseable date sort first)."""
    def key(row: dict[str, Any]) -> str:
        raw = row.get("date")
        return raw if isinstance(raw, str) else ""
    return sorted(rows, key=key)


def build_institutional(
    rows: list[dict[str, Any]], *, symbol: str, as_of: str
) -> dict[str, Any]:
    """Foreign-investor net-buy trend + consecutive-buy streak (FinMind institutional)."""
    foreign = _sorted_by_date(
        [r for r in rows if r.get("name") == "Foreign_Investor"]
    )
    if not foreign:
        return dict(_UNAVAILABLE)
    daily_net: list[Decimal] = []
    for row in foreign:
        buy = _dec(row.get("buy")) or _ZERO
        sell = _dec(row.get("sell")) or _ZERO
        daily_net.append(buy - sell)
    return {
        "symbol": symbol,
        "last_as_of": as_of,
        "consecutive_buy_days": consecutive_buy_days(daily_net),
        "consecutive_sell_days": consecutive_sell_days(daily_net),
        "foreign_net_total": _s(net_buy_sum(daily_net, len(daily_net))),
        "foreign_net_20d": _s(net_buy_sum(daily_net, 20)),
    }


def build_margin(rows: list[dict[str, Any]], *, symbol: str, as_of: str) -> dict[str, Any]:
    """Margin / short balances + their change over the window (FinMind margin)."""
    ordered = _sorted_by_date(rows)
    if not ordered:
        return dict(_UNAVAILABLE)
    latest, first = ordered[-1], ordered[0]
    margin_now = _dec(latest.get("MarginPurchaseTodayBalance"))
    margin_first = _dec(first.get("MarginPurchaseTodayBalance"))
    short_now = _dec(latest.get("ShortSaleTodayBalance"))
    short_first = _dec(first.get("ShortSaleTodayBalance"))
    margin_chg = (
        chg_pct(margin_now, margin_first)
        if margin_now is not None and margin_first is not None else None
    )
    short_chg = (
        chg_pct(short_now, short_first)
        if short_now is not None and short_first is not None else None
    )
    return {
        "symbol": symbol,
        "last_as_of": as_of,
        "margin_balance": _s(margin_now),
        "short_balance": _s(short_now),
        "margin_balance_chg": _s(margin_chg),
        "short_balance_chg": _s(short_chg),
    }


def build_valuation(rows: list[dict[str, Any]], *, symbol: str, as_of: str) -> dict[str, Any]:
    """PER/PBR/yield + historical PER percentile (FinMind valuation/PER)."""
    ordered = _sorted_by_date(rows)
    if not ordered:
        return dict(_UNAVAILABLE)
    latest = ordered[-1]
    per = _dec(latest.get("PER"))
    per_history = [d for d in (_dec(r.get("PER")) for r in ordered) if d is not None]
    per_pct = percentile(per, per_history) if per is not None and per_history else None
    return {
        "symbol": symbol,
        "last_as_of": as_of,
        "per": _s(per),
        "pbr": _s(_dec(latest.get("PBR"))),
        "dividend_yield": _s(_dec(latest.get("dividend_yield"))),
        "per_percentile": _s(per_pct),
    }


def build_monthly_revenue(
    rows: list[dict[str, Any]], *, symbol: str, as_of: str
) -> dict[str, Any]:
    """Latest monthly revenue + YoY (vs same month a year ago) and MoM."""
    ordered = _sorted_by_date(rows)
    if not ordered:
        return dict(_UNAVAILABLE)
    latest = ordered[-1]
    revenue = _dec(latest.get("revenue"))
    prev_month = _dec(ordered[-2].get("revenue")) if len(ordered) >= 2 else None
    # Year-ago: same revenue_month, year-1.
    year_ago: Decimal | None = None
    month = latest.get("revenue_month")
    year = latest.get("revenue_year")
    if month is not None and year is not None:
        for row in ordered:
            if row.get("revenue_month") == month and row.get("revenue_year") == year - 1:
                year_ago = _dec(row.get("revenue"))
                break
    return {
        "symbol": symbol,
        "last_as_of": as_of,
        "latest_revenue": _s(revenue),
        "yoy": _s(yoy(revenue, year_ago) if revenue is not None and year_ago else None),
        "mom": _s(mom(revenue, prev_month) if revenue is not None and prev_month else None),
    }


def build_financials(
    rows: list[dict[str, Any]], *, symbol: str, as_of: str
) -> dict[str, Any]:
    """Pass-through of recent financial-statement line items (FinMind financials)."""
    if not rows:
        return dict(_UNAVAILABLE)
    return {"symbol": symbol, "last_as_of": as_of, "rows": rows}


def build_market_sentiment(
    *,
    vix_close: Decimal | None,
    as_of_vix: str | None,
    fng: dict[str, Any] | None,
    as_of_fng: str | None,
) -> dict[str, Any]:
    """VIX + its zone + Fear & Greed score/rating from the latest sentiment snapshots."""
    if vix_close is None and fng is None:
        return dict(_UNAVAILABLE)
    return {
        "vix": _s(vix_close),
        "vix_zone": vix_zone(vix_close) if vix_close is not None else None,
        "fear_greed": fng["score"] if fng else None,
        "fear_greed_rating": fng["rating"] if fng else None,
        "last_as_of": as_of_vix or as_of_fng,
    }


_INDEX_LABEL: dict[str, str] = {"^TWII": "TAIEX", "^GSPC": "SPX", "^KLSE": "KLCI"}


def build_index_quotes(
    quotes: dict[str, Decimal], *, as_of: str | None
) -> dict[str, Any]:
    """Latest benchmark index closes, labelled TAIEX/SPX/KLCI (from the index snapshot)."""
    if not quotes:
        return dict(_UNAVAILABLE)
    out: dict[str, Any] = {"last_as_of": as_of}
    for symbol, value in quotes.items():
        out[_INDEX_LABEL.get(symbol, symbol)] = str(value)
    return out


__all__ = [
    "build_financials",
    "build_index_quotes",
    "build_institutional",
    "build_margin",
    "build_market_sentiment",
    "build_monthly_revenue",
    "build_valuation",
    "chg_pct",
    "consecutive_buy_days",
    "consecutive_sell_days",
    "mom",
    "net_buy_sum",
    "percentile",
    "to_decimal",
    "vix_zone",
    "yoy",
]
