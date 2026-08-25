"""Prompt-foundation API (spec 06a): variable registry, system prompt, preview, test.

Thin router. The registry + render + token-validation live in ``llm_insight.variables``
(the single reusable core); this layer only orchestrates: build the REAL computed
``DashboardData``, fetch per-symbol price history, render, and serialize.

Two paths share one validation core (``validate_tokens``):

* ``POST /prompts/preview`` — diagnostic, ALWAYS 200, lists ``unknown_tokens`` /
  ``scope_violations``, never calls the LLM (zero cost), uses real computed values.
* ``POST /prompts/test`` — execution path: 422 on any unknown token or per_symbol var
  in a portfolio-scope body (= spec 04 R1); otherwise calls the real LLM, records
  ``llm_usage`` (agent=``prompt_test``), and honours the budget (exhausted -> 402 via
  the global handler).
"""

import math
import sqlite3
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from portfolio_dash.api import signals_service
from portfolio_dash.api.deps import get_conn, get_now, get_reporting
from portfolio_dash.api.errors import error_body
from portfolio_dash.data_ingestion.holdings import load_action_index
from portfolio_dash.data_ingestion.store import list_dividends
from portfolio_dash.llm_insight import official_templates
from portfolio_dash.llm_insight import variables as V
from portfolio_dash.llm_insight.evaluations_store import (
    calibration_bins,
    gap_quantized,
    gap_wire,
    rolling_calibration_gap,
    scored_confidence_hits,
)
from portfolio_dash.llm_insight.scoring import confidence_ceiling
from portfolio_dash.llm_insight.system_prompt import get_system_prompt, set_system_prompt
from portfolio_dash.news import organizer_prompt as news_organizer_prompt
from portfolio_dash.news import store as news_store
from portfolio_dash.portfolio import external_signals as ES
from portfolio_dash.portfolio.backtest import (
    FORWARD_WINDOWS,
    EventStudy,
    HistoryPoint,
    WindowStats,
    event_study,
)
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.portfolio.price_basis import series_in
from portfolio_dash.pricing import datasources_store, finmind_datasets, snapshots_store
from portfolio_dash.pricing.store import get_fx, get_price_history
from portfolio_dash.shared import llm
from portfolio_dash.shared.corporate_actions import ActionIndex
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.llm_config import budget_remaining
from portfolio_dash.shared.wire import decimal_str
from portfolio_dash.strategy import signal_history
from portfolio_dash.strategy.rules.composite import BAND_HIGH, BAND_LOW
from portfolio_dash.strategy.rules.params import PARAMS_VERSION

router = APIRouter()

_TAIPEI = ZoneInfo("Asia/Taipei")  # default clock for the news window when now is absent

# The recent window rendered into price_history_json / price_points (token-bounded).
_HISTORY_DAYS = 180
# Longer close series fed to the technical signals so 52-week position / MA120 are honest
# (52w ≈ 252 sessions ≈ 365 calendar days). SINGLE definition (L3 fix, 2026-07-07):
# the run path (api/insight_service) imports THIS constant, so preview and execution
# always see the same technical-signal window.
_TECHNICAL_HISTORY_DAYS = 400

# FinMind chips variable token -> (source id, FinMind logical dataset) (spec 20.15).
# The required tier is read live from ``finmind_datasets.DATASET_TIER[dataset]`` and the
# degrade reason from that source's ``data_source_health`` — both router concerns so
# ``llm_insight`` keeps importing neither pricing nor health.
_FINMIND_VAR_DATASET: dict[str, str] = {
    "institutional_json": "institutional",
    "margin_json": "margin",
    "valuation_json": "valuation",
    "monthly_revenue_json": "monthly_revenue",
    "financials_json": "financials",
}


def _required_tier_for(token: str) -> str | None:
    """The live required tier for a variable token (None when none is required).

    FinMind chips read ``DATASET_TIER`` so a future paid dataset re-gates automatically;
    sentiment/index and all non-external vars require no tier.
    """
    dataset = _FINMIND_VAR_DATASET.get(token)
    if dataset is None:
        return None
    return finmind_datasets.DATASET_TIER.get(dataset)


def _tier_label(required: str) -> str:
    """A short Traditional-Chinese label for the tier a variable needs (spec 20.15.3)."""
    names = {"backer": "Backer", "sponsor": "Sponsor", "sponsorpro": "Sponsor Pro"}
    return f"需要 {names.get(required, required)} 方案"


class SystemPromptIn(BaseModel):
    body: str


class PromptBody(BaseModel):
    body: str
    scope: str  # "portfolio" | "per_symbol" | "per_market"
    symbol: str | None = None
    market: str | None = None  # per_market preview target ("TW"/"US"/"MY")


