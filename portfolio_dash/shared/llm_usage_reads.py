"""Read-only usage aggregations over ``llm_usage`` for the spec-16 settings page.

Pure SQL reads, no writes. ``llm_usage.model`` stores the model *name* (per
``shared/llm.py`` ``log_usage``); the registry maps a name back to its display
alias here so the settings page can report per-alias breakdowns. Depends only on
stdlib + the LLM tables owned by ``shared/llm_config``.
"""

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel


class ModelUsage(BaseModel):
    """One ``by_model`` row: per-alias call/token/cost totals."""

    alias: str
    calls: int
    tokens_in: int
    tokens_out: int
    cost_usd: Decimal


class AgentUsage(BaseModel):
    """One ``by_agent`` row: per-agent cost total."""

    agent: str
    cost_usd: Decimal


class ModelHealth(BaseModel):
    """Per-alias last-call signal derived from the usage ledger."""

    alias: str
    health: str  # "ok" once any call is logged, else "unknown"
    last_called: str | None


class DailySeries(BaseModel):
    """One cost line in the daily chart: a model alias + its per-date costs."""

    alias: str
    costs: list[Decimal]


class DailyUsage(BaseModel):
    """The daily cost chart: shared date axis + one cost series per model alias."""

    dates: list[str]
    series: list[DailySeries]


def _name_to_alias(conn: sqlite3.Connection) -> dict[str, str]:
    """Map each registered ``model_name`` to a stable display alias.

    On a name collision the lowest ``id`` wins (deterministic); unregistered names
    that appear only in the usage ledger fall back to the raw name at the call site.
    """
    mapping: dict[str, str] = {}
    for row in conn.execute(
        "SELECT model_name, model_alias FROM llm_models ORDER BY id"
    ):
        mapping.setdefault(row["model_name"], row["model_alias"])
    return mapping


def usage_by_model(conn: sqlite3.Connection) -> list[ModelUsage]:
    """Per-model totals (calls, tokens in/out, cost), keyed by display alias."""
    name_to_alias = _name_to_alias(conn)
    agg: dict[str, ModelUsage] = {}
    for row in conn.execute(
        "SELECT model, input_tokens, output_tokens, cost FROM llm_usage"
    ):
        alias = name_to_alias.get(row["model"], row["model"])
        cur = agg.get(alias)
        if cur is None:
            agg[alias] = ModelUsage(
                alias=alias,
                calls=1,
                tokens_in=row["input_tokens"],
                tokens_out=row["output_tokens"],
                cost_usd=Decimal(row["cost"]),
            )
        else:
            cur.calls += 1
            cur.tokens_in += row["input_tokens"]
            cur.tokens_out += row["output_tokens"]
            cur.cost_usd += Decimal(row["cost"])
    return sorted(agg.values(), key=lambda m: m.alias)


def usage_by_agent(conn: sqlite3.Connection) -> list[AgentUsage]:
    """Per-agent cost totals."""
    agg: dict[str, Decimal] = {}
    for row in conn.execute("SELECT agent, cost FROM llm_usage"):
        agg[row["agent"]] = agg.get(row["agent"], Decimal("0")) + Decimal(row["cost"])
    return [AgentUsage(agent=a, cost_usd=c) for a, c in sorted(agg.items())]


def model_health(conn: sqlite3.Connection) -> dict[str, ModelHealth]:
    """Per-alias ``{health, last_called}`` from the latest usage row of each model.

    ``llm_usage`` carries no status column, so health is ``"ok"`` once a call has been
    logged for the model and ``"unknown"`` otherwise. Keyed by display alias.
    """
    name_to_alias = _name_to_alias(conn)
    latest: dict[str, str] = {}
    for row in conn.execute("SELECT model, ts FROM llm_usage"):
        alias = name_to_alias.get(row["model"], row["model"])
        if alias not in latest or row["ts"] > latest[alias]:
            latest[alias] = row["ts"]
    return {
        alias: ModelHealth(alias=alias, health="ok", last_called=ts)
        for alias, ts in latest.items()
    }


def usage_daily(conn: sqlite3.Connection, *, days: int = 30) -> DailyUsage:
    """Per-model daily cost series over the trailing *days* window.

    Returns a shared ``dates`` axis (``"MM-DD"``) plus one ``series`` line per model
    alias that had any spend in the window. Dates with no spend for a model read 0.
    """
    name_to_alias = _name_to_alias(conn)
    today = datetime.now(UTC).date()
    start = today - timedelta(days=days - 1)
    date_keys = [start + timedelta(days=i) for i in range(days)]
    dates = [d.strftime("%m-%d") for d in date_keys]
    iso_keys = [d.isoformat() for d in date_keys]

    # alias -> {iso_date: cost}
    per: dict[str, dict[str, Decimal]] = {}
    for row in conn.execute("SELECT model, ts, cost FROM llm_usage"):
        day = row["ts"][:10]
        if day < iso_keys[0]:
            continue
        alias = name_to_alias.get(row["model"], row["model"])
        bucket = per.setdefault(alias, {})
        bucket[day] = bucket.get(day, Decimal("0")) + Decimal(row["cost"])

    series = [
        DailySeries(alias=alias, costs=[by_day.get(k, Decimal("0")) for k in iso_keys])
        for alias, by_day in sorted(per.items())
    ]
    return DailyUsage(dates=dates, series=series)
