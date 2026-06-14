"""Insight generation service (spec 04b) — the conn-bearing seam over pure llm_insight.

This is the ONLY place that reads ``pricing`` / ``portfolio`` to feed an insight run, so
``llm_insight.generate`` stays pure (architecture.md; same precedent as 06a's
``api/routers/prompts.py`` ``_build_context``). It:

1. resolves an insight_type's universe (per_symbol: ``mode:all`` → current holdings,
   ``mode:custom`` → the listed symbols; portfolio/on_alert → a single target);
2. builds one :class:`~llm_insight.variables.VarContext` per target from the REAL computed
   dashboard + per-symbol price history + external snapshots + fx (reusing the 06a
   per-variable assembly helpers);
3. computes the fed gate inputs (budget remaining, master-role configured, per-symbol
   missing prices, removed symbols);
4. delegates to the pure ``generate.run_insight_type``.

``run_for_id`` is the function the scheduler's insight runner and the manual-run endpoint
call (wired via ``scheduler.register_insight_runner`` at app startup — no scheduler→api
import).
"""

import sqlite3
from datetime import datetime, timedelta

from portfolio_dash.api.routers.prompts import (
    _dividend_rows,
    _external_reasons,
    _external_vars,
    _resolve_fx_rates,
)
from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.llm_insight import variables as V
from portfolio_dash.llm_insight.generate import RunInputs, RunResult, run_insight_type
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.portfolio.dashboard_models import DashboardData
from portfolio_dash.pricing.store import get_price_history
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.llm_config import LLMRole, budget_remaining, get_role_model_id

_HISTORY_DAYS = 180


def _resolve_universe(it: cs.InsightType, data: DashboardData) -> list[str]:
    """The per_symbol universe: custom list, or all current holdings for ``mode:all``."""
    held = sorted({h.symbol for h in data.holdings})
    universe = it.universe
    if isinstance(universe, dict):
        mode = universe.get("mode")
        if mode == "custom":
            syms = universe.get("symbols")
            return list(syms) if isinstance(syms, list) else []
        if mode == "all":
            return held
    return held  # default: follow holdings


def _per_symbol_ctx(
    conn: sqlite3.Connection,
    data: DashboardData,
    symbol: str,
    *,
    now: datetime,
    reporting: Currency,
) -> V.VarContext:
    """Build a per-symbol VarContext (dashboard + history + external snapshots + fx)."""
    external_vars = _external_vars(conn, symbol)
    ctx = V.VarContext(
        data=data,
        symbol=symbol,
        now=now,
        fx_rates=_resolve_fx_rates(conn, data, now, reporting),
        dividend_rows=_dividend_rows(conn),
        external_vars=external_vars,
        external_reasons=_external_reasons(conn, external_vars),
    )
    as_of = now.date()
    history = get_price_history(conn, symbol, as_of - timedelta(days=_HISTORY_DAYS), as_of)
    ctx.closes = [p.value for p in history]
    ctx.price_points = [{"date": p.as_of.isoformat(), "close": str(p.value)} for p in history]
    return ctx


def _portfolio_ctx(
    conn: sqlite3.Connection, data: DashboardData, *, now: datetime, reporting: Currency
) -> V.VarContext:
    """Build the portfolio-scope VarContext (no per-symbol detail)."""
    external_vars = _external_vars(conn, None)
    return V.VarContext(
        data=data,
        now=now,
        fx_rates=_resolve_fx_rates(conn, data, now, reporting),
        dividend_rows=_dividend_rows(conn),
        external_vars=external_vars,
        external_reasons=_external_reasons(conn, external_vars),
    )


def run_for_id(
    conn: sqlite3.Connection,
    insight_type_id: int,
    *,
    now: datetime,
    reporting: Currency = Currency.TWD,
    fired_rule: str | None = None,
    fired_symbol: str | None = None,
    is_shadow: bool = False,
    run_id: int | None = None,
) -> RunResult:
    """Load conn-bearing inputs and run one insight_type generation (the api seam).

    Builds the per-target VarContexts + the fed gate inputs, then delegates to the pure
    ``generate.run_insight_type``. ``fired_rule``/``fired_symbol`` are set for an on_alert
    dispatch (R7). ``run_id`` finalizes a pre-inserted running row (async manual run).
    Returns the run result.
    """
    it = cs.get_insight_type(conn, insight_type_id)
    if it is None:
        return run_insight_type(
            conn, insight_type_id, var_contexts={}, inputs=RunInputs(
                budget_remaining=budget_remaining(conn)
            ), now=now, run_id=run_id,
        )

    data = build_dashboard(conn, now=now, reporting=reporting)
    master_configured = get_role_model_id(conn, LLMRole.MASTER) is not None
    missing_prices = list(data.freshness.missing_prices)

    var_contexts: dict[str | None, V.VarContext] = {}
    universe_symbols: list[str] = []

    if it.scope == "per_symbol":
        universe_symbols = _resolve_universe(it, data)
        for sym in universe_symbols:
            ctx = _per_symbol_ctx(conn, data, sym, now=now, reporting=reporting)
            var_contexts[sym] = ctx
            # R4: a universe symbol with no price history at all (e.g. a custom-list symbol
            # not in the holdings/prices) is a missing-price anomaly → zero-LLM card.
            if not ctx.closes and sym not in missing_prices:
                missing_prices.append(sym)
    elif it.scope == "on_alert":
        target = fired_symbol
        if target is not None:
            var_contexts[target] = _per_symbol_ctx(
                conn, data, target, now=now, reporting=reporting
            )
        else:
            var_contexts[None] = _portfolio_ctx(conn, data, now=now, reporting=reporting)
    else:  # portfolio
        var_contexts[None] = _portfolio_ctx(conn, data, now=now, reporting=reporting)

    inputs = RunInputs(
        budget_remaining=budget_remaining(conn),
        master_configured=master_configured,
        universe_symbols=universe_symbols,
        missing_price_symbols=missing_prices,
        is_shadow=is_shadow,
        fired_rule=fired_rule,
        fired_symbol=fired_symbol,
    )
    return run_insight_type(
        conn, insight_type_id, var_contexts=var_contexts, inputs=inputs, now=now,
        run_id=run_id,
    )