# --- 6.1 variable registry ----------------------------------------------------


@router.get("/prompt-vars")
def prompt_vars(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict[str, Any]]:
    """The 34-variable registry (vars.js mirror + backend signal/news vars). ``available``
    drives the UI's
    "需後端新增" markers; chips/sentiment went live (spec 20.2), ai stays False (spec 04).

    Each var also carries tier metadata (spec 20.15.3): ``required_tier`` (from the live
    ``DATASET_TIER`` for FinMind chips, else null), ``tier_ok`` (computed vs the finmind
    source's marked tier; null requirement → true), and ``tier_label`` (only when not ok).
    """
    datasources_store.ensure_seeded(conn)
    finmind_tier = datasources_store.get_tier(conn, "finmind")
    rows: list[dict[str, Any]] = []
    for v in V.REGISTRY:
        required = _required_tier_for(v.token)
        ok = V.tier_ok(required, finmind_tier)
        rows.append({
            "token": v.token,
            "name": v.name,
            "category": v.category,
            "scope": v.scope,
            "desc": v.desc,
            "available": v.available,
            "sample": v.sample,
            "required_tier": required,
            "tier_ok": ok,
            "tier_label": _tier_label(required) if (required and not ok) else None,
        })
    return rows


# --- 6.2 global system prompt -------------------------------------------------


@router.get("/system-prompt")
def read_system_prompt(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, str]:
    return get_system_prompt(conn)


