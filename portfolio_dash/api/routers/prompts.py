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
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from portfolio_dash.api.deps import get_conn, get_now, get_reporting
from portfolio_dash.api.errors import error_body
from portfolio_dash.data_ingestion.store import list_dividends
from portfolio_dash.llm_insight import official_templates
from portfolio_dash.llm_insight import variables as V
from portfolio_dash.llm_insight.system_prompt import get_system_prompt, set_system_prompt
from portfolio_dash.portfolio import external_signals as ES
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.pricing import datasources_store, finmind_datasets, snapshots_store
from portfolio_dash.pricing.store import get_fx, get_price_history
from portfolio_dash.shared import llm
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.llm_config import budget_remaining
from portfolio_dash.shared.wire import decimal_str

router = APIRouter()

_HISTORY_DAYS = 180

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
    scope: str  # "portfolio" | "per_symbol"
    symbol: str | None = None


# --- 6.1 variable registry ----------------------------------------------------


@router.get("/prompt-vars")
def prompt_vars(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict[str, Any]]:
    """The 29-variable registry (mirrors web/vars.js). ``available`` drives the UI's
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


def _sentiment_var(conn: sqlite3.Connection) -> dict[str, Any]:
    """Assemble market_sentiment_json from the latest VIX + Fear & Greed snapshots."""
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
    )


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


def _external_vars(conn: sqlite3.Connection, symbol: str | None) -> dict[str, Any]:
    """Assemble the chips/sentiment/index variable values from external snapshots.

    Conn-bearing reads + pure derivation (``portfolio.external_signals``) happen HERE,
    not in ``llm_insight`` (layering, spec 20.3). Portfolio-scope sentiment/index are
    always assembled; the per-symbol chips need a symbol. Missing snapshots degrade to
    the assembler's ``{"unavailable": ...}`` shape, which the var renders as such.
    """
    out: dict[str, Any] = {
        "market_sentiment_json": _sentiment_var(conn),
        "index_quotes_json": _index_var(conn),
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
    return out


def _external_reasons(conn: sqlite3.Connection, external_vars: dict[str, Any]) -> dict[str, str]:
    """Degrade reasons per external var, fed from ``data_source_health`` (spec 20.15.4).

    For each FinMind chips var that came back ``unavailable`` (no usable snapshot), if
    the finmind source's health is ``error`` its detail is surfaced as the reason (e.g.
    "需要 Backer 方案" / "額度已滿"). ``llm_insight`` never reads health — the router does
    and feeds the reason in via ``VarContext.external_reasons``.
    """
    state = datasources_store.get_state(conn, "finmind")
    if state is None or state.status != "error" or not state.detail:
        return {}
    reasons: dict[str, str] = {}
    for token in _FINMIND_VAR_DATASET:
        value = external_vars.get(token)
        if not isinstance(value, dict) or value.get("unavailable") is True:
            reasons[token] = state.detail
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
    external_vars = _external_vars(conn, symbol)
    ctx = V.VarContext(
        data=data,
        now=now,  # spec 04.10 {{now}} renders ISO-8601 +08:00 in preview/test
        fx_rates=_resolve_fx_rates(conn, data, now, reporting),
        dividend_rows=_dividend_rows(conn),
        external_vars=external_vars,
        external_reasons=_external_reasons(conn, external_vars),
    )
    if payload.scope == "per_symbol" and payload.symbol:
        as_of = now.date()
        start = as_of - timedelta(days=_HISTORY_DAYS)
        history = get_price_history(conn, payload.symbol, start, as_of)
        ctx.symbol = payload.symbol
        ctx.closes = [p.value for p in history]
        ctx.price_points = [
            {"date": p.as_of.isoformat(), "close": decimal_str(p.value)} for p in history
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
                "validation_error", "prompt references invalid tokens", issues=issues
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
