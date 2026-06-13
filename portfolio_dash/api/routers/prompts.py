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
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from portfolio_dash.api.deps import get_conn, get_now, get_reporting
from portfolio_dash.llm_insight import variables as V
from portfolio_dash.llm_insight.system_prompt import get_system_prompt, set_system_prompt
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.pricing.store import get_price_history
from portfolio_dash.shared.enums import Currency

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


def _build_context(
    conn: sqlite3.Connection, payload: PromptBody, now: datetime, reporting: Currency
) -> V.VarContext:
    """Build the render context with the REAL computed dashboard (+ per-symbol history)."""
    data = build_dashboard(conn, now=now, reporting=reporting)
    ctx = V.VarContext(data=data)
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