@router.put("/system-prompt")
def write_system_prompt(
    payload: SystemPromptIn,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> dict[str, str]:
    return set_system_prompt(conn, payload.body, now=now)


# --- official template library (AI-input optimization program, 2026-07-05) -----


@router.get("/prompt-templates")
def read_prompt_templates() -> dict[str, Any]:
    """The official template library: versioned system prompt + strategy templates.

    Pure constants — the UI's「重置回官方版」/「從官方模板庫新增」read from here so the
    shipped optimum stays one click away regardless of user customization.
    """
    return official_templates.library_wire()


@router.post("/system-prompt/reset")
def reset_system_prompt(
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> dict[str, str]:
    """Restore the global system prompt to the official library version."""
    return set_system_prompt(conn, official_templates.SYSTEM_PROMPT_BODY, now=now)


# --- news-organizer prompt (batch ④; user-viewable + editable, with reset) -----


@router.get("/news-prompt")
def read_news_prompt(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, str]:
    """The editable news-organizer system prompt (default-seeded from the official library)."""
    return news_organizer_prompt.get_news_prompt(conn)


@router.put("/news-prompt")
def write_news_prompt(
    payload: SystemPromptIn,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> dict[str, str]:
    """Overwrite the news-organizer prompt (applies to the next nightly news run)."""
    return news_organizer_prompt.set_news_prompt(conn, payload.body, now=now)


@router.post("/news-prompt/reset")
def reset_news_prompt(
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> dict[str, str]:
    """Restore the news-organizer prompt to the official library version."""
    return news_organizer_prompt.reset_news_prompt(conn, now=now)


# --- shared assembly ----------------------------------------------------------


def _resolve_fx_rates(
    conn: sqlite3.Connection, data: Any, now: datetime, reporting: Currency
) -> dict[str, dict[str, Any]]:
    """Latest spot rate for each distinct holding currency -> reporting currency.

    Reads stored rates (``get_fx``; direct pair, else inverted) — no number is computed
    of record beyond the trivial inversion the dashboard's RateResolver also performs.
    """
    out: dict[str, dict[str, Any]] = {}
    seen: set[Currency] = set()
    for h in data.holdings:
        ccy = h.quote_ccy
        if ccy == reporting or ccy in seen:
            continue
        seen.add(ccy)
        read = get_fx(conn, ccy, reporting, now=now)
        if read is not None:
            rate, as_of, stale = read.rate, read.as_of, read.stale
        else:
            inv = get_fx(conn, reporting, ccy, now=now)
            if inv is None:
                continue
            rate, as_of, stale = Decimal("1") / inv.rate, inv.as_of, inv.stale
        out[f"{ccy.value}_{reporting.value}"] = {
            "rate": rate, "as_of": as_of.isoformat(), "stale": stale,
        }
    return out


def _dividend_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Per-event dividend ledger rows with the instrument's quote currency (LLM-facing,
    type lowercased to match the wire convention)."""
    ccy_by_symbol = {
        r["symbol"]: r["quote_ccy"]
        for r in conn.execute("SELECT symbol, quote_ccy FROM instruments")
    }
    rows: list[dict[str, Any]] = []
    for d in list_dividends(conn):
        rows.append({
            "symbol": d.symbol, "date": d.date.isoformat(), "type": d.type.lower(),
            "gross": d.gross, "withholding": d.withholding, "net": d.net,
            "reinvest_shares": d.reinvest_shares, "ccy": ccy_by_symbol.get(d.symbol),
        })
    return rows


def _finmind_var(
    conn: sqlite3.Connection,
    symbol: str,
    *,
    dataset: str,
    build: Any,
) -> dict[str, Any]:
    """Read the latest FinMind snapshot for ``dataset``/``symbol`` and assemble its var.

    Each FinMind snapshot payload holds the full multi-day window (``{"rows": [...]}``),
    so the latest snapshot is enough; the pure assembler in ``portfolio.external_signals``
    derives the value. Absent snapshot -> the assembler's unavailable shape.
    """
    snap = snapshots_store.latest_snapshot(
        conn, source="finmind", dataset=dataset, symbol=symbol
    )
    rows = snap.payload.get("rows", []) if snap is not None else []
    as_of = snap.as_of.isoformat() if snap is not None else ""
    result: dict[str, Any] = build(rows, symbol=symbol, as_of=as_of)
    return result


_FNG_TREND_DAYS = 7


def _fng_scores_newest_first(conn: sqlite3.Connection) -> list[Decimal]:
    """The last ≤7 daily Fear & Greed scores, newest first (for the local trend)."""
    series = snapshots_store.latest_series(
        conn, source="sentiment", dataset="fng", symbol=None, n=_FNG_TREND_DAYS
    )
    scores: list[Decimal] = []
    for snap in series:  # latest_series is already newest-first
        score = ES.to_decimal(snap.payload.get("score"))
        if score is not None:
            scores.append(score)
    return scores


def _sentiment_var(conn: sqlite3.Connection) -> dict[str, Any]:
    """Assemble market_sentiment_json from the latest VIX + Fear & Greed snapshots
    (VIX zone + F&G local five-zone + 7-day F&G trend — batch ③)."""
    vix_snap = snapshots_store.latest_snapshot(
        conn, source="sentiment", dataset="vix", symbol=None
    )
    fng_snap = snapshots_store.latest_snapshot(
        conn, source="sentiment", dataset="fng", symbol=None
    )
    vix_close = (
        ES.to_decimal(vix_snap.payload.get("close")) if vix_snap is not None else None
    )
    fng = fng_snap.payload if fng_snap is not None else None
    return ES.build_market_sentiment(
        vix_close=vix_close,
        as_of_vix=vix_snap.as_of.isoformat() if vix_snap is not None else None,
        fng=fng,
        as_of_fng=fng_snap.as_of.isoformat() if fng_snap is not None else None,
        fng_scores_newest_first=_fng_scores_newest_first(conn),
    )


def _fear_greed_var(conn: sqlite3.Connection) -> dict[str, Any]:
    """Assemble the standalone fear_greed_json (score + local zone + 7-day trend).

    A dedicated variable so custom prompts can reference F&G independently of VIX
    (2026-07-05 user request); derived from the same snapshots as market_sentiment_json.
    """
    fng_snap = snapshots_store.latest_snapshot(
        conn, source="sentiment", dataset="fng", symbol=None
    )
    if fng_snap is None:
        return {"unavailable": True, "last_as_of": None}
    score = ES.to_decimal(fng_snap.payload.get("score"))
    scores = _fng_scores_newest_first(conn)
    return {
        "score": fng_snap.payload.get("score"),
        "zone": ES.fear_greed_zone(score) if score is not None else None,
        "trend": ES.fear_greed_trend(scores) if scores else None,
        "last_as_of": fng_snap.as_of.isoformat(),
    }


_NEWS_WINDOW_DAYS = 7
_NEWS_MAX_ITEMS = 10


def _news_var(symbol: str, *, now: datetime) -> dict[str, Any]:
    """Assemble symbol_news_json from the separate news DB (batch ④).

    Reads AI-organized news mentioning *symbol* within the last ``_NEWS_WINDOW_DAYS``
    days from ``news.db``. Degrades to an honest empty payload when the news DB is absent
    or has nothing (the nightly pipeline may not have run). Only the summary + metadata are
    exposed — never full article bodies.
    """
    since = (now.date() - timedelta(days=_NEWS_WINDOW_DAYS)).isoformat()
    try:
        with news_store.news_session() as nconn:
            rows = news_store.query_by_symbol(
                nconn, symbol, since_date=since, limit=_NEWS_MAX_ITEMS
            )
    except Exception:  # noqa: BLE001 — a news-DB hiccup must never break card rendering
        rows = []
    if not rows:
        return {"symbol": symbol, "since": since, "items": [], "count": 0}
    items = [
        {
            "date": r.news_date, "title": r.title, "summary": r.body_summary,
            "source": r.source, "lang": r.lang, "link": r.link,
        }
        for r in rows
    ]
    return {"symbol": symbol, "since": since, "items": items, "count": len(items)}


def _index_var(conn: sqlite3.Connection) -> dict[str, Any]:
    """Assemble index_quotes_json from the latest index snapshot."""
    snap = snapshots_store.latest_snapshot(
        conn, source="index", dataset="index_quotes", symbol=None
    )
    if snap is None:
        return ES.build_index_quotes({}, as_of=None)
    raw = snap.payload.get("quotes", {})
    quotes = {sym: d for sym, v in raw.items() if (d := ES.to_decimal(v)) is not None}
    return ES.build_index_quotes(quotes, as_of=snap.as_of.isoformat())


# Honest degrade reason for a symbol yfinance carries no analyst data for (P1 batch 2).
_CONSENSUS_NO_COVERAGE = "無分析師覆蓋（yfinance 無此標的的分析師目標價／評級資料）"

# Honest degrade reason when NO fundamentals source has a block for the symbol (W3).
_FUNDAMENTALS_NO_COVERAGE = (
    "無基本面快照（yfinance 無覆蓋，Finnhub／Alpha Vantage 未啟用或無覆蓋）"
)

#: The three union sources whose blocks are read straight from their snapshots (W3,
#: AI-D14 — one block per source, no merge). TW symbols additionally get a ``finmind``
#: block mapped from the existing valuation snapshot (AI-D15, no double fetching).
_FUNDAMENTALS_SOURCES = ("yfinance", "finnhub", "alphavantage")


def _fundamentals_var(conn: sqlite3.Connection, symbol: str) -> dict[str, Any]:
    """Assemble fundamentals_json: one block per source under ``sources`` (W3, AI-D14).

    Each block is the provider's snapshot payload verbatim (canonical field names +
    currency + as_of, already final at the fetch seam — this layer computes nothing).
    TW symbols also get a ``finmind`` block mapped from the existing FinMind valuation
    snapshot (per→pe_ratio, pbr→pb_ratio, dividend_yield→dividend_yield_pct) so all
    three markets are same-shaped and comparable; per_percentile stays FinMind-specific
    and remains only in valuation_json. No blocks at all → the unavailable shape, with
    the reason fed via ``_external_reasons``.
    """
    blocks: dict[str, Any] = {}
    for source in _FUNDAMENTALS_SOURCES:
        snap = snapshots_store.latest_snapshot(
            conn, source=source, dataset="fundamentals", symbol=symbol
        )
        if snap is not None:
            blocks[source] = snap.payload
    row = conn.execute(
        "SELECT market FROM instruments WHERE symbol = ?", (symbol,)
    ).fetchone()
    if row is not None and row["market"] == "TW":
        finmind = _finmind_var(conn, symbol, dataset="valuation", build=ES.build_valuation)
        if finmind.get("unavailable") is not True:
            block: dict[str, Any] = {
                "as_of": finmind.get("last_as_of"),
                "currency": "TWD",
            }
            for src_key, canon in (
                ("per", "pe_ratio"), ("pbr", "pb_ratio"),
                ("dividend_yield", "dividend_yield_pct"),
            ):
                if finmind.get(src_key) is not None:
                    block[canon] = finmind[src_key]
            if len(block) > 2:
                blocks["finmind"] = block
    if not blocks:
        return {"unavailable": True, "last_as_of": None}
    as_ofs = [b.get("as_of") for b in blocks.values() if b.get("as_of")]
    return {
        "symbol": symbol,
        "last_as_of": max(as_ofs) if as_ofs else None,
        "sources": blocks,
    }


def _consensus_var(conn: sqlite3.Connection, symbol: str) -> dict[str, Any]:
    """Assemble consensus_json from the latest yfinance consensus snapshot (P1 batch 2).

    The snapshot payload is already the final LLM-facing shape (target prices as Decimal
    strings, rating distribution + local rating score + upside, all computed at the
    ``pricing.consensus_source`` fetch seam — llm_insight computes nothing). An absent
    snapshot degrades to the unavailable shape; the router feeds the "no coverage" reason
    via ``_external_reasons``.
    """
    snap = snapshots_store.latest_snapshot(
        conn, source="yfinance", dataset="consensus", symbol=symbol
    )
    if snap is None:
        return {"unavailable": True, "last_as_of": None}
    return dict(snap.payload)


# Honest degrade reason when a symbol's price history is too thin for the rule engine.
_RULE_SIGNALS_THIN = "價格歷史不足，法則引擎無法評估"


def _rule_signals_var(
    conn: sqlite3.Connection, symbol: str, *, now: datetime
) -> dict[str, Any]:
    """Assemble rule_signals_json via the SAME path as GET /api/signals/{symbol} (P2 batch
    3): identical evaluation + display quantization = one source of truth, one wire shape.
    Computed on read (no snapshot table, like technicals). A too-thin / absent series
    degrades to the unavailable shape (every rule None AND no composite → cannot judge),
    which the var renders as ``{"unavailable": true, "reason": …}`` via _external_reasons.
    """
    signals, price_as_of = signals_service.evaluate_symbol(conn, symbol, now=now)
    wire = signals_service.to_wire(
        symbol, signals, now=now, held=signals_service.is_held(conn, symbol),
        price_as_of=price_as_of,
    )
    rules = wire.get("rules")
    rules_all_none = isinstance(rules, dict) and all(v is None for v in rules.values())
    if wire.get("composite") is None and rules_all_none:
        return {"unavailable": True, "last_as_of": wire.get("as_of")}
    return wire


# --- W6 variables (AI-D31): the two AI-self calibration stubs lit as DECLARED, plus the
# event study on its own per-symbol token. All numbers computed locally; the LLM narrates.

_Q3 = Decimal("0.001")
_Q4 = Decimal("0.0001")


def _ratio(value: Decimal, exp: Decimal) -> str:
    return decimal_str(value.quantize(exp, rounding=ROUND_HALF_UP))


def _backtest_var(conn: sqlite3.Connection) -> dict[str, Any]:
    """``backtest_json``: the GLOBAL calibration bins + overall hit rate, from scored
    evaluations (the declared spec-04 meaning — confidence anchoring for prompts).

    Degrades to the unavailable shape when nothing has been scored yet — an empty bins
    list would anchor confidence on a population of zero without saying so.
    """
    pairs = scored_confidence_hits(conn)
    if not pairs:
        return {"unavailable": True, "last_as_of": None}
    hits = sum(1 for _, hit in pairs if hit)
    bins = calibration_bins(conn)  # already the wire shape (Decimal strings)
    # W7.1 — the anchoring law's ARITHMETIC, done here. The first live run had 0 of 13 cards
    # obey a rule that asked the model to walk a bins table mid-generation; the rule is
    # unchanged and still lives in the prompt, the model is just handed one integer now.
    # Nothing clamps the model's answer afterwards (AI-D33 stands).
    #
    # AI-D39 (2026-08-25): the rolling gap NO LONGER participates — a bucket's cap is already
    # the calibration correction, so deducting the gap charged the same over-confidence twice
    # (demo: bins 39, gap −47, answer 0). `rolling_calibration_gap` is still read by
    # `_calibration_gap_var`, which is where that number belongs: as a REPORTED diagnostic,
    # not as a second penalty.
    overall = Decimal(hits) / len(pairs)
    return {
        "bins": bins,
        "overall_hit_rate": _ratio(overall, _Q3),
        # ⚠ The helper takes a PERCENTAGE, the wire field carries a FRACTION. Converted here,
        # visibly and once — mixing the two silently is the exact defect W7.1 shipped.
        "confidence_ceiling": confidence_ceiling(bins, overall_hit_pct=overall * 100),
        "confidence_ceiling_note": (
            "本系統依你的戰績算好的信心上限（0-100 整數）：所屬區間實際命中率＋5；"
            "該區間樣本不足或從未計分過時，改用你的整體命中率＋5（而非固定值），"
            "所以沒有戰績的區間不會比量測過的區間更寬鬆。"
        ),
    }


def _calibration_gap_var(conn: sqlite3.Connection) -> dict[str, Any]:
    """``calibration_gap_json``: the ROLLING signed calibration gap — ``actual − claimed``
    (positive = the model is UNDER-confident). The computation lives in
    ``evaluations_store.rolling_calibration_gap`` (W7, AI-D36: ONE definition shared with
    the ai-score API); below the gate it degrades honestly.
    """
    result = rolling_calibration_gap(conn)
    if result.gap is None:
        return {"unavailable": True, "last_as_of": None}
    # W7.1 — a PRE-WORDED reading rides with the number. The weekly card of 2026-08-23 read
    # gap −0.466 as 「低估自身表現」 (the opposite) even though the template states the sign
    # convention in the same section: a signed fraction is one negation away from asserting
    # the reverse of the truth, so the direction ships as text the model can copy.
    # Derived from the QUANTIZED gap, so the words and the number cannot disagree.
    pp = _ratio(abs(gap_quantized(result.gap)) * 100, Decimal("0.1"))
    return {
        "gap": gap_wire(result.gap),
        "window_n": result.window_n,
        "reading": (
            f"最近 {result.window_n} 筆平均高估自己 {pp} 個百分點"
            if result.gap < 0
            else f"最近 {result.window_n} 筆平均低估自己 {pp} 個百分點"
        ),
    }


def _window_stats_wire(stats: WindowStats) -> dict[str, Any]:
    """The study's full-precision Decimals are quantized HERE (display boundary — the
    module stays exact), 4 dp like the ratio-like evidence keys."""
    return {
        "n": stats.n,
        "mean": _ratio(stats.mean, _Q4),
        "median": _ratio(stats.median, _Q4),
        "pct_positive": _ratio(stats.pct_positive, _Q4),
    }


def _signal_backtest_wire(
    symbol: str, rows: list[signal_history.SignalHistoryRow], study: EventStudy
) -> dict[str, Any]:
    groups: list[dict[str, Any]] = []
    for group in study.groups:
        per_window: dict[str, Any] = {}
        for outcome in group.outcomes:
            cell: dict[str, Any] = (
                _window_stats_wire(outcome.stats)
                if outcome.stats is not None
                # 不足以判斷 — the count is shown, the numbers are withheld (AI-D30).
                else {"n": outcome.n, "insufficient": True}
            )
            cell["n_overlapping"] = outcome.n_overlapping
            cell["n_censored"] = outcome.n_censored
            per_window[str(outcome.window)] = cell
        groups.append({
            "kind": group.kind,
            "direction": group.direction,
            "events": group.events,
            "per_window": per_window,
        })
    baseline = {
        str(w): (_window_stats_wire(s) if s is not None else None)
        for w, s in zip(FORWARD_WINDOWS, study.baselines, strict=True)
    }
    return {
        "symbol": symbol,
        # W7.1 — the unit rides WITH the numbers. Every mean/median/pct_positive is a
        # FRACTION, and that fact previously existed only in the variable registry's `desc`
        # (UI documentation the model never sees): the first live run printed 0.1336 as
        # 「+0.1336%」 — the true value 100× smaller — alongside a bare fraction and a USD
        # amount, three renderings of one quantity in one batch of cards.
        "units": {
            "mean": "fraction — 0.1336 means +13.36%",
            "median": "fraction — same scale as mean",
            "pct_positive": "fraction — 0.8182 means 81.82% of events",
        },
        "history": {
            "first": rows[0].as_of.isoformat(),
            "last": rows[-1].as_of.isoformat(),
            "rows": len(rows),
            "params_version": PARAMS_VERSION,
        },
        "windows": list(FORWARD_WINDOWS),
        "groups": groups,
        "baseline": baseline,
        "events_without_price": study.events_without_price,
    }


def _signal_backtest_var(
    conn: sqlite3.Connection, symbol: str, *, actions: ActionIndex, now: datetime
) -> dict[str, Any]:
    """``signal_backtest_json``: the per-symbol event study over ``signal_history``.

    The close series spans the FULL history (first stored signal date → today), NOT the
    583-day evaluation window — forward returns must survive every event, and the
    +120-session tail of an old event predates any recent window. Re-expressed into
    today's share terms via ``series_in`` (valued_on=end) so a return across a split is
    honest (AI-D30/W6c); local currency (AI-D23). Only the CURRENT params vintage is
    studied. No history rows → the unavailable shape (the full-coverage floor means young
    symbols honestly have none). ``actions`` is the request's ONE action index, built by
    the caller (trap #21 — this producer runs once per symbol per generation run).
    """
    rows = signal_history.list_rows(conn, symbol, params_version=PARAMS_VERSION)
    if not rows:
        return {"unavailable": True, "last_as_of": None}
    end = now.date()
    history = series_in(
        actions, symbol,
        get_price_history(conn, symbol, rows[0].as_of, end),
        valued_on=end,
    )
    closes = [(p.as_of, p.value) for p in history]
    points = [
        HistoryPoint(
            as_of=r.as_of,
            scores={
                "trend_filter": r.trend_score,
                "ma_cross": r.cross_score,
                "momentum_12_1": r.momentum_score,
                "rsi_regime": r.rsi_score,
            },
            tech_score=r.tech_score,
        )
        for r in rows
    ]
    study = event_study(points, closes, band_high=BAND_HIGH, band_low=BAND_LOW)
    return _signal_backtest_wire(symbol, rows, study)


def _external_vars(
    conn: sqlite3.Connection,
    symbol: str | None,
    *,
    now: datetime | None = None,
    actions: ActionIndex | None,
) -> dict[str, Any]:
    """Assemble the chips/sentiment/index/news variable values from external snapshots.

    Conn-bearing reads + pure derivation (``portfolio.external_signals``) happen HERE,
    not in ``llm_insight`` (layering, spec 20.3). Portfolio-scope sentiment/index are
    always assembled; the per-symbol chips need a symbol. Missing snapshots degrade to
    the assembler's ``{"unavailable": ...}`` shape, which the var renders as such.

    ``actions`` is a REQUIRED keyword (the injection rule: forgetting it is a TypeError,
    not a silent loss) — ``None`` is the explicit portfolio-scope value; a per-symbol
    assembly always carries the request's ONE action index (trap #21).
    """
    out: dict[str, Any] = {
        "market_sentiment_json": _sentiment_var(conn),
        "fear_greed_json": _fear_greed_var(conn),
        "index_quotes_json": _index_var(conn),
        # W6 (AI-D31): the two AI-self calibration vars — portfolio scope, always on.
        "backtest_json": _backtest_var(conn),
        "calibration_gap_json": _calibration_gap_var(conn),
    }
    if symbol:
        out["institutional_json"] = _finmind_var(
            conn, symbol, dataset="institutional", build=ES.build_institutional
        )
        out["margin_json"] = _finmind_var(
            conn, symbol, dataset="margin", build=ES.build_margin
        )
        out["valuation_json"] = _finmind_var(
            conn, symbol, dataset="valuation", build=ES.build_valuation
        )
        out["monthly_revenue_json"] = _finmind_var(
            conn, symbol, dataset="monthly_revenue", build=ES.build_monthly_revenue
        )
        out["financials_json"] = _finmind_var(
            conn, symbol, dataset="financials", build=ES.build_financials
        )
        out["consensus_json"] = _consensus_var(conn, symbol)
        out["fundamentals_json"] = _fundamentals_var(conn, symbol)
        out["symbol_news_json"] = _news_var(symbol, now=now or datetime.now(_TAIPEI))
        out["rule_signals_json"] = _rule_signals_var(
            conn, symbol, now=now or datetime.now(_TAIPEI)
        )
        # Programmer-error guard: a per-symbol assembly always carries the request's
        # action index (mypy cannot narrow the symbol/actions correlation for us).
        assert actions is not None
        out["signal_backtest_json"] = _signal_backtest_var(
            conn, symbol, actions=actions, now=now or datetime.now(_TAIPEI)
        )
    return out


def _external_reasons(conn: sqlite3.Connection, external_vars: dict[str, Any]) -> dict[str, str]:
    """Degrade reasons per external var (fed via ``VarContext.external_reasons``).

    Two honest-degrade sources, both resolved by the router (``llm_insight`` never reads
    health/snapshots itself):

    * FinMind chips — when a chips var is ``unavailable`` AND the finmind source's health
      is ``error`` (spec 20.15.4), its detail is surfaced (e.g. "需要 Backer 方案" /
      "額度已滿").
    * Consensus — when ``consensus_json`` is ``unavailable`` (no snapshot), a static
      "no analyst coverage" reason (yfinance carries no analyst data for the symbol).
    * Rule signals — when ``rule_signals_json`` is ``unavailable`` (too-thin price
      history), a static "price history insufficient" reason.
    """
    reasons: dict[str, str] = {}
    state = datasources_store.get_state(conn, "finmind")
    if state is not None and state.status == "error" and state.detail:
        for token in _FINMIND_VAR_DATASET:
            value = external_vars.get(token)
            if not isinstance(value, dict) or value.get("unavailable") is True:
                reasons[token] = state.detail
    consensus = external_vars.get("consensus_json")
    if isinstance(consensus, dict) and consensus.get("unavailable") is True:
        reasons["consensus_json"] = _CONSENSUS_NO_COVERAGE
    fundamentals = external_vars.get("fundamentals_json")
    if isinstance(fundamentals, dict) and fundamentals.get("unavailable") is True:
        reasons["fundamentals_json"] = _FUNDAMENTALS_NO_COVERAGE
    rule_signals = external_vars.get("rule_signals_json")
    if isinstance(rule_signals, dict) and rule_signals.get("unavailable") is True:
        reasons["rule_signals_json"] = _RULE_SIGNALS_THIN
    return reasons


def _build_context(
    conn: sqlite3.Connection, payload: PromptBody, now: datetime, reporting: Currency
) -> V.VarContext:
    """Build the render context with the REAL computed dashboard (+ per-symbol history).

    Conn-bearing reads (FX spot rates, dividend ledger rows, external snapshots, health
    reasons) are resolved HERE and fed into the context — ``llm_insight`` must not import
    ``pricing``/``data_ingestion``.
    """
    data = build_dashboard(conn, now=now, reporting=reporting)
    symbol = payload.symbol if payload.scope == "per_symbol" else None
    # ONE action index per request (trap #21): shared by the external vars
    # (signal_backtest's re-expression) and the long-history read below. Portfolio
    # previews deliberately pay zero ledger reads.
    actions = load_action_index(conn) if symbol else None
    external_vars = _external_vars(conn, symbol, now=now, actions=actions)
    ctx = V.VarContext(
        data=data,
        now=now,  # spec 04.10 {{now}} renders ISO-8601 +08:00 in preview/test
        fx_rates=_resolve_fx_rates(conn, data, now, reporting),
        dividend_rows=_dividend_rows(conn),
        external_vars=external_vars,
        external_reasons=_external_reasons(conn, external_vars),
        # ⑬ — derived from the payloads just built; no extra read.
        external_as_of=V.external_as_of_map(external_vars),
    )
    if payload.scope == "per_market" and payload.market:
        ctx.market = payload.market  # market-sliced preview (2026-07-05 spec)
    if payload.scope == "per_symbol" and payload.symbol:
        as_of = now.date()
        # L3 fix: same split as the run path (insight_service._per_symbol_ctx) —
        # ctx.closes gets the LONG series (honest 52w/MA120 technical signals);
        # price_points keeps only the recent window (token-bounded).
        # §5.1(d) / W6c: re-expressed into `as_of` exactly as the run path does, so the
        # preview a prompt is authored against is the series the run will render. The ONE
        # request index built above (trap #21). ⚠ volume keeps the provider's basis
        # (D39b).
        assert actions is not None  # per-symbol preview always carries it (built above)
        long_hist = series_in(
            actions, payload.symbol,
            get_price_history(
                conn, payload.symbol,
                as_of - timedelta(days=_TECHNICAL_HISTORY_DAYS), as_of,
            ),
            valued_on=as_of,
        )
        ctx.symbol = payload.symbol
        ctx.closes = [p.value for p in long_hist]
        # Volumes aligned 1:1 with closes (probe-gated) so preview + generation match.
        vols = [p.volume for p in long_hist]
        ctx.volumes = vols if any(v is not None for v in vols) else None
        recent = [p for p in long_hist if p.as_of >= as_of - timedelta(days=_HISTORY_DAYS)]
        ctx.price_points = [
            {"date": p.as_of.isoformat(), "close": decimal_str(p.value)} for p in recent
        ]
    return ctx


def _est_tokens(system_prompt: str, rendered: str) -> int:
    """Heuristic token estimate (no tokenizer dep): ~4 chars per token, ceil."""
    return math.ceil(len(system_prompt + "\n" + rendered) / 4)


# --- 6.2 preview (always 200, no LLM) -----------------------------------------


@router.post("/prompts/preview")
def preview(
    payload: PromptBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
    reporting: Currency = Depends(get_reporting),
) -> dict[str, Any]:
    validation = V.validate_tokens(payload.body, payload.scope)
    ctx = _build_context(conn, payload, now, reporting)
    rendered, tokens_used = V.render_prompt(payload.body, ctx)
    system_prompt = get_system_prompt(conn)["body"]
    return {
        "system_prompt": system_prompt,
        "rendered": rendered,
        "tokens_used": tokens_used,
        "unknown_tokens": validation.unknown_tokens,
        "scope_violations": validation.scope_violations,
        "est_tokens": _est_tokens(system_prompt, rendered),
    }


# --- 6.2 test (real LLM; 422 on bad tokens; budget -> 402 via global handler) --


@router.post("/prompts/test")
def test_prompt(
    payload: PromptBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
    reporting: Currency = Depends(get_reporting),
) -> Any:
    """Execution path: render + send to the real LLM, record usage, honour the budget.

    Unlike preview, this REJECTS bad tokens with 422 (unknown token OR a per_symbol var
    used in a portfolio-scope body = spec 04 R1). The budget gate / role activation /
    provider failure surface as 402 / 409 / 503 via the global exception handlers.
    Records ``llm_usage`` with agent ``prompt_test``; does NOT write an insight card.
    """
    validation = V.validate_tokens(payload.body, payload.scope)
    if validation.unknown_tokens or validation.scope_violations:
        issues = [
            {"code": "unknown_token", "token": t} for t in validation.unknown_tokens
        ] + [
            {"code": "scope_violation", "token": t} for t in validation.scope_violations
        ]
        return JSONResponse(
            status_code=422,
            content=error_body(
                "validation_error", "提示詞中含有無效的變數符號", issues=issues
            ),
        )
    ctx = _build_context(conn, payload, now, reporting)
    rendered, _ = V.render_prompt(payload.body, ctx)
    system_prompt = get_system_prompt(conn)["body"]
    result = llm.complete_text(rendered, agent="prompt_test", conn=conn, system=system_prompt)
    return {
        "reply": result.reply,
        "model": result.model,
        "via": "litellm",
        "tokens_in": result.tokens_in,
        "tokens_out": result.tokens_out,
        "cost_usd": decimal_str(result.cost),
        "quota_remaining": decimal_str(budget_remaining(conn)),
    }
