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
from portfolio_dash.llm_insight import variables as V
from portfolio_dash.llm_insight.system_prompt import get_system_prompt, set_system_prompt
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.pricing.store import get_fx, get_price_history
from portfolio_dash.shared import llm
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.llm_config import budget_remaining

router = APIRouter()

_HISTORY_DAYS = 180


class SystemPromptIn(BaseModel):
    body: str


class PromptBody(BaseModel):
    body: str
    scope: str  # "portfolio" | "per_symbol"
    symbol: str | None = None


# --- 6.1 variable registry ----------------------------------------------------


@router.get("/prompt-vars")
def prompt_vars() -> list[dict[str, Any]]:
    """The 26-variable registry (mirrors web/vars.js). ``available`` drives the UI's
    "需後端新增" markers; chips/sentiment/ai are False until specs 06b / 04."""
    return [
        {
            "token": v.token,
            "name": v.name,
            "category": v.category,
            "scope": v.scope,
            "desc": v.desc,
            "available": v.available,
            "sample": v.sample,
        }
        for v in V.REGISTRY
    ]


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


def _build_context(
    conn: sqlite3.Connection, payload: PromptBody, now: datetime, reporting: Currency
) -> V.VarContext:
    """Build the render context with the REAL computed dashboard (+ per-symbol history).

    Conn-bearing reads (FX spot rates, dividend ledger rows) are resolved HERE and fed
    into the context — ``llm_insight`` must not import ``pricing``/``data_ingestion``.
    """
    data = build_dashboard(conn, now=now, reporting=reporting)
    ctx = V.VarContext(
        data=data,
        fx_rates=_resolve_fx_rates(conn, data, now, reporting),
        dividend_rows=_dividend_rows(conn),
    )
    if payload.scope == "per_symbol" and payload.symbol:
        as_of = now.date()
        start = as_of - timedelta(days=_HISTORY_DAYS)
        history = get_price_history(conn, payload.symbol, start, as_of)
        ctx.symbol = payload.symbol
        ctx.closes = [p.value for p in history]
        ctx.price_points = [
            {"date": p.as_of.isoformat(), "close": str(p.value)} for p in history
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
        "cost_usd": str(result.cost),
        "quota_remaining": str(budget_remaining(conn)),
    }
